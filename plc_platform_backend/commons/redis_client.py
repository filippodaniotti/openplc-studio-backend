from functools import lru_cache

import redis.asyncio as aioredis

from plc_platform_backend.commons.configuration.configuration import get_configuration


@lru_cache
def get_redis_client() -> aioredis.Redis:
    config = get_configuration()
    return aioredis.from_url(config.redis_url)
