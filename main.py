import nb_log
import uvicorn

from core.lib import util
from core.lib.cfg import get_cmd_opts, load_cfg

logger = nb_log.get_logger(__name__)


def uvicorn_run(cfg):
    cfg = cfg.get('uvicorn_cfg')
    uvicorn.run('app:APP', **cfg)


def main() -> None:
    """
    main function, steps are:
    1. Get cmd opts with the current environment (dev/prod)
    2. Read configs by env (uvicorn.json, logger.json)
    3. Run uvicorn application, launch APP in app/__init__.py
    for more uvicorn args, refer to uvicorn/config.py
    :return: None
    """
    opts = get_cmd_opts()
    logger.info('launch with cmd opts: %s' % util.pfmt(opts))
    cfg = load_cfg(opts['env'])

    uvicorn_run(cfg)
    # gunicorn_run(cfg)


if __name__ == '__main__':
    main()
