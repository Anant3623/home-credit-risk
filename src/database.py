"""Private local PostgreSQL setup and database IO; no passwords in logs."""
from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path
import psycopg
from psycopg import sql

PROJECT = Path(__file__).resolve().parents[1]

def pg_binary(name: str) -> str:
    if os.environ.get("PG_BIN"):
        candidate = Path(os.environ["PG_BIN"]) / name
        if candidate.is_file():
            return str(candidate)
    located = shutil.which(name)
    if located:
        return located
    for base in [Path("/Library/PostgreSQL/18/bin"), Path("/opt/homebrew/opt/postgresql@18/bin"), Path("/opt/homebrew/opt/postgresql@17/bin"), Path("/usr/lib/postgresql/16/bin")]:
        if (base / name).is_file():
            return str(base / name)
    raise FileNotFoundError(f"PostgreSQL binary {name} not found; set PG_BIN to its bin directory")

def local_dsn(database: str = "homecredit_real", start: bool = True) -> str:
    if database not in {"homecredit_real", "homecredit_synthetic"}:
        raise ValueError("Only dedicated project database names can be created automatically")
    runtime = PROJECT / ".runtime"
    runtime.mkdir(exist_ok=True)
    data = runtime / "pgdata"
    # Short socket path stays below PostgreSQL's Unix path length limit.
    socket = Path("/private/tmp/homecredit-pg-socket") if Path("/private/tmp").exists() else Path("/tmp/homecredit-pg-socket")
    socket.mkdir(mode=0o700, exist_ok=True)
    socket.chmod(0o700)
    port = int(os.environ.get("HOME_CREDIT_PG_PORT", "55438"))
    admin = f"host={socket} port={port} user=homecredit dbname=postgres"
    if start:
        if not (data / "PG_VERSION").exists():
            subprocess.run([pg_binary("initdb"), "-D", str(data), "-U", "homecredit", "--auth-local=trust", "--auth-host=scram-sha-256", "--no-locale", "--encoding=UTF8"], check=True)
        status = subprocess.run([pg_binary("pg_ctl"), "-D", str(data), "status"], capture_output=True)
        if status.returncode:
            subprocess.run([pg_binary("pg_ctl"), "-D", str(data), "-l", str(runtime / "postgres.log"), "-o", f"-h '' -p {port} -k {socket}", "start"], check=True)
        with psycopg.connect(admin, autocommit=True) as connection:
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    return f"host={socket} port={port} user=homecredit dbname={database}"

def connect(dsn: str):
    connection = psycopg.connect(dsn, autocommit=True)
    connection.execute("SET work_mem='128MB'")
    connection.execute("SET statement_timeout=0")
    connection.execute("SET lock_timeout='30s'")
    connection.execute("SET max_parallel_workers_per_gather=2")
    return connection

def export_query(connection, query: str, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle, connection.cursor().copy(f"COPY ({query}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)") as copy:
        for data in copy:
            handle.write(data)
    return path.stat().st_size
