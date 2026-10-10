from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from plc_platform_backend.plugins.plugins_models import PluginInventory, PluginInventoryItem
from plc_platform_backend.plugins.plugins_service import PluginsService, get_plugins_service

router = APIRouter(prefix="/plugins", tags=["plugins"])


@router.post("", response_model=PluginInventoryItem, status_code=201)
async def upload_plugin(
    file: Annotated[UploadFile, File()],
    plugins_service: Annotated[PluginsService, Depends(get_plugins_service)],
) -> PluginInventoryItem:
    try:
        content = await file.read(5 * 1024 * 1024 + 1)
        return plugins_service.upload(file.filename or "", content)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        await file.close()


@router.get("", response_model=PluginInventory)
def get_plugins(
    plugins_service: Annotated[PluginsService, Depends(get_plugins_service)],
) -> PluginInventory:
    try:
        return plugins_service.scan()
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
