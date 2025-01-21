import logging

from fastapi import FastAPI

from plc_platform_backend.configuration.logging_utils import setup_logging

from .routers import modules

logger = logging.getLogger(__name__)

setup_logging()

app = FastAPI()

app.include_router(modules.router)
