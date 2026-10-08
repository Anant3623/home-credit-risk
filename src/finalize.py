"""Verify notebook execution evidence, publish the report, and package results."""
from __future__ import annotations
import json
import re
from pathlib import Path
import nbformat
from .database import PROJECT
from .package import package
from .report import write_report

def finalize() -> Path:
    output=PROJECT / "runs/real"
    path=output / "run_manifest.json"
    manifest=json.loads(path.read_text())
    if manifest["status"]!="complete":
        raise ValueError("The real-data pipeline has not completed")
    checks=json.loads((output / "acceptance_checks.json").read_text())
    if not checks or not all(c["passed"] for c in checks):
        raise ValueError("Warehouse acceptance checks did not all pass")
    notebooks=[]
    for file in sorted((output / "notebooks").glob("*.ipynb")):
        document=nbformat.read(file,as_version=4)
        code=[c for c in document.cells if c.cell_type=="code"]
        errors=[o for cell in code for o in cell.outputs if o.output_type=="error"]
        if not code or errors or any(c.execution_count is None for c in code):
            raise ValueError(f"Notebook execution incomplete: {file}")
        notebooks.append({"notebook":file.name,"code_cells":len(code),"executed_cells":len(code),"errors":0})
    if len(notebooks)!=4:
        raise ValueError("Expected four executed notebooks")
    (output / "notebook_checks.json").write_text(json.dumps(notebooks,indent=2))
    manifest["acceptance_checks_passed"]=len(checks)
    manifest["notebook_execution_checks"]=notebooks
    test_log=(PROJECT / "runs/synthetic/tests.log").read_text()
    summary=re.search(r"(\d+) passed, (\d+) subtests passed",test_log)
    if not summary:
        raise ValueError("Final test suite has no passing summary")
    manifest["automated_tests"]={"tests_passed":int(summary[1]),"subtests_passed":int(summary[2]),"database":"homecredit_synthetic"}
    path.write_text(json.dumps(manifest,indent=2))
    write_report(output)
    return package()

if __name__=="__main__":
    print(finalize())
