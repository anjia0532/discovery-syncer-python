import logging
import typing

from funboost import BoosterParams, BrokerEnum

class FunboostCommonConfig(BoosterParams):
    # 中间件选型见3.1章节 https://funboost.readthedocs.io/zh/latest/articles/c3.html
    broker_kind: str = BrokerEnum.SQLITE_QUEUE
    # 最大自动重试次数
    max_retry_times: int = 4
    # 函数出错后间隔多少秒再重试.
    retry_interval: typing.Union[float, int] = 30
    # 消费者和发布者的日志级别
    log_level: int = logging.INFO
    # 是否使用分布式控频
    is_using_distributed_frequency_control: bool = False
    # # 函数达到最大重试次数仍然没成功，是否发送到死信队列,死信队列的名字是 队列名字 + _dlx。
    is_push_to_dlx_queue_when_retry_max_times: bool = True
    # 任务过滤的失效期，为0则永久性过滤任务。例如设置过滤过期时间是1800秒 ， 30分钟前发布过1 + 2 的任务，现在仍然执行，如果是30分钟以内发布过这个任务，则不执行1 + 2
    task_filtering_expire_seconds: int = 0
    # # 是否对函数入参进行过滤去重.
    do_task_filtering: bool = False
    # 运行时候,是否记录从消息队列获取出来的消息内容
    is_show_message_get_from_broker: bool = True
    # 提供一个用户自定义的保存消息处理记录到某个地方例如mysql数据库的函数，函数仅仅接受一个入参，入参类型是 FunctionResultStatus，用户可以打印参数
    # user_custom_record_process_info_func: typing.Callable = save_result_status_to_sqlalchemy
    # 是否将发布者的心跳发送到redis，有些功能的实现需要统计活跃消费者。因为有的中间件不是真mq。这个功能,需要安装redis.
    is_send_consumer_hearbeat_to_redis: bool = False
    # 是否支持远程任务杀死功能，如果任务数量少，单个任务耗时长，确实需要远程发送命令来杀死正在运行的函数，才设置为true，否则不建议开启此功能。
    is_support_remote_kill_task: bool = False
    function_timeout: typing.Union[int, float] = 600