import asyncio
import json

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from plc_platform_backend.commons.configuration.configuration import get_configuration

router = APIRouter()

RUN_COMPLETION_CHANNEL = "run.complete"
RUN_PROGRESS_CHANNEL = "run.progress"


@router.websocket("/ws/runs")
async def runs_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    redis_client = aioredis.from_url(get_configuration().redis_url)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(RUN_COMPLETION_CHANNEL, RUN_PROGRESS_CHANNEL)
    run_id: str | None = None

    async def receive_run_id():
        nonlocal run_id
        try:
            data = await websocket.receive_text()
            payload = json.loads(data)
            run_id = payload.get("run_id")
        except Exception:
            pass

    async def forward_messages():
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = json.loads(message["data"].decode("utf-8"))
                    if run_id is None or data.get("run_id") == run_id:
                        await websocket.send_text(json.dumps(data))
        except WebSocketDisconnect:
            pass

    try:
        await asyncio.gather(receive_run_id(), forward_messages())
    finally:
        await pubsub.unsubscribe(RUN_COMPLETION_CHANNEL, RUN_PROGRESS_CHANNEL)
        await pubsub.close()
