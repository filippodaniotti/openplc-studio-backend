from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from plc_platform_backend.plugins.plugins_models import (
    PluginInventory, PluginInventoryItem, PluginSource, PluginSourceInput,
    PluginSaveInput, PluginValidation,
)
from plc_platform_backend.plugins.plugins_service import (
    PluginsService, get_plugins_service, InvalidPluginSource, PluginConflictError,
)

router = APIRouter(prefix="/plugins", tags=["plugins"])


@contextmanager
def source_errors():
    try:
        yield
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PluginConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InvalidPluginSource as error:
        raise HTTPException(status_code=400, detail={
            "message": "Fix validation errors before saving.",
            "diagnostics": [item.model_dump() for item in error.diagnostics],
        }) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("/{filename}/source", response_model=PluginSource)
def get_plugin_source(
    filename: str,
    plugins_service: Annotated[PluginsService, Depends(get_plugins_service)],
) -> PluginSource:
    with source_errors():
        return plugins_service.get_source(filename)


@router.post("/{filename}/validate", response_model=PluginValidation)
def validate_plugin_source(
    filename: str,
    source: PluginSourceInput,
    plugins_service: Annotated[PluginsService, Depends(get_plugins_service)],
) -> PluginValidation:
    with source_errors():
        return plugins_service.validate_source(filename, source.content)


@router.put("/{filename}/source", response_model=PluginSource)
def save_plugin_source(
    filename: str,
    source: PluginSaveInput,
    plugins_service: Annotated[PluginsService, Depends(get_plugins_service)],
) -> PluginSource:
    with source_errors():
        return plugins_service.save_source(filename, source.content, source.revision)


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
