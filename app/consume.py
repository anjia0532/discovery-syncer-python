import importlib
import inspect
from threading import Thread

from funboost import BoostersManager


def load_task_functions(module_name):
    module = importlib.import_module(module_name)
    for name, func in inspect.getmembers(module, inspect.isfunction):
        if name.startswith('task_') and (
                hasattr(func, 'is_decorated_as_consume_function') and func.is_decorated_as_consume_function):
            BoostersManager.get_or_create_booster_by_queue_name(func.__name__).consume()


def register_task(modules):
    for module in modules:
        Thread(target=load_task_functions, args=(module,)).start()
