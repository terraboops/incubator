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
    message = json.dumps({
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    })
    disconnected = set()
    for client in _clients:
        try:
            await client.send_text(message)
        except Exception:
            disconnected.add(client)
    _clients -= disconnected


async def broadcast_phase_transition(idea_id: str, from_phase: str, to_phase: str) -> None:
    await broadcast_event("phase_transition", {
        "idea_id": idea_id,
        "from_phase": from_phase,
        "to_phase": to_phase,
    })


async def broadcast_agent_status(idea_id: str, agent: str, status: str, detail: str = "") -> None:
    await broadcast_event("agent_status", {
        "idea_id": idea_id,
        "agent": agent,
        "status": status,
        "detail": detail,
    })


async def broadcast_activity(idea_id: str, message: str, kind: str = "info") -> None:
    await broadcast_event("activity", {
        "idea_id": idea_id,
        "message": message,
        "kind": kind,
    })


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
