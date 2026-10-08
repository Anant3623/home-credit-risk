"""Deterministic SYNTHETIC integration data, never a substitute for Kaggle results.

Includes the original seven file names and headers, informative but noisy outcomes,
and documented repayment/cohort/date edge cases. Only the requested output directory
is written. No application_test outcome is invented.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import Counter
from pathlib import Path


APP_COLUMNS = (
    "SK_ID_CURR TARGET NAME_CONTRACT_TYPE CODE_GENDER FLAG_OWN_CAR FLAG_OWN_REALTY "
    "CNT_CHILDREN AMT_INCOME_TOTAL AMT_CREDIT AMT_ANNUITY AMT_GOODS_PRICE NAME_TYPE_SUITE "
    "NAME_INCOME_TYPE NAME_EDUCATION_TYPE NAME_FAMILY_STATUS NAME_HOUSING_TYPE "
    "REGION_POPULATION_RELATIVE DAYS_BIRTH DAYS_EMPLOYED DAYS_REGISTRATION DAYS_ID_PUBLISH "
    "OWN_CAR_AGE FLAG_MOBIL FLAG_EMP_PHONE FLAG_WORK_PHONE FLAG_CONT_MOBILE FLAG_PHONE "
    "FLAG_EMAIL OCCUPATION_TYPE CNT_FAM_MEMBERS REGION_RATING_CLIENT REGION_RATING_CLIENT_W_CITY "
    "WEEKDAY_APPR_PROCESS_START HOUR_APPR_PROCESS_START REG_REGION_NOT_LIVE_REGION "
    "REG_REGION_NOT_WORK_REGION LIVE_REGION_NOT_WORK_REGION REG_CITY_NOT_LIVE_CITY "
    "REG_CITY_NOT_WORK_CITY LIVE_CITY_NOT_WORK_CITY ORGANIZATION_TYPE EXT_SOURCE_1 EXT_SOURCE_2 EXT_SOURCE_3"
).split()
BUILDING_FIELDS = (
    "APARTMENTS BASEMENTAREA YEARS_BEGINEXPLUATATION YEARS_BUILD COMMONAREA ELEVATORS "
    "ENTRANCES FLOORSMAX FLOORSMIN LANDAREA LIVINGAPARTMENTS LIVINGAREA "
    "NONLIVINGAPARTMENTS NONLIVINGAREA"
).split()
APP_COLUMNS += [f"{field}_{suffix}" for suffix in ("AVG", "MODE", "MEDI") for field in BUILDING_FIELDS]
APP_COLUMNS += (
    "FONDKAPREMONT_MODE HOUSETYPE_MODE TOTALAREA_MODE WALLSMATERIAL_MODE EMERGENCYSTATE_MODE "
    "OBS_30_CNT_SOCIAL_CIRCLE DEF_30_CNT_SOCIAL_CIRCLE OBS_60_CNT_SOCIAL_CIRCLE "
    "DEF_60_CNT_SOCIAL_CIRCLE DAYS_LAST_PHONE_CHANGE"
).split()
APP_COLUMNS += [f"FLAG_DOCUMENT_{n}" for n in range(2, 22)]
APP_COLUMNS += [f"AMT_REQ_CREDIT_BUREAU_{period}" for period in ("HOUR", "DAY", "WEEK", "MON", "QRT", "YEAR")]
assert len(APP_COLUMNS) == 122

HEADERS = {
    "application_train": APP_COLUMNS,
    "bureau": (
        "SK_ID_CURR SK_ID_BUREAU CREDIT_ACTIVE CREDIT_CURRENCY DAYS_CREDIT CREDIT_DAY_OVERDUE "
        "DAYS_CREDIT_ENDDATE DAYS_ENDDATE_FACT AMT_CREDIT_MAX_OVERDUE CNT_CREDIT_PROLONG "
        "AMT_CREDIT_SUM AMT_CREDIT_SUM_DEBT AMT_CREDIT_SUM_LIMIT AMT_CREDIT_SUM_OVERDUE "
        "CREDIT_TYPE DAYS_CREDIT_UPDATE AMT_ANNUITY"
    ).split(),
    "bureau_balance": "SK_ID_BUREAU MONTHS_BALANCE STATUS".split(),
    "previous_application": (
        "SK_ID_PREV SK_ID_CURR NAME_CONTRACT_TYPE AMT_ANNUITY AMT_APPLICATION AMT_CREDIT "
        "AMT_DOWN_PAYMENT AMT_GOODS_PRICE WEEKDAY_APPR_PROCESS_START HOUR_APPR_PROCESS_START "
        "FLAG_LAST_APPL_PER_CONTRACT NFLAG_LAST_APPL_IN_DAY RATE_DOWN_PAYMENT RATE_INTEREST_PRIMARY "
        "RATE_INTEREST_PRIVILEGED NAME_CASH_LOAN_PURPOSE NAME_CONTRACT_STATUS DAYS_DECISION "
        "NAME_PAYMENT_TYPE CODE_REJECT_REASON NAME_TYPE_SUITE NAME_CLIENT_TYPE NAME_GOODS_CATEGORY "
        "NAME_PORTFOLIO NAME_PRODUCT_TYPE CHANNEL_TYPE SELLERPLACE_AREA NAME_SELLER_INDUSTRY "
        "CNT_PAYMENT NAME_YIELD_GROUP PRODUCT_COMBINATION DAYS_FIRST_DRAWING DAYS_FIRST_DUE "
        "DAYS_LAST_DUE_1ST_VERSION DAYS_LAST_DUE DAYS_TERMINATION NFLAG_INSURED_ON_APPROVAL"
    ).split(),
    "installments_payments": (
        "SK_ID_PREV SK_ID_CURR NUM_INSTALMENT_VERSION NUM_INSTALMENT_NUMBER DAYS_INSTALMENT "
        "DAYS_ENTRY_PAYMENT AMT_INSTALMENT AMT_PAYMENT"
    ).split(),
    "pos_cash_balance": (
        "SK_ID_PREV SK_ID_CURR MONTHS_BALANCE CNT_INSTALMENT CNT_INSTALMENT_FUTURE "
        "NAME_CONTRACT_STATUS SK_DPD SK_DPD_DEF"
    ).split(),
    "credit_card_balance": (
        "SK_ID_PREV SK_ID_CURR MONTHS_BALANCE AMT_BALANCE AMT_CREDIT_LIMIT_ACTUAL "
        "AMT_DRAWINGS_ATM_CURRENT AMT_DRAWINGS_CURRENT AMT_DRAWINGS_OTHER_CURRENT "
        "AMT_DRAWINGS_POS_CURRENT AMT_INST_MIN_REGULARITY AMT_PAYMENT_CURRENT "
        "AMT_PAYMENT_TOTAL_CURRENT AMT_RECEIVABLE_PRINCIPAL AMT_RECIVABLE AMT_TOTAL_RECEIVABLE "
        "CNT_DRAWINGS_ATM_CURRENT CNT_DRAWINGS_CURRENT CNT_DRAWINGS_OTHER_CURRENT "
        "CNT_DRAWINGS_POS_CURRENT CNT_INSTALMENT_MATURE_CUM NAME_CONTRACT_STATUS SK_DPD SK_DPD_DEF"
    ).split(),
}
FILENAMES = {table: f"{table}.csv" for table in HEADERS}
FILENAMES["pos_cash_balance"] = "POS_CASH_balance.csv"


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-value))


def make_fixture(output_path: Path, seed: int = 42, n_applicants: int = 2500) -> dict:
    """Write original-format CSVs and return their counts plus edge expectations."""
    if n_applicants < 100:
        raise ValueError("Use at least 100 applicants so fixture edge cases fit.")
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    rows: dict[str, list[dict]] = {table: [] for table in HEADERS}
    latent_risk: dict[int, float] = {}
    for index in range(n_applicants):
        applicant = 100001 + index
        risk = rng.gauss(0, 1)
        latent_risk[applicant] = risk
        income = round(math.exp(rng.gauss(math.log(170000), 0.45)), 2)
        credit_to_income = min(8.5, max(0.6, 3 + 0.9 * risk + rng.gauss(0, 0.7)))
        credit = round(income * credit_to_income, 2)
        age = rng.randint(22, 69)
        income_type = rng.choices(["Working", "Commercial associate", "Pensioner", "State servant"], [60, 20, 12, 8])[0]
        missing_employed = income_type == "Pensioner" or rng.random() < 0.04
        app = {column: "" for column in APP_COLUMNS}
        app.update({
            "SK_ID_CURR": applicant,
            "TARGET": int(rng.random() < _sigmoid(-1.8 + 1.15 * risk)),
            "NAME_CONTRACT_TYPE": rng.choices(["Cash loans", "Revolving loans"], [90, 10])[0],
            "CODE_GENDER": rng.choice(["M", "F"]), "FLAG_OWN_CAR": rng.choice(["Y", "N"]),
            "FLAG_OWN_REALTY": rng.choices(["Y", "N"], [68, 32])[0], "CNT_CHILDREN": rng.choice([0, 0, 0, 1, 1, 2]),
            "AMT_INCOME_TOTAL": income, "AMT_CREDIT": credit, "AMT_ANNUITY": round(credit / rng.randint(15, 45), 2),
            "AMT_GOODS_PRICE": round(credit * rng.uniform(0.85, 1.0), 2), "NAME_TYPE_SUITE": "Unaccompanied",
            "NAME_INCOME_TYPE": income_type,
            "NAME_EDUCATION_TYPE": rng.choices(["Secondary / secondary special", "Higher education", "Incomplete higher", "Lower secondary"], [64, 25, 8, 3])[0],
            "NAME_FAMILY_STATUS": rng.choices(["Married", "Single / not married", "Civil marriage", "Separated", "Widow"], [60, 20, 10, 6, 4])[0],
            "NAME_HOUSING_TYPE": rng.choices(["House / apartment", "With parents", "Rented apartment"], [80, 12, 8])[0],
            "REGION_POPULATION_RELATIVE": round(rng.uniform(0.003, 0.07), 6),
            "DAYS_BIRTH": -int(age * 365.25),
            "DAYS_EMPLOYED": 365243 if missing_employed else -int(max(30, rng.uniform(100, 5000) - 550 * risk)),
            "DAYS_REGISTRATION": -rng.randint(0, 12000), "DAYS_ID_PUBLISH": -rng.randint(0, 5000),
            "OWN_CAR_AGE": rng.randint(0, 25) if rng.random() > 0.6 else "",
            "FLAG_MOBIL": 1, "FLAG_EMP_PHONE": int(not missing_employed), "FLAG_WORK_PHONE": rng.randint(0, 1),
            "FLAG_CONT_MOBILE": 1, "FLAG_PHONE": rng.randint(0, 1), "FLAG_EMAIL": rng.randint(0, 1),
            "OCCUPATION_TYPE": rng.choices(["Laborers", "Sales staff", "Core staff", "Managers", "XNA", ""], [24, 16, 20, 10, 5, 25])[0],
            "CNT_FAM_MEMBERS": rng.choice([1, 2, 2, 3, 4]), "REGION_RATING_CLIENT": rng.choice([1, 2, 2, 3]),
            "REGION_RATING_CLIENT_W_CITY": rng.choice([1, 2, 2, 3]), "WEEKDAY_APPR_PROCESS_START": rng.choice(["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]),
            "HOUR_APPR_PROCESS_START": rng.randint(8, 18), "ORGANIZATION_TYPE": rng.choice(["Business Entity Type 3", "Self-employed", "Government", "XNA"]),
            "DAYS_LAST_PHONE_CHANGE": -rng.randint(0, 2500),
            "OBS_30_CNT_SOCIAL_CIRCLE": rng.randint(0, 5), "DEF_30_CNT_SOCIAL_CIRCLE": rng.choice([0, 0, 0, 1]),
            "OBS_60_CNT_SOCIAL_CIRCLE": rng.randint(0, 5), "DEF_60_CNT_SOCIAL_CIRCLE": rng.choice([0, 0, 0, 1]),
        })
        for field in ("REG_REGION_NOT_LIVE_REGION", "REG_REGION_NOT_WORK_REGION", "LIVE_REGION_NOT_WORK_REGION", "REG_CITY_NOT_LIVE_CITY", "REG_CITY_NOT_WORK_CITY", "LIVE_CITY_NOT_WORK_CITY"):
            app[field] = int(rng.random() < 0.12)
        for source, noise, missing_rate in ((1, 0.6, 0.40), (2, 0.3, 0.04), (3, 0.5, 0.18)):
            app[f"EXT_SOURCE_{source}"] = "" if rng.random() < missing_rate else round(_sigmoid(-risk + rng.gauss(0, noise)), 6)
        for suffix in ("AVG", "MODE", "MEDI"):
            for field in BUILDING_FIELDS:
                app[f"{field}_{suffix}"] = "" if rng.random() < 0.6 else round(rng.uniform(0, 1), 5)
        for number in range(2, 22):
            app[f"FLAG_DOCUMENT_{number}"] = int(rng.random() < (0.7 if number == 3 else 0.04))
        for period in ("HOUR", "DAY", "WEEK", "MON", "QRT", "YEAR"):
            app[f"AMT_REQ_CREDIT_BUREAU_{period}"] = rng.randint(0, 4) if rng.random() > 0.13 else ""
        if index == 0:
            app["DAYS_EMPLOYED"] = 365243
            app["NAME_TYPE_SUITE"] = "XAP"
        elif index == 1:
            app["DAYS_BIRTH"] = -40000  # Audited and nulled in staging.
        elif index == 2:
            app["AMT_CREDIT"] = -100  # Audited and nulled in staging.
        elif index == 3:
            app["AMT_INCOME_TOTAL"] = 15000000  # Audit, not automatic deletion.
        rows["application_train"].append(app)

    next_previous = 21000000
    next_bureau = 31000000

    def previous(applicant: int, loan_id: int, status: str = "Approved", decision: int = -500) -> None:
        amount = round(rng.uniform(10000, 250000), 2)
        row = {column: "" for column in HEADERS["previous_application"]}
        row.update({"SK_ID_PREV": loan_id, "SK_ID_CURR": applicant, "NAME_CONTRACT_TYPE": "Cash loans", "AMT_ANNUITY": round(amount / 12, 2), "AMT_APPLICATION": amount, "AMT_CREDIT": amount, "AMT_DOWN_PAYMENT": 0, "AMT_GOODS_PRICE": amount, "WEEKDAY_APPR_PROCESS_START": "MONDAY", "HOUR_APPR_PROCESS_START": 12, "FLAG_LAST_APPL_PER_CONTRACT": "Y", "NFLAG_LAST_APPL_IN_DAY": 1, "RATE_DOWN_PAYMENT": 0, "NAME_CASH_LOAN_PURPOSE": "XAP", "NAME_CONTRACT_STATUS": status, "DAYS_DECISION": decision, "NAME_PAYMENT_TYPE": "Cash through the bank", "CODE_REJECT_REASON": "XAP" if status == "Approved" else "HC", "NAME_TYPE_SUITE": "Unaccompanied", "NAME_CLIENT_TYPE": "Repeater", "NAME_GOODS_CATEGORY": "XNA", "NAME_PORTFOLIO": "Cash", "NAME_PRODUCT_TYPE": "x-sell", "CHANNEL_TYPE": "Credit and cash offices", "SELLERPLACE_AREA": 100, "NAME_SELLER_INDUSTRY": "XNA", "CNT_PAYMENT": 12, "NAME_YIELD_GROUP": "middle", "PRODUCT_COMBINATION": "Cash X-Sell: middle", "DAYS_FIRST_DRAWING": 365243, "DAYS_FIRST_DUE": decision + 30, "DAYS_LAST_DUE_1ST_VERSION": decision + 390, "DAYS_LAST_DUE": decision + 390 if status == "Approved" else 365243, "DAYS_TERMINATION": decision + 400 if status == "Approved" else 365243, "NFLAG_INSURED_ON_APPROVAL": 0})
        rows["previous_application"].append(row)

    def payment(applicant: int, loan: int, number: int, due: int | str, paid_day: int | str, scheduled: float | str, paid: float | str, version: int = 1) -> None:
        rows["installments_payments"].append(dict(zip(HEADERS["installments_payments"], [loan, applicant, version, number, due, paid_day, scheduled, paid])))

    def bureau(applicant: int, loan: int, active: str = "Active") -> None:
        amount = round(rng.uniform(20000, 300000), 2)
        rows["bureau"].append({"SK_ID_CURR": applicant, "SK_ID_BUREAU": loan, "CREDIT_ACTIVE": active, "CREDIT_CURRENCY": "currency 1", "DAYS_CREDIT": -rng.randint(200, 1500), "CREDIT_DAY_OVERDUE": rng.choice([0, 0, 0, 10, 45]), "DAYS_CREDIT_ENDDATE": 180 if active == "Active" else -90, "DAYS_ENDDATE_FACT": "" if active == "Active" else -90, "AMT_CREDIT_MAX_OVERDUE": 0, "CNT_CREDIT_PROLONG": 0, "AMT_CREDIT_SUM": amount, "AMT_CREDIT_SUM_DEBT": round(amount * rng.uniform(0.1, 0.9), 2) if active == "Active" else 0, "AMT_CREDIT_SUM_LIMIT": amount, "AMT_CREDIT_SUM_OVERDUE": 0, "CREDIT_TYPE": rng.choice(["Consumer credit", "Credit card"]), "DAYS_CREDIT_UPDATE": -10, "AMT_ANNUITY": round(amount / 20, 2)})

    def bb(loan: int, month: int, status: str) -> None:
        rows["bureau_balance"].append({"SK_ID_BUREAU": loan, "MONTHS_BALANCE": month, "STATUS": status})

    def pos(applicant: int, loan: int, month: int, dpd: int) -> None:
        rows["pos_cash_balance"].append({"SK_ID_PREV": loan, "SK_ID_CURR": applicant, "MONTHS_BALANCE": month, "CNT_INSTALMENT": 12, "CNT_INSTALMENT_FUTURE": max(0, -month), "NAME_CONTRACT_STATUS": "Active", "SK_DPD": dpd, "SK_DPD_DEF": dpd})

    def card(applicant: int, loan: int, month: int, balance: float, limit: float = 50000, dpd: int = 0) -> None:
        row = {column: 0 for column in HEADERS["credit_card_balance"]}
        row.update({"SK_ID_PREV": loan, "SK_ID_CURR": applicant, "MONTHS_BALANCE": month, "AMT_BALANCE": round(balance, 2), "AMT_CREDIT_LIMIT_ACTUAL": limit, "AMT_DRAWINGS_CURRENT": round(max(0, balance) * 0.2, 2), "AMT_PAYMENT_CURRENT": 2500, "AMT_PAYMENT_TOTAL_CURRENT": 2500, "AMT_RECEIVABLE_PRINCIPAL": max(0, balance), "AMT_RECIVABLE": max(0, balance), "AMT_TOTAL_RECEIVABLE": max(0, balance), "CNT_DRAWINGS_CURRENT": 1, "CNT_INSTALMENT_MATURE_CUM": 12, "NAME_CONTRACT_STATUS": "Active", "SK_DPD": dpd, "SK_DPD_DEF": dpd})
        rows["credit_card_balance"].append(row)

    # Reserve applicant 100001 exclusively for transparent arithmetic edge tests.
    for applicant in range(100002, 100001 + n_applicants):
        risk = latent_risk[applicant]
        if rng.random() < 0.14:
            continue  # Applicants without recorded history remain in the fact table.
        for _ in range(rng.randint(1, 3)):
            next_previous += 1
            status = rng.choices(["Approved", "Refused", "Canceled", "Unused offer"], [65, 18 + max(0, risk) * 5, 9, 8])[0]
            previous(applicant, next_previous, status)
            if status == "Approved":
                for number in range(1, rng.randint(4, 9)):
                    due = -270 + number * 30
                    late = rng.randint(1, 45) if rng.random() < _sigmoid(-1.5 + risk) else -rng.randint(0, 7)
                    scheduled = rng.choice([1500, 2500, 4000, 6000])
                    if rng.random() < 0.08:
                        payment(applicant, next_previous, number, due, due - 5, scheduled, scheduled * 0.5)
                        payment(applicant, next_previous, number, due, due + late, scheduled, scheduled * 0.5)
                    else:
                        payment(applicant, next_previous, number, due, due + late, scheduled, scheduled)
                for month in (-3, -2, -1, 0):
                    dpd = rng.randint(1, 60) if rng.random() < _sigmoid(-2 + risk) else 0
                    pos(applicant, next_previous, month, dpd)
                if rng.random() < 0.3:
                    for month in (-3, -2, -1, 0):
                        utilization = min(1.3, max(0, 0.5 + risk * 0.15 + rng.gauss(0, 0.15)))
                        card(applicant, next_previous, month, utilization * 50000, dpd=0)
        if rng.random() < 0.85:
            for _ in range(rng.randint(1, 3)):
                next_bureau += 1
                active = rng.choice(["Active", "Active", "Closed"])
                bureau(applicant, next_bureau, active)
                for month in range(-6, 1):
                    status = rng.choices(["0", "1", "2", "3", "4", "5", "X"], [70, 14 + max(0, risk) * 10, 7, 4, 2, 1, 2])[0]
                    if month == 0 and active == "Closed":
                        status = "C"
                    bb(next_bureau, month, status)

    # Exact controlled edges: partial payments, versions, conflicts, unpaid, dates.
    previous(100001, 20000001)
    previous(100001, 20000002)
    previous(100001, 20000003, status="Refused")
    payment(100001, 20000001, 1, -30, -35, 100, 50)
    payment(100001, 20000001, 1, -30, -20, 100, 50)
    payment(100001, 20000001, 2, -60, -59, 100, 25)
    payment(100001, 20000001, 2, -60, -59, 100, 75)
    payment(100001, 20000001, 1, -90, -90, 100, 100, version=2)
    payment(100001, 20000001, 3, -10, "", 100, "")
    payment(100001, 20000001, 4, 5, 6, 100, 100)
    payment(100001, 20000001, 5, -5, 3, 100, 100)
    payment(100001, 20000001, 6, -100, -101, 100, 125)  # Overpayment retained.
    payment(100001, 20000002, 1, -50, -45, 80, 40)
    payment(100001, 20000002, 1, -49, -44, 100, 40)
    # Exact duplicates are audited, not silently removed. Isolate another applicant.
    previous(100005, 20000004)
    payment(100005, 20000004, 1, -30, -30, 100, 50)
    payment(100005, 20000004, 1, -30, -30, 100, 50)
    # History loan need not be represented in the prior-application extract.
    payment(100006, 88888001, 1, -20, -20, 100, 100)
    pos(100001, 20000001, -1, 5)
    pos(100001, 20000001, 0, 0)
    pos(100001, 20000001, 1, 999)  # Must be excluded from history.
    card(100001, 20000002, -1, -50)
    card(100001, 20000002, 0, 25000)
    card(100001, 20000002, 1, 999999, dpd=999)
    bureau(100001, 30000001)
    for month, status in ((-4, "1"), (-3, "2"), (-1, "0"), (0, "C"), (1, "5")):
        bb(30000001, month, status)
    bureau(100001, 30000002, "Closed")
    for month, status in ((-4, "3"), (-3, "4"), (-2, "5"), (-1, "X"), (0, "Z")):
        bb(30000002, month, status)
    bb(89999999, -1, "2")  # Missing parent bureau row, distinct from other cohort.
    outside_train = 99999990
    previous(outside_train, 99999001)
    payment(outside_train, 99999001, 1, -30, -20, 100, 100)
    pos(outside_train, 99999001, -1, 10)
    card(outside_train, 99999001, -1, 40000)
    bureau(outside_train, 99998001)
    bb(99998001, -1, "2")

    counts = {}
    for table, columns in HEADERS.items():
        file_path = output_path / FILENAMES[table]
        with file_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows[table])
        counts[table] = len(rows[table])
    targets = Counter(row["TARGET"] for row in rows["application_train"])
    metadata = {
        "data_kind": "SYNTHETIC — integration and workflow validation only; no real Home Credit results",
        "seed": seed, "n_applicants": n_applicants, "counts": counts,
        "default_count": targets[1], "default_rate": targets[1] / n_applicants,
        "files": FILENAMES,
        "edge_cases": {
            "applicant_id": 100001, "partial_payment_loan_id": 20000001,
            "partial_payment_installment": {"version": 1, "number": 1, "scheduled_amount": 100, "paid_amount": 100, "settlement_day": -20, "days_late": 10},
            "same_day_installment": {"version": 1, "number": 2, "scheduled_amount": 100, "paid_amount": 100, "settlement_day": -59, "days_late": 1},
            "distinct_version_installment": {"version": 2, "number": 1, "days_late": 0},
            "unpaid_installment": {"version": 1, "number": 3, "paid_amount": 0, "days_late_lower_bound": 10},
            "future_due_installment": {"version": 1, "number": 4, "excluded": True},
            "future_payment_installment": {"version": 1, "number": 5, "paid_amount": 0, "days_late_lower_bound": 5},
            "overpaid_installment": {"version": 1, "number": 6, "scheduled_amount": 100, "paid_amount": 125},
            "schedule_conflict_loan_id": 20000002,
            "schedule_conflict": {"version": 1, "number": 1, "chosen_due_day": -50, "chosen_scheduled_amount": 100, "paid_amount": 80},
            "exact_duplicate_loan_id": 20000004,
            "absent_previous_parent_loan_id": 88888001,
            "bureau_gap_loan_id": 30000001, "bureau_unknown_loan_id": 30000002,
            "absent_bureau_parent_loan_id": 89999999,
            "outside_training_applicant_id": outside_train,
            "sentinel_employed_applicant_id": 100001, "invalid_age_applicant_id": 100002,
            "negative_credit_applicant_id": 100003, "high_income_applicant_id": 100004,
            "negative_card_balance": -50, "observed_card_utilization": 0.5,
            "observed_pos_max_dpd": 5,
        },
        "notes": [
            "Raw primary keys and monthly snapshot grains are unique. Duplicate-PK tests use a rolled-back transaction.",
            "Outside-training histories are excluded by cohort, while missing prior extract loan links are audited separately.",
            "The fixture intentionally includes amount/due-date conflicts and exact duplicate installment rows for auditing.",
            "Generated TARGET is probabilistic with informative noisy external scores, affordability, and histories.",
        ],
    }
    (output_path / "meta_fixture.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--applicants", type=int, default=2500)
    args = parser.parse_args()
    metadata = make_fixture(args.output, args.seed, args.applicants)
    print(json.dumps({"data_kind": "SYNTHETIC", "output": str(args.output.resolve()), "counts": metadata["counts"], "default_rate": metadata["default_rate"]}, indent=2))


if __name__ == "__main__":
    main()
