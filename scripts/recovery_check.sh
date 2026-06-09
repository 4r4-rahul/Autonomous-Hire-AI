#!/bin/zsh
# One-shot LinkedIn recovery check (scheduled ~8:52 PM via launchd). Wrapped so it has the
# venv + PYTHONPATH. Writes data/recovery_status.md.
cd /Users/rahul/Workspace/Job_Finder || exit 1
export PYTHONPATH=/Users/rahul/Workspace/Job_Finder
/Users/rahul/Workspace/Job_Finder/.venv/bin/python scripts/recovery_check.py >> data/recovery_check.log 2>&1
