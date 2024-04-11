from enum import Enum

from pydantic import Extra
from pydantic_settings import BaseSettings


class ContentType(str, Enum):
    yaml = 'yaml'
    json = 'json'


class Settings(BaseSettings):
    ENV: str
    TITLE: str
    DESCRIPTION: str
    VERSION: str
    # 是否单元测试
    UNIT_TEST: str = 'False'

    class Config:
        case_sensitive = True  # 大小写敏感
        extra: Extra = Extra.ignore

    def get_config(self):
        return self.dict()
