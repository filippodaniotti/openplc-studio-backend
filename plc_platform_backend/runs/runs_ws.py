import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from plc_platform_backend.commons.configuration.configuration import get_configuration


router = APIRouter()

RUN_COMPLETITION_CHANNEL = "run.complete"


@router.websocket("/ws/runs")
async def runs_websocket(websocket: WebSocket) -> None:
    # Accept the WebSocket connection

    await websocket.accept()
    redis_client = aioredis.from_url(get_configuration().redis_url)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(RUN_COMPLETITION_CHANNEL)

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"].decode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(RUN_COMPLETITION_CHANNEL)
        await pubsub.close()
