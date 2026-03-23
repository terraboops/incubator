"""Human decision endpoints (parallel to Telegram)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

# In-memory store for pending decisions (in production, use persistent store)
_pending_decisions: dict[str, dict] = {}


@router.get("/pending")
async def list_pending():
    return list(_pending_decisions.values())


@router.post("/{decision_id}")
async def respond_to_decision(decision_id: str, response: str):
    decision = _pending_decisions.pop(decision_id, None)
    if not decision:
        return {"error": "Decision not found or already resolved"}
    # The decision future would be resolved here
    return {"status": "resolved", "response": response}
