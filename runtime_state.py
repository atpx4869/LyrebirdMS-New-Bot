import json
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

from app_config import config

_RUNTIME_DIR = Path(config.get('runtime_dir', Path(__file__).resolve().parent / 'runtime'))
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = Path(os.getenv('STATE_FILE', _RUNTIME_DIR / 'status.json'))
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _read() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            'updated_at': _now_iso(),
            'service': 'unknown',
            'bot': {},
            'web_admin': {},
            'counters': {},
            'events': [],
            'features': {},
        }
    try:
        with STATE_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {
        'updated_at': _now_iso(),
        'service': 'unknown',
        'bot': {},
        'web_admin': {},
        'counters': {},
        'events': [],
        'features': {},
    }


def _write(data: Dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix('.tmp')
    data['updated_at'] = _now_iso()
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def read_state() -> Dict[str, Any]:
    with _LOCK:
        return _read()


def merge_state(partial: Dict[str, Any]) -> None:
    with _LOCK:
        data = _read()
        for key, value in partial.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            else:
                data[key] = value
        _write(data)


def record_event(name: str, **payload: Any) -> None:
    with _LOCK:
        data = _read()
        events = data.setdefault('events', [])
        events.append({'name': name, 'time': _now_iso(), **payload})
        data['events'] = events[-50:]
        _write(data)


def bump_counter(name: str, amount: int = 1) -> None:
    with _LOCK:
        data = _read()
        counters = data.setdefault('counters', {})
        counters[name] = int(counters.get(name, 0)) + amount
        _write(data)


def set_bot_status(status: str, **extra: Any) -> None:
    merge_state({'service': status, 'bot': {'status': status, 'heartbeat': _now_iso(), **extra}})


def set_feature_flags(flags: Dict[str, Any]) -> None:
    merge_state({'features': flags})
