"""Package the reviewable project and actual results without input data or DB files."""
from __future__ import annotations
import json
import zipfile
from pathlib import Path
from .database import PROJECT

def package(destination: Path | None=None) -> Path:
    path=destination or PROJECT.parent / "Home_Credit_Risk_Project.zip"
    real=PROJECT / "runs/real/run_manifest.json"
    if not real.exists() or json.loads(real.read_text())["status"]!="complete":
        raise ValueError("Cannot package incomplete real-data results")
    files=[]
    directories=["src","sql","tests","docs","notebooks","runs/real"]
    for directory in directories:
        files.extend(p for p in (PROJECT / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".ipynb_checkpoints" not in p.parts)
    files.extend(PROJECT / name for name in ["README.md","requirements.txt","requirements.lock.txt","run.command",".gitignore","data/source.json"])
    # Keep synthetic test evidence, but generate fixtures on demand rather than
    # redistributing a second set of example applicant data in the main archive.
    files.extend(p for p in [PROJECT / "runs/synthetic/tests.log",PROJECT / "runs/synthetic/run_manifest.json"] if p.exists())
    with zipfile.ZipFile(path,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zipped:
        for item in sorted(set(files)):
            zipped.write(item,Path("home-credit-risk") / item.relative_to(PROJECT))
    with zipfile.ZipFile(path) as zipped:
        bad=zipped.testzip()
        if bad:
            raise ValueError(f"Archive CRC error in {bad}")
    return path

if __name__=="__main__":
    print(package())
