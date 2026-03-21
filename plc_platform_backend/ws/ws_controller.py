from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from plc_platform_backend.ws import get_ws_connection_manager

router = APIRouter(
    prefix="/ws",
    tags=["websocket"],
    dependencies=[],
    responses={404: {"description": "Not found"}},
)


@router.websocket("")
async def ws_endpoint(websocket: WebSocket):
    manager = get_ws_connection_manager()
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.send(data)
    except WebSocketDisconnect:
        manager.disconnect()
