"""Download the pinned public Kaggle mirror and extract only project inputs."""
from __future__ import annotations
import argparse
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path
from .database import PROJECT
from .generate_ddl import FILES, sha256_file

URL = "https://www.kaggle.com/api/v1/datasets/download/megancrenshaw/home-credit-default-risk?datasetVersionNumber=2"

def download(data_dir: Path = PROJECT / "data/raw") -> dict:
    data_dir.mkdir(parents=True,exist_ok=True)
    archive = data_dir.parent / "home-credit-default-risk.zip"
    if not archive.exists():
        temporary = archive.with_suffix(".zip.part")
        print("Downloading Home Credit public dataset mirror (about 688 MiB compressed)",flush=True)
        with urllib.request.urlopen(URL,timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response,output,16*1024*1024)
        if not zipfile.is_zipfile(temporary):
            raise ValueError("Download is not a CSV ZIP archive; check Kaggle access/network")
        temporary.replace(archive)
    required={value[0] for value in FILES.values()} | {"HomeCredit_columns_description.csv"}
    extracted={}
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            name=Path(info.filename).name
            if name not in required or name in extracted:
                continue
            target=data_dir / name
            if not target.exists() or target.stat().st_size != info.file_size:
                with zipped.open(info) as source,target.open("wb") as output:
                    shutil.copyfileobj(source,output,8*1024*1024)
            extracted[name]={"bytes":target.stat().st_size,"sha256":sha256_file(target)}
    missing=required-set(extracted)
    if missing:
        raise ValueError(f"Archive is missing files: {sorted(missing)}")
    source={"official_source":"https://www.kaggle.com/competitions/home-credit-default-risk/data",
        "download_source":"https://www.kaggle.com/datasets/megancrenshaw/home-credit-default-risk",
        "download_api":URL,"dataset_id":2602785,"dataset_version":2,"archive_sha256":sha256_file(archive),
        "data_status":"real","selected_files":extracted,"excluded":"application_test.csv and sample_submission.csv",
        "access":"Public Kaggle mirror, original competition provenance retained; no credentials used"}
    (data_dir.parent / "source.json").write_text(json.dumps(source,indent=2))
    return source

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",type=Path,default=PROJECT / "data/raw")
    download(parser.parse_args().data_dir)
