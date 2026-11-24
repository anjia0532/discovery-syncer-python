# from core.lib import cfg
#
# MYSQL_USER = cfg.get('')
# MYSQL_PASSWORD = cfg.get('MYSQL_PASSWORD')
# MYSQL_SERVER = cfg.get('MYSQL_SERVER')
# MYSQL_DB = cfg.get('MYSQL_DB')
#
# MYSQL_DATABASE_URI = f'mysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_SERVER}/{MYSQL_DB}'
#
# engine = create_engine(
#     SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
# )
# SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
#
# from sqlalchemy.orm import declarative_base
#
# Base = declarative_base()
import functools
import logging

from db_libs.sqla_lib import SqlaReflectHelper
from sqlalchemy import create_engine, event

_logger = logging.getLogger(__name__)


@functools.lru_cache()
def get_sqla_helper():
    # echo 关掉：健康检查写入路径每轮都会 INSERT/UPDATE/DELETE，echo=True 会把每条 SQL
    # 都刷进日志（还经 nb_log 重定向进文件），排查业务问题时纯噪音。
    # timeout=15 + WAL：TaskBus 工作线程与调度线程会并发写同一个 sqlite 文件，
    # 默认 5s 超时容易撞 "database is locked"；WAL 允许读写并发、写入不阻塞读。
    enginex = create_engine(
        "sqlite+pysqlite:///syncer-jobs.db",
        echo=False,
        connect_args={"timeout": 15},
    )

    @event.listens_for(enginex, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        # WAL 需要在同目录创建 -wal/-shm。只读挂载或共享内存语义不完整的网络盘上会失败；
        # 失败只影响并发写体验、不影响正确性，所以降级告警而不是让启动直接崩。
        cursor = dbapi_connection.cursor()
        for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
            try:
                cursor.execute(pragma)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("sqlite %s 设置失败，按默认模式继续: %s", pragma, exc)
        cursor.close()

    sqla_helper = SqlaReflectHelper(enginex)
    return enginex, sqla_helper
