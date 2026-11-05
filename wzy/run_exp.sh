#!/usr/bin/env bash
# Run one experiment config over the full split, re-running failed questions.
# usage: run_exp.sh <worktree_dir> <config_path> <log_file>
# run_batch.py --resume skips questions that already have status=ok,
# so each extra pass only retries questions that failed (e.g. network errors).
set -u
cd "$1" || exit 1
PY=../.venv/Scripts/python.exe
export PYTHONUTF8=1
for pass in 1 2 3 4 5 6; do
  echo "=== pass $pass $(date '+%F %T')" >> "$3"
  if "$PY" scripts/run_batch.py --config "$2" --resume >> "$3" 2>&1; then
    echo "=== ALL_OK after pass $pass $(date '+%F %T')" >> "$3"
    exit 0
  fi
  sleep 20
done
echo "=== GAVE_UP $(date '+%F %T')" >> "$3"
exit 1
