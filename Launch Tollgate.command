#!/bin/zsh
set -e
task_root="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$task_root"
if [[ -x "$task_root/.venv/bin/python" ]]; then
  task_python="$task_root/.venv/bin/python"
else
  echo "Create a Python environment first; see README.md."
  read -r "task_reply?Press Return to close."
  exit 1
fi
exec "$task_python" -B -m tollgate.demo_server "$@"
