"""注册中心抽象基类。

新增的订阅钩子全部带默认实现，保证既有子类（以及用户自定义插件）不需要改动即可加载。
"""

from abc import abstractmethod
from typing import Any, List, Optional

from app.model.syncer_model import Instance, Registration, Service


class Discovery(object):
    """注册中心客户端基类。

    Attributes:
        _config: ``config.yaml`` 里的 discovery-server 配置模型。
        _capability: 由 :meth:`detect_version` 填充的版本能力对象。
        _version_evidence: 版本判定依据，供日志与 ``/-/versions`` 展示。
    """

    def __init__(self, config):
        self._config = config
        self._capability: Optional[Any] = None
        self._version_evidence: str = ""

    # -- 版本能力（子类可覆写/填充） ----------------------------------------
    @property
    def capability(self) -> Optional[Any]:
        """版本能力对象（``app.compat.Capability``），未探测时为 ``None``。"""
        return self._capability

    @property
    def version_evidence(self) -> str:
        """版本判定依据文本。"""
        return self._version_evidence

    def detect_version(self) -> Optional[Any]:
        """探测并设置版本能力，返回能力对象。默认不做探测。"""
        return self._capability

    # -- 订阅能力 -----------------------------------------------------------
    @property
    def supports_push(self) -> bool:
        """该注册中心是否具备原生服务端推送能力。默认 ``False``（走增量轮询）。"""
        return False

    def subscribe_instance(self, service_name: str, group: str, namespace: str, udp_port: int) -> None:
        """向注册中心登记一个服务的变更订阅（Nacos 1.x 用 UDP、2.x+ 用 gRPC）。

        默认无操作；不支持推送的注册中心由订阅层自动改用快照 diff 增量轮询。

        Args:
            service_name: 服务名。
            group: Nacos groupName。
            namespace: Nacos namespaceId。
            udp_port: 本地 UDP 接收端口（Nacos 1.x）。
        """

    # -- 原有抽象接口（保持签名不变） ---------------------------------------
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
