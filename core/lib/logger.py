import datetime
import logging
import os
import sys
import time
from collections import OrderedDict
from queue import Queue
# noinspection PyPackageRequirements
# from elasticsearch import Elasticsearch, helpers  # 性能导入时间消耗2秒,实例化时候再导入。
from threading import Thread

from funboost.core.current_task import funboost_current_task
from funboost.core.task_id_logger import TaskIdLogger
from nb_log import LogManager
from nb_log.monkey_print import nb_print

from nb_log_config import IS_ADD_ELASTIC_HANDLER, \
    NB_LOG_FORMATER_INDEX_FOR_CONSUMER_AND_PUBLISHER, ELASTIC_HOST, computer_name

very_nb_print = nb_print


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
            fct = funboost_current_task()

            log_info_dict['@timestamp'] = datetime.datetime.utcfromtimestamp(record.created).isoformat()
            log_info_dict['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
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
            if record.task_id:
                log_info_dict['task_id'] = record.task_id
            self.__add_task_to_bulk({
                "_index": f'{self._index_prefix}{record.name.lower()}',
                "_source": log_info_dict
            })

        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.handleError(record)
