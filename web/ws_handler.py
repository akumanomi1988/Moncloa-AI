import json
import logging
from typing import Any
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)
        logger.info(f"WS client connected ({len(self.connections)} total)")

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)
        logger.info(f"WS client disconnected ({len(self.connections)} remaining)")

    async def broadcast(self, data: dict[str, Any]):
        msg = json.dumps(data, ensure_ascii=False, default=str)
        stale = []
        for ws in self.connections:
            try:
                await ws.send_text(msg)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)

    def make_event_handler(self):
        async def handler(event: dict):
            await self.broadcast(event)
        return handler
