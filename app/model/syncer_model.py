from datetime import datetime
from enum import Enum
from typing import List, Dict

from db_libs.sqla_lib import SqlaReflectHelper
from pydantic import BaseModel, Field
from pydantic import model_validator, AliasChoices
from sqlalchemy import Column, Integer, String, Boolean
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Jobs(Base):
    __tablename__ = 'jobs'
    __table_args__ = {'extend_existing': True}

    target_id = Column(String, primary_key=True)
    description = Column(String)
    discovery = Column(String)
    gateway = Column(String)
    maximum_interval_sec = Column(Integer, default=-1)
    enabled = Column(Boolean)
    last_time = Column(DATETIME)

    def __init__(self, **items):
        for key in items:
            if hasattr(self, key):
                setattr(self, key, items[key])
            elif key == 'id':
                setattr(self, "target_id", items["id"])
            elif key == 'name':
                setattr(self, "description", items["name"])

    def save_or_update(self, sqla_helper: SqlaReflectHelper):
        with sqla_helper.session as ss:
            job = ss.query(Jobs).filter(Jobs.target_id == self.target_id).first()
            if job:
                job.description = self.description
                job.discovery = self.discovery
                job.gateway = self.gateway
                job.maximum_interval_sec = self.maximum_interval_sec
                job.last_time = datetime.now()
                ss.merge(job)
            else:
                self.last_time = None
                ss.add(self)
            ss.commit()

    @staticmethod
    def create_table_if_not_exists(sqla_helper: SqlaReflectHelper):
        Base.metadata.drop_all(sqla_helper.engine)
        Base.metadata.create_all(sqla_helper.engine)

    @staticmethod
    def query_all(sqla_helper: SqlaReflectHelper) -> List['Jobs']:
        with sqla_helper.session as ss:
            ss.expire_on_commit = False
            result = ss.query(Jobs).all()
            ss.commit()
            return result

    @staticmethod
    def clear_all(sqla_helper: SqlaReflectHelper):
        with sqla_helper.session as ss:
            ss.query(Jobs).delete()
            ss.commit()


class Instance(BaseModel):
    port: int
    ip: str
    weight: float = 1.0
    metadata: Dict[str, str] = {}
    enabled: bool = False
    change: bool = False
    ext: Dict[str, object] = {}

    def _replace(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        return self

    @model_validator(mode='after')
    def check_instance(self) -> 'Instance':
        assert len(self.ip) > 0, "host 必填, 服务实例ip地址"
        assert 0 < self.port <= 65535, "port 必填, 取值范围为 (0, 65535]"
        assert 0 < self.weight <= 100, "weight 必填, 取值范围为 (0, 100]"
        return self


class Service(BaseModel):
    name: str
    last_time: int = -1
    instances: List[Instance] = []

    @model_validator(mode='after')
    def check_service(self) -> 'Service':
        assert self.name is not None, "name 必填，服务名称"
        return self


class RegistrationType(Enum):
    IP = "IP"
    METADATA = "METADATA"


class RegistrationStatus(Enum):
    ORIGIN = "ORIGIN"
    UP = "UP"
    DOWN = "DOWN"


class Registration(BaseModel):
    """
    description: 注册中心注册实例修改参数
    """
    type: RegistrationType = Field(RegistrationType.METADATA, example="METADATA",
                                   description="基于注册中心元数据还是基于实例ip来查找，METADATA元数据，IP ip，默认 METADATA")
    regexp_str: str = Field(None, validation_alias=AliasChoices("regexp-str", "regexp_str", "regexpStr"),
                            description="匹配的查询条件，支持正则",
                            example="^oms.*$")
    metadata_key: str = Field(None, validation_alias=AliasChoices("metadata-key", "metadata_key", "metadataKey"),
                              example="name",
                              description="如果type==METADATA,则需指定元数据的key,如果是ip则不用填")
    status: RegistrationStatus = Field(RegistrationStatus.UP, example="UP",
                                       description="匹配到的将状态改成上线还是下线，UP上线，DOWN下线，ORIGIN保持不动，默认 UP")
    other_status: RegistrationStatus = Field(RegistrationStatus.ORIGIN, example="ORIGIN",
                                             validation_alias=AliasChoices("other-status", "other_status",
                                                                           "otherStatus"),
                                             description="其他没匹配的，状态是上线还是下线，UP上线，DOWN下线，ORIGIN保持不动，默认 ORIGIN")
    service_name: str = Field(None, validation_alias=AliasChoices("service-name", "service_name", "serviceName"),
                              description="检索哪个服务下的实例", example="example")
    ext_data: Dict[str, str] = Field({}, validation_alias=AliasChoices("ext-data", "ext_data", "extData"), example="{}",
                                     description="扩展数据，比如 nacos 的组id，命名空间等数据")

    @model_validator(mode='after')
    def check_registration(self) -> 'Registration':
        assert self.type is not None, "type 必填, 值为: METADATA 或者 IP"
        assert self.service_name is not None, "service_name 必填，服务名称"
        return self
