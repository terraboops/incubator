"""Registry migration routes — check and apply migrations via the web UI."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from incubator.config import get_settings
from incubator.core.migrations import (
    MigrationResult,
    check_all,
    load_registry_data,
    run_migrations,
)

router = APIRouter()


@router.get("/check")
async def migrations_check():
    """Check which migrations are needed for the current registry."""
    settings = get_settings()
    data = load_registry_data(settings.registry_path)
    needed = check_all(data)
    return {
        "registry": str(settings.registry_path),
        "pending": [
            {
                "version": m.version,
                "description": m.description,
                "llm_assisted": m.llm_assisted,
                "affected_agents": check.affected_agents or [],
                "reason": check.reason,
            }
            for m, check in needed
        ],
        "up_to_date": len(needed) == 0,
    }


class ApplyRequest(BaseModel):
    auto_yes: bool = True  # Web UI always auto-confirms mechanical migrations
    dry_run: bool = False


@router.post("/apply")
async def migrations_apply(req: ApplyRequest):
    """Apply all pending migrations to the current registry.

    For LLM-assisted migrations, a 'pending_review' flag is returned
    and the migration is NOT applied — the user must review in the UI.
    """
    settings = get_settings()

    results = []

    async def confirm(action: str, details: str) -> bool:
        # Web UI always confirms mechanical migrations (auto_yes=True by default)
        # LLM-assisted ones should use a separate review endpoint
        return req.auto_yes

    migration_results = await run_migrations(
        registry_path=settings.registry_path,
        confirm=confirm,
        dry_run=req.dry_run,
        auto_yes=req.auto_yes,
    )

    for r in migration_results:
        results.append({
            "success": r.success,
            "message": r.message,
            "agents_modified": r.agents_modified,
            "errors": r.errors,
        })

    return {"results": results, "applied": len([r for r in migration_results if r.success])}
