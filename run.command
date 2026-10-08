#!/bin/zsh
set -eu
task_project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$task_project_dir"
if [[ -x ../.venv-home-credit/bin/python ]]; then
  task_python=../.venv-home-credit/bin/python
elif [[ -x .venv/bin/python ]]; then
  task_python=.venv/bin/python
else
  python3 -m venv .venv
  task_python=.venv/bin/python
  "$task_python" -m pip install -r requirements.txt
fi
"$task_python" -m src.download_data
"$task_python" -u -m src.run_pipeline "$@"
