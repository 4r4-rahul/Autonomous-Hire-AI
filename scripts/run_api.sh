#!/bin/zsh
# Runs the AutonomousHire FastAPI backend on :8000 — the source the web UI reads and the browser
# extension's autofill resolver calls. Kept alive + auto-restarted by launchd (com.autonomoushire.api),
# mirroring the scheduler agent, so the backend self-heals and starts at login instead of being a
# manual terminal launch. Uses the venv python (which has asyncpg — the framework python does NOT).
cd /Users/rahul/Workspace/Job_Finder || exit 1
export PYTHONPATH=/Users/rahul/Workspace/Job_Finder
exec /Users/rahul/Workspace/Job_Finder/.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
