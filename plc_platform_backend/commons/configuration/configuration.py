from functools import lru_cache

from pydantic_settings import BaseSettings


@lru_cache
def get_configuration():
    return Configuration()


class Configuration(BaseSettings):
    mongo_initdb_root_username: str
    mongo_initdb_root_password: str
    mongo_host: str = "mongo"
    mongo_port: int = 27017
    mongo_database: str = "plc-testbench"
    plc_root_folder: str
    plugins_directory: str
    redis_url: str

    def validate(self):
        for field_name in [
            "mongo_initdb_root_username",
            "mongo_initdb_root_password",
            "mongo_host",
            "mongo_port",
            "mongo_database",
            "plc_root_folder",
            "plugins_directory",
            "redis_url",
        ]:
            value = getattr(self, field_name)
            if not value or (isinstance(value, str) and value.strip() == ""):
                raise ValueError(f"Configuration field '{field_name}' cannot be blank.")
