"""
AutonomousHire FastAPI backend.
Run: uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.applications import router as apps_router
from api.autofill import router as autofill_router
from api.insights import router as insights_router
from api.jobs import router as jobs_router
from api.pipeline import router as pipeline_router

app = FastAPI(
    title="AutonomousHire API",
    description="AI-powered job search pipeline",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pipeline_router, prefix="/api/v1")
app.include_router(jobs_router,     prefix="/api/v1")
app.include_router(apps_router,     prefix="/api/v1")
app.include_router(insights_router, prefix="/api/v1")
app.include_router(autofill_router, prefix="/api/v1")

# Serve screenshots so the frontend can display submission evidence
_SCREENSHOT_DIR = Path(__file__).parent.parent / "data" / "screenshots"
_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(_SCREENSHOT_DIR)), name="screenshots")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/v1/stats")
async def stats():
    from api.jobs import get_stats
    return await get_stats()


@app.get("/api/v1/health/system")
def health_system():
    """Observability — Anthropic credits, Gmail, LinkedIn, datastore sync, freshness."""
    from agents.monitoring.health import system_health
    return system_health(probe_llm=False)  # cheap for UI polling; runner --health probes the LLM


@app.get("/api/v1/digest")
def digest():
    """Daily digest — pipeline funnel + today's activity + health summary."""
    from agents.monitoring.health import build_digest
    return build_digest(write=False)
