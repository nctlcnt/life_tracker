"""
日志模块：集中配置
其他模块只要 `logger = get_logger(__name__)`，之后用 logger.info/debug/error 即可。
要改格式、级别、输出目标，只动这一个文件。
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_ROOT_NAME = "life_tracker"
_configured = False


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """
    在进程启动最开始调用一次。
    level: 日志级别字符串（DEBUG/INFO/WARNING/ERROR）
    log_file: 若提供则同时写入文件（带轮转），默认只输出 stdout
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(level.upper())
    root.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout handler（Docker 友好）
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # 可选文件 handler
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # 不向 python root logger 传播，避免 uvicorn 等重复打印
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    获取子 logger。传 __name__ 即可，会自动挂到 life_tracker 命名空间下。
    """
    short = name
    for prefix in ("bot.", "api.", "scripts."):
        if short.startswith(prefix):
            short = short[len(prefix):]
            break
    if short == "__main__":
        short = "main"
    return logging.getLogger(f"{_ROOT_NAME}.{short}")
