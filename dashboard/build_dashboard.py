"""Build the public, aggregated-only dashboard (dashboard/index.html).

Reads the local (git-ignored) application mart and the committed aggregate outputs,
and embeds ONLY aggregated tables in the page: no applicant-level rows, and any
published cell with fewer than 10 applicants is suppressed.

Run from the project root after the pipeline has produced runs/real:
    python dashboard/build_dashboard.py
"""
import base64, gzip, itertools, json, math
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs" / "real"
EXP, ANA = RUN / "exports", RUN / "analysis"
MIN = 10  # minimum applicants for any published cell

INC = ['Working', 'Commercial associate', 'Pensioner', 'State servant', 'Unemployed', 'Student', 'Businessman', 'Maternity leave']
EDU = ['Secondary / secondary special', 'Higher education', 'Incomplete higher', 'Lower secondary', 'Academic degree']
AGE = ['<25', '25-34', '35-44', '45-54', '55-64', '65+']
FAM = ['Married', 'Single / not married', 'Civil marriage', 'Separated', 'Widow', 'Unknown']
BK = ['Current', '1-30', '31-60', '61-90', '91-120', '120+', 'Closed', 'Unknown']
BINS = {
    'ext3': ['Missing', '<0.20', '0.20–0.35', '0.35–0.50', '0.50–0.65', '0.65–0.80', '0.80+'],
    'emp': ['Not employed / missing', '<1 yr', '1–3 yrs', '3–5 yrs', '5–10 yrs', '10+ yrs'],
    'cti': ['<2×', '2–3×', '3–4×', '4–6×', '6×+'],
    'ref': ['No prior applications', '0 refused', '1', '2', '3–4', '5+'],
    'late': ['No EMI history', '0%', '0–5%', '5–15%', '15–30%', '30%+'],
    'bdpd': ['No bureau record', '0 days', '1–30 days', '31–90 days', '91+ days'],
    'ccu': ['No card history', '<10%', '10–50%', '50–90%', '90%+'],
}


def cut(x, edges, missing=True):
    b = np.digitize(x, edges) + (1 if missing else 0)
    return np.where(pd.isna(x), 0, b) if missing else b


def clean(o):
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else o
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return clean(float(o))
    if isinstance(o, list):
        return [clean(x) for x in o]
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    return o


def load_applicants():
    cols = ['target', 'contract_type', 'income_type', 'education_type', 'family_status', 'occupation_type', 'region_rating',
            'employed_years', 'credit_to_income', 'ext_source_3', 'installment_late_rate', 'bureau_max_dpd',
            'previous_application_count', 'previous_refused_count', 'cc_utilization_mean', 'profile_id', 'pd',
            'risk_score', 'risk_band', 'split', 'ead_proxy']
    f = pd.read_csv(EXP / 'fact_application.csv', usecols=cols)
    prof = pd.read_csv(EXP / 'dim_profile.csv')[['profile_id', 'age_band']]
    f = f.merge(prof, on='profile_id', how='left')
    occ = ['Not stated'] + list(f.occupation_type.value_counts().index)
    r = f.previous_refused_count
    d = pd.DataFrame({
        't': f.target.values,
        'sp': f.split.map({'train': 0, 'validation': 1, 'test': 2}).values,
        'con': (f.contract_type == 'Revolving loans').astype(int).values,
        'reg': f.region_rating.astype(int).values,
        'band': f.risk_band.astype(int).values,
        'inc': f.income_type.map({v: i for i, v in enumerate(INC)}).values,
        'edu': f.education_type.map({v: i for i, v in enumerate(EDU)}).values,
        'age': f.age_band.map({v: i for i, v in enumerate(AGE)}).values,
        'fam': f.family_status.map({v: i for i, v in enumerate(FAM)}).values,
        'occ': f.occupation_type.fillna('Not stated').map({v: i for i, v in enumerate(occ)}).values,
        'pd': f.pd.values, 'ead': f.ead_proxy.values,
        'h': np.clip(np.floor(f.risk_score).astype(int) - 460, 0, 209),
        'ext3': cut(f.ext_source_3, [0.2, 0.35, 0.5, 0.65, 0.8]),
        'emp': cut(f.employed_years, [1, 3, 5, 10]),
        'cti': cut(f.credit_to_income, [2, 3, 4, 6], False),
        'ref': np.select([f.previous_application_count == 0, r == 0, r == 1, r == 2, r <= 4], [0, 1, 2, 3, 4], 5),
        'late': np.where(f.installment_late_rate.isna(), 0, cut(f.installment_late_rate.fillna(0), [1e-9, 0.05, 0.15, 0.30], False) + 1),
        'bdpd': np.where(f.bureau_max_dpd.isna(), 0, cut(f.bureau_max_dpd.fillna(0), [1, 31, 91], False) + 1),
        'ccu': np.where(f.cc_utilization_mean.isna(), 0, cut(f.cc_utilization_mean.fillna(0), [0.1, 0.5, 0.9], False) + 1),
    })
    d['pde'] = d.pd * d.ead
    return d, occ


def match(d, flt, skip=None):
    m = np.ones(len(d), bool)
    for k, v in flt.items():
        if k != skip and v >= 0:
            m &= d[k].values == v
    return d[m]


def counts(sub, key, size, with_pd=False):
    g = sub.groupby(key).agg(n=('t', 'size'), b=('t', 'sum'), p=('pd', 'sum'))
    out = [[0, 0, 0] if with_pd else [0, 0] for _ in range(size)]
    for i, row in g.iterrows():
        if row.n >= MIN:
            out[int(i)] = [int(row.n), int(row.b), round(float(row.p), 3)] if with_pd else [int(row.n), int(row.b)]
    return out


def build_combos(d, occ):
    sizes = {'con': 2, 'reg': 4, 'inc': 8, 'edu': 5, 'age': 6, 'fam': 6, 'occ': len(occ), 'band': 11}
    combos = {}
    for sp, con, reg, band in itertools.product([-1, 0, 1, 2], [-1, 0, 1], [-1, 1, 2, 3], [-1] + list(range(1, 11))):
        flt = {'sp': sp, 'con': con, 'reg': reg, 'band': band}
        sub = match(d, flt)
        key = f"{sp}|{con}|{reg}|{band}"
        if len(sub) == 0:
            combos[key] = None
            continue
        o = {'n': int(len(sub)), 'bad': int(sub.t.sum()), 'pd': round(float(sub.pd.sum()), 2),
             'ead': round(float(sub.ead.sum()), 0), 'pde': round(float(sub.pde.sum()), 0)}
        o['seg'] = {k: counts(sub, k, sizes[k], True) for k in ['inc', 'edu', 'age', 'fam', 'occ']}
        for k in ['con', 'reg', 'band']:  # filterable dimensions ignore their own filter
            o['seg'][k] = counts(match(d, flt, k), k, sizes[k], True)
        heat = [[0, 0] for _ in range(6 * 8)]
        for (a, i), row in sub.groupby(['age', 'inc']).t.agg(['size', 'sum']).iterrows():
            if row['size'] >= MIN:
                heat[int(a) * 8 + int(i)] = [int(row['size']), int(row['sum'])]
        o['heat'] = heat
        o['drv'] = {k: counts(sub, k, len(BINS[k])) for k in BINS}
        o['hist'] = [np.bincount(sub.h[sub.t == 0], minlength=210).tolist(), np.bincount(sub.h[sub.t == 1], minlength=210).tolist()]
        combos[key] = o
    return combos


def build_strategy(d):
    strat = {}
    for con, reg, band in itertools.product([-1, 0, 1], [-1, 1, 2, 3], [-1] + list(range(1, 11))):
        sub = match(d, {'sp': -1, 'con': con, 'reg': reg, 'band': band})
        v = np.sort(sub[sub.sp == 1].pd.values)
        t = sub[sub.sp == 2]
        key = f"{con}|{reg}|{band}"
        if len(v) < 50 or len(t) < 50:
            strat[key] = None
            continue
        # cut-offs from validation quantiles, evaluated on test
        cuts = np.array([float(v[min(len(v), max(1, math.ceil(a / 100 * len(v)))) - 1]) for a in range(1, 101)])
        ts = t.sort_values('pd')
        P, T, E, PE, I, n = ts.pd.values, ts.t.values, ts.ead.values, ts.pde.values, ts.inc.values, len(ts)
        pos = np.searchsorted(P, cuts, side='right')
        cT, cE, cPE = (np.concatenate([[0], np.cumsum(x)]) for x in (T, E, PE))
        oh = np.zeros((n, 8), int)
        oh[np.arange(n), I] = 1
        cI = np.vstack([np.zeros((1, 8), int), np.cumsum(oh, 0)])
        cIb = np.vstack([np.zeros((1, 8), int), np.cumsum(oh * T[:, None], 0)])
        rows = [[int(p), int(cT[p]), round(float(cE[p]), 0), round(float(cPE[p]), 0)] for p in pos]
        pb, swaps = pos[49], []
        for p in pos:
            lo, hi = min(p, pb), max(p, pb)
            a, b = cI[hi] - cI[lo], cIb[hi] - cIb[lo]
            swaps.append([[int(a[k]), int(b[k])] if a[k] >= MIN else [0, 0] for k in range(8)])
        idx = [max(1, int(round(n * k / 100))) for k in range(1, 101)]
        strat[key] = {'cut': [round(float(c), 6) for c in cuts], 'rows': rows, 'swap': swaps,
                      'trade': [round(100 * cT[i] / i, 4) for i in idx], 'n': int(n), 'bad': int(t.t.sum())}
    return strat


def static_tables():
    rd = pd.read_csv
    rb = rd(EXP / 'risk_band_metrics.csv')
    rb = rb[rb.split.isin(['validation', 'test'])][['split', 'risk_band', 'applicants', 'bad_rate', 'bad_rate_ci_low', 'bad_rate_ci_high', 'mean_predicted_pd']]
    cal = rd(ANA / 'calibration.csv')
    cal = cal[cal.split == 'test'][['bin', 'pd_lower', 'pd_upper', 'applicants', 'mean_predicted_pd', 'observed_bad_rate', 'bad_rate_ci_low', 'bad_rate_ci_high']]
    iv = rd(ANA / 'iv_summary.csv')
    iv = iv[iv.selected].sort_values('iv', ascending=False)[['feature', 'iv', 'kind']]
    iv = iv.merge(rd(ANA / 'chi_square_tests.csv')[['feature', 'cramers_v']], on='feature', how='left').round(4).fillna(-1)
    bi = {b: i for i, b in enumerate(BK)}
    ag = rd(EXP / 'agg_repayment_monthly.csv').groupby(['relative_month', 'dpd_bucket', 'risk_band', 'contract_id', 'region_rating_id']).observed_loan_month_count.sum().reset_index()
    ro = rd(EXP / 'fact_roll_rate.csv').groupby(['from_bucket', 'to_bucket', 'risk_band', 'contract_id', 'region_rating_id']).transition_count.sum().reset_index()
    dq = rd(EXP / 'dq_results.csv')
    dqs = dq[dq.status != 'PASS'].groupby(['check_name', 'table_name']).issue_count.max().reset_index().sort_values('issue_count', ascending=False)
    return {
        'rb': rb.round(6).values.tolist(), 'cal': cal.round(6).values.tolist(), 'iv': iv.values.tolist(),
        'or': rd(ANA / 'odds_ratios.csv')[['feature', 'coefficient', 'odds_ratio']].round(4).values.tolist(),
        'mm': rd(ANA / 'model_metrics.csv').round(4).values.tolist(),
        'ct': rd(EXP / 'cutoff_table.csv')[['requested_validation_approval_rate', 'pd_cutoff', 'test_approval_rate', 'test_bad_rate']].round(6).values.tolist(),
        'ag': np.column_stack([ag.relative_month + 96, ag.dpd_bucket.map(bi), ag.risk_band, ag.contract_id, ag.region_rating_id, ag.observed_loan_month_count]).astype(int).flatten().tolist(),
        'ro': np.column_stack([ro.from_bucket.map(bi), ro.to_bucket.map(bi), ro.risk_band, ro.contract_id, ro.region_rating_id, ro.transition_count]).astype(int).flatten().tolist(),
        'dq': dqs.values.tolist(),
        'dqn': [int((dq.status == 'PASS').sum()), int((dq.status == 'WARN').sum()), int((dq.status == 'FAIL').sum())],
    }


def main():
    d, occ = load_applicants()
    data = {'bins': BINS, 'INC': INC, 'EDU': EDU, 'AGE': AGE, 'FAM': FAM, 'OCC': occ, 'BK': BK, 'MIN': MIN}
    data.update(static_tables())
    data['combos'] = build_combos(d, occ)
    data['strat'] = build_strategy(d)
    payload = json.dumps(clean(data), separators=(',', ':'), allow_nan=False).encode()
    b64 = base64.b64encode(gzip.compress(payload, 9)).decode()
    template = (Path(__file__).parent / 'template.html').read_text()
    (Path(__file__).parent / 'index.html').write_text(template.replace('__DATA__', b64))
    print(f"dashboard/index.html written ({len(b64) / 1e6:.2f} MB embedded, aggregated only)")


if __name__ == '__main__':
    main()
