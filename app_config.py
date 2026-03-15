import json
import os
from pathlib import Path
from typing import Any, Dict

from admin_settings import load_admin_overrides

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv('CONFIG_PATH', BASE_DIR / 'config.json'))


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _read_config_file() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f'配置文件不存在: {CONFIG_PATH}. 请挂载 config.json，或通过 CONFIG_PATH 指定路径。'
        )
    with CONFIG_PATH.open('r', encoding='utf-8') as f:
        return json.load(f)


def _env_bool(name: str, default: bool | None = None) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value == '':
        return default
    return int(value)


def _proxy_from_env() -> Dict[str, Any] | None:
    proxy_url = os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')
    if not proxy_url:
        return None
    # 供 requests 使用时直接读环境变量即可；这里只是给 pyrogram 用
    # 支持 http://user:pass@host:port 或 socks5://...
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    if not parsed.hostname or not parsed.port:
        return None
    return {
        'scheme': parsed.scheme or 'http',
        'hostname': parsed.hostname,
        'port': parsed.port,
        'username': parsed.username,
        'password': parsed.password,
    }


def load_config() -> Dict[str, Any]:
    cfg = _read_config_file()

    env_override: Dict[str, Any] = {
        'proxy_mode': _env_bool('PROXY_MODE', cfg.get('proxy_mode', False)),
        'search_timeout': _env_int('SEARCH_TIMEOUT', cfg.get('search_timeout', 60)),
        'request_timeout': _env_int('REQUEST_TIMEOUT', cfg.get('request_timeout', 30)),
        'request_retries': _env_int('REQUEST_RETRIES', cfg.get('request_retries', 2)),
        'transfer_notice_enabled': _env_bool('TRANSFER_NOTICE_ENABLED', cfg.get('transfer_notice_enabled', True)),
        'translation_enabled': _env_bool('TRANSLATION_ENABLED', cfg.get('translation_enabled', True)),
        'tmdb_bg_enabled': _env_bool('TMDB_BG_ENABLED', cfg.get('tmdb_bg_enabled', False)),
        'proxy': cfg.get('proxy', {}),
    }

    env_proxy = _proxy_from_env()
    if env_proxy:
        env_override['proxy_mode'] = True
        env_override['proxy'] = env_proxy

    cfg = _deep_merge(cfg, env_override)
    cfg = _deep_merge(cfg, load_admin_overrides())
    cfg.setdefault('search_timeout', 60)
    cfg.setdefault('request_timeout', 30)
    cfg.setdefault('request_retries', 2)
    cfg.setdefault('transfer_notice_enabled', True)
    cfg.setdefault('translation_enabled', True)
    cfg.setdefault('tmdb_bg_enabled', False)
    cfg.setdefault('log_level', os.getenv('LOG_LEVEL', 'INFO'))
    cfg.setdefault('log_json', _env_bool('LOG_JSON', False) or False)
    cfg.setdefault('runtime_dir', os.getenv('RUNTIME_DIR', str(BASE_DIR / 'runtime')))
    cfg.setdefault('session_workdir', os.getenv('SESSION_WORKDIR', str(Path(cfg['runtime_dir']) / 'pyrogram')))

    cfg.setdefault('admin_panel_enabled', _env_bool('ADMIN_PANEL_ENABLED', True))
    cfg.setdefault('admin_panel_host', os.getenv('ADMIN_PANEL_HOST', '0.0.0.0'))
    cfg.setdefault('admin_panel_port', _env_int('ADMIN_PANEL_PORT', 47521) or 47521)
    cfg.setdefault('admin_panel_token', os.getenv('ADMIN_PANEL_TOKEN', cfg.get('admin_panel_token', 'change-me')))
    cfg.setdefault('admin_panel_title', os.getenv('ADMIN_PANEL_TITLE', f"{cfg.get('name', 'LyrebirdMS Bot')} 管理面板"))
    return cfg


config = load_config()
