"""Atomic, resumable CSV imports using psql's client-side \\copy."""
from __future__ import annotations
import json
import os
import subprocess
import time
from pathlib import Path
import psycopg
from psycopg import sql
from .database import pg_binary
from .generate_ddl import FILES, sha256_file, table_ddl

def load_raw(connection, dsn: str, manifest: dict, output_dir: Path, data_status: str, reload: bool = False) -> list[dict]:
    connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
    connection.execute("CREATE SCHEMA IF NOT EXISTS audit")
    connection.execute("""CREATE TABLE IF NOT EXISTS audit.raw_imports (
        table_name text PRIMARY KEY, source_sha256 text NOT NULL, row_count bigint NOT NULL,
        byte_count bigint NOT NULL, data_status text NOT NULL, inferred_fallback boolean NOT NULL,
        imported_at timestamptz NOT NULL DEFAULT now())""")
    evidence = []
    for table, entry in manifest["tables"].items():
        path = Path(entry["file"])
        checksum = sha256_file(path)
        old = connection.execute("SELECT source_sha256,row_count,data_status,inferred_fallback FROM audit.raw_imports WHERE table_name=%s", (table,)).fetchone()
        actual = None
        exists = connection.execute("SELECT to_regclass(%s)", ("raw." + table,)).fetchone()[0]
        if old and exists and old[0] == checksum and old[2] == data_status and not reload:
            actual = connection.execute(sql.SQL("SELECT count(*) FROM raw.{}").format(sql.Identifier(table))).fetchone()[0]
            if actual != old[1]:
                raise ValueError(f"Raw table {table} changed after import; rerun with --reload in the dedicated project DB")
            fallback = bool(old[3])
            if fallback:
                entry["inferred_columns"] = entry["columns"]
                entry["columns"] = [{"name": c["name"], "type": "TEXT"} for c in entry["columns"]]
            elapsed = 0
            print(f"Resume {table}: {actual:,} rows", flush=True)
        else:
            if exists and not reload:
                raise ValueError(f"raw.{table} already exists with a different/missing import fingerprint; use --reload only in this dedicated project DB")
            if exists:
                # Clear only the project's derived schemas to release view dependencies.
                # Caller explicitly opted into --reload; other schemas are untouched.
                for schema in ["marts", "intermediate", "staging"]:
                    connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
                connection.execute(sql.SQL("DROP TABLE raw.{}").format(sql.Identifier(table)))
            connection.execute(table_ddl(table, entry["columns"]))
            start = time.monotonic()
            fallback = False
            columns = ",".join('"' + c['name'].replace('"', '""') + '"' for c in entry["columns"])
            file_literal = "'" + str(path).replace("'", "''") + "'"
            copy_command = f"\\copy raw.\"{table}\" ({columns}) FROM {file_literal} WITH (FORMAT CSV, HEADER TRUE, NULL '')"
            script = output_dir / f"02_load_{table}.sql"
            script.write_text("\\set ON_ERROR_STOP on\nBEGIN;\n" + copy_command + "\nCOMMIT;\n")
            env = dict(os.environ, PGCONNECT_TIMEOUT="15")
            result = subprocess.run([pg_binary("psql"), "-X", "-d", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(script)], env=env, capture_output=True, text=True)
            if result.returncode:
                # Inference cannot prove a type for unseen rows. Preserve all source
                # values by retrying this table as TEXT, never skip a bad record.
                if "invalid input syntax" not in result.stderr and "out of range" not in result.stderr:
                    raise RuntimeError(f"CSV load failed for {table}: {result.stderr[-1600:]}")
                connection.execute(sql.SQL("DROP TABLE raw.{}").format(sql.Identifier(table)))
                entry["inferred_columns"] = entry["columns"]
                entry["columns"] = [{"name": c["name"], "type": "TEXT"} for c in entry["columns"]]
                connection.execute(table_ddl(table, entry["columns"]))
                fallback = True
                result = subprocess.run([pg_binary("psql"), "-X", "-d", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(script)], env=env, capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(f"Lossless fallback load failed for {table}: {result.stderr[-1600:]}")
                print(f"{table}: unseen numeric value caused type fallback; every raw field preserved as TEXT", flush=True)
            actual = connection.execute(sql.SQL("SELECT count(*) FROM raw.{}").format(sql.Identifier(table))).fetchone()[0]
            elapsed = time.monotonic() - start
            connection.execute("""INSERT INTO audit.raw_imports(table_name,source_sha256,row_count,byte_count,data_status,inferred_fallback)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(table_name) DO UPDATE SET
                source_sha256=excluded.source_sha256,row_count=excluded.row_count,byte_count=excluded.byte_count,
                data_status=excluded.data_status,inferred_fallback=excluded.inferred_fallback,imported_at=now()""", (table,checksum,actual,path.stat().st_size,data_status,fallback))
            print(f"Loaded {table}: {actual:,} rows in {elapsed:.1f}s", flush=True)
        expected = FILES[table][1] if data_status == "real" else actual
        item = {"table": table,"actual_rows":actual,"expected_rows":expected,"matches_expected":actual==expected,"sha256":checksum,"bytes":path.stat().st_size,"seconds":round(elapsed,2),"text_fallback":fallback}
        evidence.append(item)
        (output_dir / "raw_counts.json").write_text(json.dumps(evidence, indent=2))
        if actual != expected:
            raise ValueError(f"{table}: expected {expected:,} rows, found {actual:,}; incomplete or different dataset")
    for table,key in [("application_train","SK_ID_CURR"),("previous_application","SK_ID_PREV"),("bureau","SK_ID_BUREAU"),("bureau","SK_ID_CURR")]:
        connection.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON raw.{} ({})").format(sql.Identifier(f"ix_{table}_{key.lower()}"),sql.Identifier(table),sql.Identifier(key)))
    for table in FILES:
        connection.execute(sql.SQL("ANALYZE raw.{}").format(sql.Identifier(table)))
    (output_dir / "raw_schema.json").write_text(json.dumps(manifest, indent=2))
    return evidence
