import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import WebSocketDisconnect
from plc_platform_backend.runs.runs_ws import runs_websocket


def test_disconnect_cancels_idle_redis_listener_and_closes_connections():
    async def idle_listener():
        await asyncio.Event().wait()
        yield {}
    pubsub = SimpleNamespace(subscribe=AsyncMock(), unsubscribe=AsyncMock(), aclose=AsyncMock(), listen=idle_listener)
    redis = SimpleNamespace(pubsub=lambda: pubsub, aclose=AsyncMock())
    websocket = SimpleNamespace(accept=AsyncMock(), receive_text=AsyncMock(side_effect=['{"type":"run.subscribe","run_id":"run"}', WebSocketDisconnect()]), send_text=AsyncMock())
    with patch('plc_platform_backend.runs.runs_ws.aioredis.from_url', return_value=redis), patch('plc_platform_backend.runs.runs_ws.get_configuration', return_value=SimpleNamespace(redis_url='redis://test')):
        asyncio.run(asyncio.wait_for(runs_websocket(websocket), timeout=1))
    pubsub.aclose.assert_awaited_once()
    redis.aclose.assert_awaited_once()
    websocket.send_text.assert_not_called()
