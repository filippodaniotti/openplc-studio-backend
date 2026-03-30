import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from plc_platform_backend.commons.redis_client import get_redis_client

router = APIRouter(
    prefix="/ws",
    tags=["websocket"],
    dependencies=[],
    responses={404: {"description": "Not found"}},
)


@router.websocket("")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    rc = get_redis_client()
    async with rc.pubsub() as pubsub:
        await pubsub.subscribe("run:progress")
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True)

                if not message:
                    await asyncio.sleep(0.5)
                    continue

                data: bytes = message.get("data")
                await websocket.send_json({"msg": data.decode("ascii")})
        except WebSocketDisconnect:
            websocket.close()
