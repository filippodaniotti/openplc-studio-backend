import asyncio
import json

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from plc_platform_backend.commons.configuration.configuration import get_configuration

router = APIRouter()

RUN_COMPLETION_CHANNEL = "run.complete"
RUN_PROGRESS_CHANNEL = "run.progress"
RUN_STATE_CHANGE_CHANNEL = "run.state_change"


@router.websocket("/ws/runs")
async def runs_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    redis_client = aioredis.from_url(get_configuration().redis_url)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(
        RUN_COMPLETION_CHANNEL,
        RUN_PROGRESS_CHANNEL,
        RUN_STATE_CHANGE_CHANNEL,
    )
    run_id: str | None = None

    async def receive_run_id():
        nonlocal run_id
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("type") == "run.subscribe":
                    run_id = payload.get("run_id")
        except WebSocketDisconnect:
            return

    async def forward_messages():
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = json.loads(message["data"].decode("utf-8"))
                    if (
                        data.get("type") == RUN_STATE_CHANGE_CHANNEL
                        or run_id is None
                        or data.get("run_id") == run_id
                    ):
                        await websocket.send_text(json.dumps(data))
        except WebSocketDisconnect:
            pass
        except RuntimeError as error:
            # Uvicorn can report this instead of WebSocketDisconnect when a
            # send races a close; unrelated programming errors still propagate.
            if "Unexpected ASGI message 'websocket.send'" not in str(error):
                raise

    tasks = [asyncio.create_task(receive_run_id()), asyncio.create_task(forward_messages())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        # A disconnected receiver must not leave the Redis listener alive forever
        # (which also prevents Uvicorn hot reload/shutdown from finishing).
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pubsub.unsubscribe(
            RUN_COMPLETION_CHANNEL,
            RUN_PROGRESS_CHANNEL,
            RUN_STATE_CHANGE_CHANNEL,
        )
        await pubsub.aclose()
        await redis_client.aclose()
