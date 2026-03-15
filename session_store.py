import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from app_config import config
from logger.logger import write_log

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None

_RUNTIME_DIR = Path(config.get('runtime_dir', Path(__file__).resolve().parent / 'runtime'))
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_FILE = Path(os.getenv('SESSION_CACHE_FILE', _RUNTIME_DIR / 'session_cache.json'))
_REDIS_URL = os.getenv('REDIS_URL', '').strip()
_PREFIX = os.getenv('REDIS_PREFIX', 'lyrebird:session:')
_TTL = int(os.getenv('SESSION_CACHE_TTL', '7200'))
_LOCK = threading.Lock()
_redis_client = None

if _REDIS_URL and redis is not None:
    try:
        _redis_client = redis.from_url(_REDIS_URL, decode_responses=True)
        _redis_client.ping()
        write_log('Session store 已启用 Redis 持久化')
    except Exception as e:
        _redis_client = None
        write_log(f'Redis 不可用，回退到文件会话缓存: {e}', level='WARNING')


def _read_file_cache() -> Dict[str, Any]:
    if not _CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_CACHE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _write_file_cache(data: Dict[str, Any]) -> None:
    tmp = _CACHE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(_CACHE_FILE)


class SessionStore:
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if _redis_client:
            try:
                value = _redis_client.get(_PREFIX + key)
                return json.loads(value) if value else None
            except Exception as e:
                write_log(f'Redis 读取会话失败 key={key}: {e}', level='WARNING')
        with _LOCK:
            return _read_file_cache().get(key)

    def set(self, key: str, value: Dict[str, Any], ttl: int = _TTL) -> None:
        if _redis_client:
            try:
                _redis_client.setex(_PREFIX + key, ttl, json.dumps(value, ensure_ascii=False))
                return
            except Exception as e:
                write_log(f'Redis 写入会话失败 key={key}: {e}', level='WARNING')
        with _LOCK:
            data = _read_file_cache()
            data[key] = value
            _write_file_cache(data)

    def delete(self, key: str) -> None:
        if _redis_client:
            try:
                _redis_client.delete(_PREFIX + key)
            except Exception as e:
                write_log(f'Redis 删除会话失败 key={key}: {e}', level='WARNING')
        with _LOCK:
            data = _read_file_cache()
            data.pop(key, None)
            _write_file_cache(data)

    def update(self, key: str, patch: Dict[str, Any], ttl: int = _TTL) -> Dict[str, Any]:
        current = self.get(key) or {}
        current.update(patch)
        self.set(key, current, ttl=ttl)
        return current

    def stats(self) -> Dict[str, Any]:
        if _redis_client:
            try:
                count = 0
                for _ in _redis_client.scan_iter(match=f'{_PREFIX}*', count=200):
                    count += 1
                return {'backend': 'redis', 'keys': count, 'cache_file': str(_CACHE_FILE)}
            except Exception as e:
                return {'backend': 'redis', 'keys': 0, 'error': str(e), 'cache_file': str(_CACHE_FILE)}
        with _LOCK:
            data = _read_file_cache()
            return {'backend': 'file', 'keys': len(data), 'cache_file': str(_CACHE_FILE)}

    def clear(self) -> int:
        removed = 0
        if _redis_client:
            try:
                keys = list(_redis_client.scan_iter(match=f'{_PREFIX}*', count=200))
                if keys:
                    removed += _redis_client.delete(*keys)
            except Exception as e:
                write_log(f'Redis 清理会话失败: {e}', level='WARNING')
        with _LOCK:
            data = _read_file_cache()
            removed += len(data)
            _write_file_cache({})
        return removed


session_store = SessionStore()
