import time
from functools import wraps
from threading import Thread

from funboost import boost, BrokerEnum, BoostersManager

task_list = list()


def register_task():
    def _f():
        global task_list
        while True:
            if task_list:
                task = task_list.pop()
                """
                ? 在以多进程启动的服务中，不能使用multi_process_consume方法再次fork多进程
                """
                BoostersManager.get_or_create_booster_by_queue_name(task).consume()
            time.sleep(3)

    Thread(target=_f).start()


def add_task(func):
    global task_list
    task_list.append(func)

    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper

def task_delete(pk):
    return pk
