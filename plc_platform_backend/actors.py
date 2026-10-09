import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware.asyncio import AsyncIO

from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.commons.logging_config import configure_logging

configure_logging()

broker = RedisBroker(url=get_configuration().redis_url)
broker.add_middleware(AsyncIO())

dramatiq.set_broker(broker)

from .runs.runs_actors import launch_run, finalize_listening_run

__all__ = ("launch_run", "finalize_listening_run")
