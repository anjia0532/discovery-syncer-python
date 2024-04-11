from abc import abstractmethod
from typing import List

from app.model.syncer_model import Instance, Registration, Service


class Discovery(object):
    def __init__(self, config):
        self._config = config

    @abstractmethod
    def get_all_service(self, config: dict, enabled_only: bool = True) -> List[Service]:
        """
        获取所有服务名称
        @param enabled_only: 只返回启用的实例
        @param config: 额外参数，比如groupName,namespaceId等
        @return:  通用服务列表
        """
        pass

    @abstractmethod
    def get_service_all_instances(self, service_name: str, ext_data: dict, enabled_only: bool = True) -> tuple[
        List[Instance], int]:
        """
        获取指定服务的所有实例
        @param enabled_only:  只返回启用的实例
        @param service_name: 服务名称
        @param ext_data: 额外参数，比如groupName,namespaceId等
        @return: 通用实例列表
        """
        pass

    @abstractmethod
    def modify_registration(self, registration: Registration, instances: List[Instance]):
        """
        修改服务注册信息
        @param registration:  注册信息
        @param instances:  实例列表
        @return:  None
        """
        pass
