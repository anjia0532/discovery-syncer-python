import datetime
import json
import logging
import os
import socket
import sys
import time
from collections import OrderedDict
# noinspection PyUnresolvedReferences
from logging.handlers import WatchedFileHandler
from queue import Queue
from threading import Lock, Thread

import requests
from funboost.core.task_id_logger import TaskIdLogger
from nb_log import LogManager
# noinspection PyPackageRequirements
from nb_log.monkey_print import nb_print

from nb_log_config import IS_ADD_ELASTIC_HANDLER, \
    NB_LOG_FORMATER_INDEX_FOR_CONSUMER_AND_PUBLISHER, ELASTIC_HOST, computer_name, IS_ADD_DING_TALK_HANDLER, \
    DING_TALK_TOKEN, DING_TALK_SECRET, TIME_INTERVAL, DING_TALK_MSG_TEMPLATE

very_nb_print = nb_print

host_name = socket.gethostname()


def get(name: str = '', tag: str = '') -> logging.Logger:
    """
    get logger of specific tag
    :param name: name of the logger
    :param tag: tag of the logger
    :return: logger instance
    """
    if tag:
        logger_name = (tag + '_' + name)
    else:
        logger_name = name
    logger = LogManager(logger_name, logger_cls=TaskIdLogger).get_logger_and_add_handlers(
        formatter_template=NB_LOG_FORMATER_INDEX_FOR_CONSUMER_AND_PUBLISHER)

    if IS_ADD_ELASTIC_HANDLER:
        handler = ElasticHandler([ELASTIC_HOST], "")
        handler.setLevel(10)
        logger.addHandler(handler)

    if IS_ADD_DING_TALK_HANDLER:
        handler = DingTalkHandler(DING_TALK_TOKEN, TIME_INTERVAL, DING_TALK_SECRET)
        # warnings
        handler.setLevel(30)
        logger.addHandler(handler)
    return logger


def for_handler(name: str) -> logging.Logger:
    """
    get handler logger
    :param name: handler name
    :return: controller logger
    """
    return get('handler', name)


def for_middleware(name: str) -> logging.Logger:
    """
    get middleware logger
    :param name: middleware name
    :return: middleware logger
    """
    return get('middleware', name)


def for_model(name: str) -> logging.Logger:
    """
    get model logger
    :param name: model name
    :return: model logger
    """
    return get('model', name)


def for_service(name: str) -> logging.Logger:
    """
    get service logger
    :param name: service name
    :return: service logger
    """
    return get('service', name)


def for_task(name: str) -> logging.Logger:
    """
    get service logger
    :param name: task name
    :return: task logger
    """
    return get('task', name)


class DingTalkHandler(logging.Handler):
    _lock_for_remove_handlers = Lock()

    def __init__(self, ding_talk_token: str = None, time_interval: int = 60, ding_talk_secret: str = None):
        super().__init__()
        self.ding_talk_token = ding_talk_token
        self.ding_talk_secret = ding_talk_secret
        self._ding_talk_url = f'https://oapi.dingtalk.com/robot/send?access_token={ding_talk_token}'
        self._current_time = 0
        self._time_interval = time_interval  # 最好别频繁发。
        self._msg_template = '{"msgtype":"markdown","markdown":{"title":"discovery-syncer-python","text":"**时间:** %(asctime)s\n\n**任务:** %(task_id)s\n\n**脚本:** %(pathname)s\n\n**函数:** %(funcName)s\n\n**行号:** %(lineno)s\n\n**信息:** %(msg)s"}}'
        self._lock = Lock()

    def emit(self, record):
        # from threading import Thread
        with self._lock:
            if time.time() - self._current_time > self._time_interval:
                # very_nb_print(self._current_time)
                self._current_time = time.time()
                self.__emit(record)
                # Thread(target=self.__emit, args=(record,)).start()

            else:
                very_nb_print(
                    f'此次离上次发送钉钉消息时间间隔不足 {self._time_interval} 秒，此次不发送这个钉钉内容： {record.msg}')

    def __emit(self, record):
        try:
            record.msg = record.msg.replace("\\", "\\\\").replace("\\\\n", "\\n").replace("\"", "'")
            record.pathname = record.pathname.replace("\\", "\\\\").replace("\\\\n", "\\n").replace("\"", "'")
            data = (DING_TALK_MSG_TEMPLATE or self._msg_template) % record.__dict__
            # 因为钉钉发送也是使用requests实现的，如果requests调用的urllib3命名空间也加上了钉钉日志，将会造成循环，程序卡住。一般情况是在根日志加了钉钉handler。
            self._remove_urllib_hanlder()
            resp = requests.post(self._ding_talk_url + self.sign(), json=json.loads(data), timeout=(5, 5))
            very_nb_print(f'钉钉返回 : {resp.text}')
        except Exception as e:
            very_nb_print(f"发送消息给钉钉机器人失败,原始消息: {record.msg} {e}")

    def __repr__(self):
        level = logging.getLevelName(self.level)
        return '<%s (%s)>' % (self.__class__.__name__, level) + ' dingtalk token is ' + self.ding_talk_token

    def sign(self):
        if not self.ding_talk_secret:
            return ""
        import time
        import hmac
        import hashlib
        import base64
        import urllib.parse
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.ding_talk_secret.encode('utf-8')
        string_to_sign = '{}\n{}'.format(timestamp, self.ding_talk_secret)
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return "&timestamp={}&sign={}".format(timestamp, sign)

    @classmethod
    def _remove_urllib_hanlder(cls):
        for name in ['root', 'urllib3', 'requests']:
            cls.__remove_urllib_hanlder_by_name(name)

    @classmethod
    def __remove_urllib_hanlder_by_name(cls, logger_name):
        with cls._lock_for_remove_handlers:
            for index, hdlr in enumerate(logging.getLogger(logger_name).handlers):
                if 'DingTalkHandler' in str(hdlr):
                    logging.getLogger(logger_name).handlers.pop(index)


# noinspection PyUnresolvedReferences
class ElasticHandler(logging.Handler):
    """
    日志批量写入es中。
    """
    ES_INTERVAL_SECONDS = 0.5

    host_name = computer_name
    host_process = f'{host_name} -- {os.getpid()}'

    script_name = sys.argv[0]

    task_queue = Queue()
    last_es_op_time = time.time()
    has_start_do_bulk_op = False

    def __init__(self, elastic_hosts: list, elastic_port, index_prefix='pylog-'):
        """
        :param elastic_hosts:  es的ip地址，数组类型
        :param elastic_port：  es端口
        :param index_prefix: index名字前缀。
        """
        logging.Handler.__init__(self)
        from elasticsearch import Elasticsearch, helpers
        self._helpers = helpers
        self._es_client = Elasticsearch(elastic_hosts, )
        self._index_prefix = index_prefix
        t = Thread(target=self._do_bulk_op)
        t.setDaemon(True)
        t.start()

    @classmethod
    def __add_task_to_bulk(cls, task):
        cls.task_queue.put(task)

    # noinspection PyUnresolvedReferences
    @classmethod
    def __clear_bulk_task(cls):
        cls.task_queue.queue.clear()

    def _do_bulk_op(self):
        if self.__class__.has_start_do_bulk_op:
            return
        self.__class__.has_start_do_bulk_op = True
        while 1:
            try:
                if self.__class__.task_queue.qsize() > 10000:
                    very_nb_print('防止意外日志积累太多了，不插入es了。')
                    self.__clear_bulk_task()
                    return
                tasks = list(self.__class__.task_queue.queue)
                self.__clear_bulk_task()
                self._helpers.bulk(self._es_client, tasks)
                self.__class__.last_es_op_time = time.time()
            except Exception as e:
                very_nb_print(e)
            finally:
                time.sleep(self.ES_INTERVAL_SECONDS)

    def emit(self, record):
        # noinspection PyBroadException, PyPep8
        try:
            level_str = None
            if record.levelno == 10:
                level_str = 'DEBUG'
            elif record.levelno == 20:
                level_str = 'INFO'
            elif record.levelno == 30:
                level_str = 'WARNING'
            elif record.levelno == 40:
                level_str = 'ERROR'
            elif record.levelno == 50:
                level_str = 'CRITICAL'
            log_info_dict = OrderedDict()

            log_info_dict['@timestamp'] = datetime.datetime.utcfromtimestamp(record.created).isoformat()
            log_info_dict['name'] = record.name
            log_info_dict['host'] = self.host_name
            log_info_dict['host_process'] = self.host_process
            log_info_dict['file_path'] = record.pathname
            log_info_dict['file_name'] = record.filename
            log_info_dict['func_name'] = record.funcName
            log_info_dict['line_no'] = record.lineno
            log_info_dict['log_level'] = level_str
            log_info_dict['msg'] = str(record.msg)
            log_info_dict['script'] = self.script_name
            log_info_dict['task_id'] = record.task_id
            self.__add_task_to_bulk({
                "_index": f'{self._index_prefix}{time.strftime("%Y.%m.%d")}',
                "_source": log_info_dict
            })

        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.handleError(record)
