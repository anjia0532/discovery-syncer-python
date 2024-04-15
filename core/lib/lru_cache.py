"""
:Author:  HelloWorld
:Create:  2023/4/20
缓存装饰器
废弃，发现功能强大的工具包aiocache
"""
import functools
from collections import OrderedDict


def async_lru_cache(maxsize=128):
    def wrapper(func):
        cache = OrderedDict()

        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            key = (args, frozenset(kwargs.items()))
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
            result = await func(*args, **kwargs)
            cache[key] = result
            if len(cache) > maxsize:
                cache.popitem(last=False)
            return result
        return wrapped
    return wrapper
