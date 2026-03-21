from __future__ import annotations

from functools import lru_cache
from typing import Optional

from fastapi import WebSocket


@lru_cache
def get_ws_connection_manager() -> WsConnectionManager:
    _ws_conn_manager = WsConnectionManager()
    return _ws_conn_manager


class WsConnectionManager:
    def __init__(self):
        self.active: Optional[WebSocket] = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active = websocket

    def disconnect(self):
        self.active = None

    async def send(self, payload: dict):
        if self.active:
            try:
                await self.active.send_json(payload)
            except Exception:
                self.disconnect()
