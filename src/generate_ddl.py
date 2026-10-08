"""Generate a conservative raw schema from CSV headers and sampled values.

Amounts use unconstrained NUMERIC, never floating point or a rounding scale.
Raw columns retain original names, sentinels, categories and missing values.
An inference failure triggers an explicit lossless TEXT fallback in the loader.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import itertools
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

FILES = {
    "application_train": ("application_train.csv", 307511),
    "previous_application": ("previous_application.csv", 1670214),
    "installments_payments": ("installments_payments.csv", 13605401),
    "pos_cash_balance": ("POS_CASH_balance.csv", 10001358),
    "credit_card_balance": ("credit_card_balance.csv", 3840312),
    "bureau": ("bureau.csv", 1716428),
    "bureau_balance": ("bureau_balance.csv", 27299925),
}

def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'

def discover_files(data_dir: Path) -> dict[str, Path]:
    paths = {}
    for table, (name, _) in FILES.items():
        candidates = sorted(data_dir.rglob(name))
        if not candidates:
            raise FileNotFoundError(f"Missing required input: {name} in {data_dir}")
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous input {name}: {candidates}; use a directory containing one copy")
        paths[table] = candidates[0].resolve()
    return paths

def infer_type(name: str, values: list[str]) -> str:
    populated = [v for v in values if v != ""]
    # STATUS has numeric-looking samples but also C and X later in the file.
    if name == "STATUS" or name.startswith(("NAME_", "CODE_", "WEEKDAY_", "FONDKAPREMONT_", "HOUSETYPE_", "WALLSMATERIAL_", "EMERGENCYSTATE_")):
        return "TEXT"
    if name.startswith("SK_ID_"):
        return "BIGINT"
    if populated:
        try:
            decimals = [Decimal(v) for v in populated]
            if any(not d.is_finite() for d in decimals):
                return "TEXT"
            # Choose BIGINT only when the input uses integral lexical values.
            if all(re.fullmatch(r"[+-]?\d+", v) and -(2**63) <= int(v) < 2**63 for v in populated):
                return "BIGINT"
            return "NUMERIC"
        except (InvalidOperation, ValueError):
            return "TEXT"
    # Entirely empty sampled numeric fields must still work in typed views.
    if name.startswith(("AMT_", "DAYS_", "CNT_", "NUM_", "EXT_SOURCE_", "RATE_", "HOUR_", "REGION_RATING_")) or name.endswith(("_AVG", "_MODE", "_MEDI")):
        return "NUMERIC"
    return "TEXT"

def table_ddl(table: str, columns: list[dict]) -> str:
    column_lines = ",\n".join(f"    {quote_ident(c['name'])} {c['type']}" for c in columns)
    return f"CREATE TABLE IF NOT EXISTS raw.{quote_ident(table)} (\n{column_lines}\n);\n"

def generate(data_dir: Path, output_dir: Path, sample_rows: int = 2000) -> dict:
    if sample_rows < 1:
        raise ValueError("sample_rows must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": "raw", "sample_rows": sample_rows, "tables": {}}
    statements = ["-- Generated from CSV headers; no cleaning is performed here.\nCREATE SCHEMA IF NOT EXISTS raw;\n"]
    for table, path in discover_files(data_dir).items():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            if not header or len(header) != len(set(header)) or any(not c for c in header):
                raise ValueError(f"Empty or duplicate header in {path}")
            sampled = list(itertools.islice(reader, sample_rows))
            if any(len(row) != len(header) for row in sampled):
                raise ValueError(f"Malformed CSV record in sample: {path}")
            columns = [{"name": name, "type": infer_type(name, [r[i] for r in sampled])} for i, name in enumerate(header)]
        entry = {"file": str(path), "bytes": path.stat().st_size, "expected_rows": FILES[table][1], "column_count": len(header), "columns": columns}
        manifest["tables"][table] = entry
        statements.append(table_ddl(table, columns))
    (output_dir / "01_raw_tables.sql").write_text("\n".join(statements))
    (output_dir / "raw_schema.json").write_text(json.dumps(manifest, indent=2))
    return manifest

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("sql/generated"))
    parser.add_argument("--sample-rows", type=int, default=2000)
    args = parser.parse_args()
    generate(args.data_dir, args.output_dir, args.sample_rows)
