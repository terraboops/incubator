"""WebSocket endpoint for live event streaming."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()

_clients: set[WebSocket] = set()


async def broadcast_event(event_type: str, data: dict) -> None:
    """Broadcast an event to all connected WebSocket clients."""
    global _clients
    message = json.dumps(
        {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
    )
    disconnected = set()
    for client in _clients:
        try:
            await client.send_text(message)
        except Exception:
            disconnected.add(client)
    _clients -= disconnected


async def broadcast_phase_transition(idea_id: str, from_phase: str, to_phase: str) -> None:
    await broadcast_event(
        "phase_transition",
        {
            "idea_id": idea_id,
            "from_phase": from_phase,
            "to_phase": to_phase,
        },
    )


async def broadcast_agent_status(idea_id: str, agent: str, status: str, detail: str = "") -> None:
    await broadcast_event(
        "agent_status",
        {
            "idea_id": idea_id,
            "agent": agent,
            "status": status,
            "detail": detail,
        },
    )


async def broadcast_activity(idea_id: str, message: str, kind: str = "info") -> None:
    await broadcast_event(
        "activity",
        {
            "idea_id": idea_id,
            "message": message,
            "kind": kind,
        },
    )


async def broadcast_idea_update(idea_id: str, status: dict) -> None:
    """Broadcast a projection update so browsers can patch the DOM."""
    await broadcast_event(
        "idea_update",
        {
            "idea_id": idea_id,
            "phase": status.get("phase", ""),
            "title": status.get("title", ""),
            "total_cost_usd": status.get("total_cost_usd", 0),
            "priority_score": status.get("priority_score", 0),
            "iteration_count": status.get("iteration_count", 0),
            "needs_human_review": status.get("needs_human_review", False),
        },
    )


@router.websocket("/ws/events")
async def events_websocket(websocket: WebSocket):
    await websocket.accept()
    _clients.add(websocket)
    logger.info("WebSocket client connected (%d total)", len(_clients))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _clients.discard(websocket)
