import logging
import os
from logging import handlers


# configure logger and log that this shouldn't be done here
def config_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s -> %(message)s"
    )
    log_file = "calipower.log"
    # Idempotent: don't open a second OS handle on the same file if a rotating
    # handler for it is already attached. Two open handles make log rotation
    # fail on Windows (WinError 32); see monet/__init__.py:config_logger.
    resolved = os.path.abspath(log_file)
    for existing in logger.handlers:
        if isinstance(
            existing, handlers.RotatingFileHandler
        ) and os.path.abspath(getattr(existing, "baseFilename", "")) == (
            resolved
        ):
            return
    file_handler = handlers.RotatingFileHandler(
        log_file, maxBytes=int(1e6), backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.WARNING)
    logger.addHandler(file_handler)
    # logger.addHandler(stream_handler)


config_logger()
