from abc import abstractmethod
from typing import Tuple, List

from app.model.syncer_model import Instance


class Gateway(object):
    def __init__(self, config):
        self._config = config

    @abstractmethod
    def get_service_all_instances(self, target: dict, upstream_name: str = None) -> List[Instance]:
        pass

    @abstractmethod
    def sync_instances(self, target: dict, upstream_name: str, diff_ins: list, instances: list):
        pass

    @abstractmethod
    def fetch_admin_api_to_file(self) -> Tuple[str, str]:
        pass

    @abstractmethod
    async def migrate_to(self, target_gateway: 'Gateway'):
        pass

    def get_upstream_name(self, target: dict, upstream_name: str) -> str:
        """
        如果target中有upstream_prefix,则使用upstream_prefix-upstream_name,否则直接使用upstream_name
        @param target: target配置
        @param upstream_name:  upstream名称
        @return:  upstream名称
        """
        return '-'.join(
            [item for item in [target.get("upstream_prefix", None), upstream_name] if item is not None])

    async def restore_gateway(self, body: str) -> str:
        pass
