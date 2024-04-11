import heapq

from core.lib import util
from core.lib.cfg import load_env
from core.settings.settings import Settings

settings: Settings

# 优先加载本地配置文件
load_env()


class MultiSettings(object):

    def __init__(self):
        self._heap = []

    def register_settings(self, index: int, s):
        # 数字越大，优先级越高
        heapq.heappush(self._heap, (index, s))

    def list_settings(self):
        return self._heap

    def get_configs(self):
        configs = {}
        # 优先使用本地配置
        for _, s in self._heap:
            config = s.get_config()
            self.update_config(configs, config)
        return configs

    def update_config(self, config_1, config_2):
        for k, v in config_2.items():

            if isinstance(v, dict):
                if not config_1.get(k):
                    config_1[k] = {}
                self.update_config(config_1[k], config_2[k])
            elif v is not None:
                config_1[k] = v

    def get_settings(self):
        configs = self.get_configs()
        return Settings(**configs)


def load_config():
    global settings

    ms = MultiSettings()

    local_settings = Settings()
    ms.register_settings(2, local_settings)

    settings = ms.get_settings()

load_config()
