import json
import logging
import os
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app_config import config

request_id_var: ContextVar[str | None] = ContextVar('request_id', default=None)

LOG_LEVEL = str(config.get('log_level', 'INFO')).upper()
LOG_JSON = bool(config.get('log_json', False))
LOG_DIR = Path(os.getenv('LOG_DIR', Path(__file__).resolve().parent))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE_PATH = Path(os.getenv('LOG_FILE', LOG_DIR / 'bot.log'))
LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', str(20 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', '3'))


class BeijingFormatter(logging.Formatter):
    def converter(self, *_args):
        utc_dt = datetime.now(timezone.utc)
        bj_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
        return bj_dt.timetuple()

    def format(self, record: logging.LogRecord) -> str:
        setattr(record, 'request_id', request_id_var.get() or '-')
        return super().format(record)


class JsonFormatter(BeijingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            'timestamp': datetime.now(timezone(timedelta(hours=8))).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'module': record.filename,
            'line': record.lineno,
            'request_id': request_id_var.get() or '-',
            'message': record.getMessage(),
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


TEXT_FORMATTER = BeijingFormatter(
    fmt='%(asctime)s [%(levelname)s] [req=%(request_id)s] %(filename)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
JSON_FORMATTER = JsonFormatter()
FORMATTER = JSON_FORMATTER if LOG_JSON else TEXT_FORMATTER


def setup_logger() -> logging.Logger:
    logger = logging.getLogger('LyrebirdBot')
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    file_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8',
        delay=True,
    )
    file_handler.setFormatter(FORMATTER)
    file_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(FORMATTER)
    console_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = setup_logger()


def sanitize_message(message: str) -> str:
    for secret_key in ('bot_token', 'api_hash', 'gemini_api_key', 'password', 'mstoken', 'emby_api'):
        secret_value = config.get(secret_key)
        if secret_value and isinstance(secret_value, str):
            message = message.replace(secret_value, '***')
    proxy_password = (config.get('proxy') or {}).get('password')
    if proxy_password:
        message = message.replace(proxy_password, '***')
    return message


def write_log(content: str, level: str = 'INFO', exc_info: bool = False):
    safe_content = sanitize_message(str(content))
    lvl = level.upper()
    if lvl == 'ERROR':
        logger.error(safe_content, exc_info=exc_info)
    elif lvl == 'WARNING':
        logger.warning(safe_content, exc_info=exc_info)
    elif lvl == 'DEBUG':
        logger.debug(safe_content, exc_info=exc_info)
    elif lvl == 'CRITICAL':
        logger.critical(safe_content, exc_info=exc_info)
    else:
        logger.info(safe_content, exc_info=exc_info)


def log_exception(prefix: str):
    write_log(f'{prefix}: {traceback.format_exc()}', level='ERROR')
