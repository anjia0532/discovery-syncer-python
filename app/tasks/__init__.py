"""同步任务模块。

任务函数现在是普通函数，由 ``app.runtime.TaskBus`` 提交执行，
不再需要 funboost 的 ``@boost`` 装饰器与 ``.consume()`` 消费者注册。
保留本包是为了维持 ``app.tasks.task_syncer`` 的既有导入路径。
"""
