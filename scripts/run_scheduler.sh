#!/bin/zsh
# Runs the AutonomousHire pipeline scheduler (hourly LinkedIn scan → tailor → park),
# wrapped in `caffeinate` so the Mac doesn't idle-sleep while plugged in.
# AUTONOMOUSHIRE_AUTO_SUBMIT is intentionally UNSET → auto-submit OFF (you review + apply
# via the extension). Set it to 1 here only if you want hands-off submitting later.
cd /Users/rahul/Workspace/Job_Finder || exit 1
export PYTHONPATH=/Users/rahul/Workspace/Job_Finder
# -i no idle sleep, -s no system sleep (effective on AC power)
exec /usr/bin/caffeinate -i -s /Users/rahul/Workspace/Job_Finder/.venv/bin/python -m orchestration.scheduler
