import itertools
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import List, Dict

import httpx
from db_libs.sqla_lib import SqlaReflectHelper
from httpx import TimeoutException
from pydantic import BaseModel, Field
from pydantic import model_validator, AliasChoices
from sqlalchemy import Column, Integer, String, Boolean, and_, delete, text
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.ext.declarative import declarative_base

from app.model.config import HealthCheckType
from core.lib.logger import for_model
from core.lib.util import http_status_in_array

logger = for_model(__name__)

Base = declarative_base()

SQL_UPDATE_INSTANCES = text("""
update instances
set successes= min(case when :failures + :timeouts > 0 then 0 else successes + :successes end, 256),
    failures= min(case when :successes > 0 then 0 else failures + :failures end, 256),
    timeouts= min(case when :successes > 0 then 0 else timeouts + :timeouts end, 256),
    status= ifnull(:status, 'unknown'),
    last_time=:last_time
where id = :id
""")

SQL_SELECT_INSTANCES = text("""
select *
from instances
where target_id = :target_id
  and service = :service
order by iif(status = 'unhealthy', 1, 0) asc, failures + timeouts asc, successes desc
limit ifnull(:skip, 0),-1
""")


class DiscoveryInstance(Base):
    __tablename__ = 'instances'
    __table_args__ = {'extend_existing': True}

    id = Column(String, primary_key=True)
    target_id = Column(String)
    service = Column(String)
    instance = Column(String)
    successes = Column(Integer, default=0)
    failures = Column(Integer, default=0)
    timeouts = Column(Integer, default=0)
    status = Column(String, default='unknown')
    create_time = Column(DATETIME)
    last_time = Column(DATETIME)

    def __init__(self, item: {}):
        self.id = item.get('id', None)
        self.target_id = item.get('target_id', None)
        self.service = item.get('service', None)
        self.instance = item.get('instance', None)
        self.successes = item.get('successes', 0)
        self.failures = item.get('failures', 0)
        self.timeouts = item.get('timeouts', 0)
        self.status = item.get('status', 'unknown')
        self.create_time = item.get('create_time', None)

    def __getitem__(self, field):
        return self.__dict__.get(field)

    def __setitem__(self, k, v):
        self.k = v

    def to_dict(self):
        return DiscoveryInstance(dict([(k, getattr(self, k)) for k in self.__dict__.keys() if not k.startswith("_")]))

    def to_dict_item(self):
        return dict([(k, getattr(self, k)) for k in self.__dict__.keys() if not k.startswith("_")])

    def get_instances_by_target_id(self, target_id: str, sqla_helper: SqlaReflectHelper) -> List['DiscoveryInstance']:
        with sqla_helper.session as ss:
            instances = ss.query(DiscoveryInstance).filter(and_(DiscoveryInstance.target_id == target_id)).all()
            if instances:
                return [row.to_dict() for row in instances]
            else:
                return []

    def health_check(self, healthcheck: dict, sqla_helper: SqlaReflectHelper):
        success = False
        params = {'id': self.id, "successes": 0, "failures": 0, "timeouts": 0, "status": self.status}
        schema = ""
        try:
            if HealthCheckType.HTTP.value == healthcheck.get("type"):
                schema = "http://"
            elif HealthCheckType.HTTPS.value == healthcheck.get("type"):
                schema = "https://"
            assert len(schema) > 0, f"暂时不支持 {healthcheck.get('type')} 协议"
            resp = httpx.request(method=healthcheck.get("method", "GET").upper(),
                                 url=f"{schema}{self.instance}{healthcheck.get('uri')}",
                                 timeout=healthcheck.get("timeout-sec", 30))
            if http_status_in_array(resp.status_code, healthcheck.get("healthy", {}).get("http_statuses", [])):
                params['successes'] = 1
                success = True
            elif http_status_in_array(resp.status_code, healthcheck.get("unhealthy", {}).get("http_statuses", [])):
                params['failures'] = 1
        except TimeoutException as e:
            params['timeouts'] = 1
            logger.warning(
                f"健康检查 {self.target_id} {self.service} {schema}{self.instance}{healthcheck.get('uri')} 超时, {e.args}")
        except Exception as e:
            params['failures'] = 1
            logger.warning(
                f"健康检查 {self.target_id} {self.service} {schema}{self.instance}{healthcheck.get('uri')} 失败, {e.args}")
        params['last_time'] = datetime.now()
        if success:
            if self.successes + params['successes'] >= healthcheck.get("healthy", {}).get("successes", 1):
                params['status'] = "healthy"
        else:
            if self.failures + params['failures'] >= healthcheck.get("unhealthy", {}).get("failures", 1):
                params['status'] = "unhealthy"
            if self.timeouts + params['timeouts'] >= healthcheck.get("unhealthy", {}).get("timeouts", 1):
                params['status'] = "unhealthy"
        self.set_counts(params, sqla_helper)
        if not success:
            instances = self.get_target_service_all_instance(0, sqla_helper)
            logger.warning(
                f"健康检查 {self.target_id} {self.service} {schema}{self.instance}{healthcheck.get('uri')} , 实例状态: {json.dumps([d.to_dict_item() for d in instances])}")
        if params['status'] != self.status and healthcheck.get("alert", {}).get("url"):
            instances = self.get_target_service_all_instance(0, sqla_helper)
            keyfunc = lambda item: item['status']
            group_dict = {k: [t.to_dict_item() for t in list(v)] for k, v in
                          itertools.groupby(sorted(instances, key=keyfunc), keyfunc)}
            logger.info(
                f"{self.target_id} 下的服务: {self.service} 中的实例: {self.instance} 由 {self.status} 改为 {params['status']}")
            body = self.to_dict_item()
            if params['status'] == "unhealthy":
                body['successes'] = 0
                body['failures'] = params['failures'] + self.failures
                body['timeouts'] = params['timeouts'] + self.timeouts
            else:
                body['successes'] = params['successes'] + self.successes
                body['failures'] = 0
                body['timeouts'] = 0
            body['last_time'] = params['last_time']
            body['new_status'] = params['status']
            body['items'] = group_dict
            try:
                resp = httpx.request(method=healthcheck.get("alert", {}).get("method", "GET").upper(), timeout=10,
                                     url=healthcheck.get("alert", {}).get("url"), params={"body": json.dumps(body)})
                logger.info(
                    f"健康检查状态变更通知 参数为: {json.dumps(body)} {healthcheck.get('alert', {})} 结果为: {resp.text}, status_code: {resp.status_code}, headers: {resp.headers}")
            except Exception as e:
                logger.error(f"健康检查状态变更通知 {healthcheck.get('alert', {})} 报错 {e.args}")

    def save_or_update(self, discovery_instances: ['Instance'], sqla_helper: SqlaReflectHelper):
        with sqla_helper.session as ss:
            instances = ss.query(DiscoveryInstance).filter(and_(DiscoveryInstance.target_id == self.target_id,
                                                                DiscoveryInstance.service == self.service)).all()
            if len(discovery_instances) == 0:
                if len(instances) > 0:
                    delete_instances = [d.instance for d in instances]
                    logger.info(f"删除无效的实例: {json.dumps(delete_instances)}")
                    sql = delete(DiscoveryInstance).where(DiscoveryInstance.instance.in_(delete_instances))
                    ss.execute(sql)
                    ss.commit()
                return instances
            tmps = [f"{d.ip}:{d.port}" for d in discovery_instances if d.enabled]
            ins = [d.instance for d in instances]

            # 删除无效的
            delete_instances = [d for d in ins if d not in tmps]
            if delete_instances:
                logger.info(f"删除无效的实例: {json.dumps(delete_instances)}")
                sql = delete(DiscoveryInstance).where(DiscoveryInstance.instance.in_(delete_instances))
                ss.execute(sql)
                ss.commit()

            # 增加新增的
            save_instances = [DiscoveryInstance(
                {"id": self.id or str(uuid.uuid4()), "target_id": self.target_id, "service": self.service,
                 "instance": d, "create_time": datetime.now()}) for d in tmps if d not in ins]
            if save_instances:
                logger.info(f"新增的实例: {json.dumps([d.to_dict_item() for d in save_instances])}")
                ss.add_all(save_instances)
                ss.commit()
            return instances

    def set_counts(self, params: {}, sqla_helper: SqlaReflectHelper):
        with sqla_helper.session as ss:
            ss.execute(SQL_UPDATE_INSTANCES, params)
            ss.commit()

    def delete_by_instances(self, instances: [], sqla_helper: SqlaReflectHelper):
        if len(instances) == 0:
            return
        with sqla_helper.session as ss:
            logger.info(f"删除无效的实例: {json.dumps(instances)}")
            sql = delete(DiscoveryInstance).where(DiscoveryInstance.instance.in_(instances))
            ss.execute(sql)
            ss.commit()

    def get_target_service_all_instance(self, skip: 0, sqla_helper: SqlaReflectHelper) -> List['DiscoveryInstance']:
        with sqla_helper.session as ss:
            instances = ss.execute(SQL_SELECT_INSTANCES,
                                   {"target_id": self.target_id, "service": self.service, "skip": skip}).all()
            return [DiscoveryInstance(row._asdict()) for row in instances]

    @staticmethod
    def create_table_if_not_exists(sqla_helper: SqlaReflectHelper):
        Base.metadata.drop_all(sqla_helper.engine)
        Base.metadata.create_all(sqla_helper.engine)

    @staticmethod
    def query_all(sqla_helper: SqlaReflectHelper) -> List['DiscoveryInstance']:
        with sqla_helper.session as ss:
            ss.expire_on_commit = False
            result = ss.query(DiscoveryInstance).all()
            ss.commit()
            return result

    @staticmethod
    def clear_all(sqla_helper: SqlaReflectHelper):
        with sqla_helper.session as ss:
            ss.query(DiscoveryInstance).delete()
            ss.commit()


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
