import json
import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.getenv('RUNTIME_DIR', BASE_DIR / 'runtime'))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = Path(os.getenv('ADMIN_SETTINGS_FILE', RUNTIME_DIR / 'admin_overrides.json'))

SAFE_KEYS = {
    'translation_enabled': bool,
    'transfer_notice_enabled': bool,
    'tmdb_bg_enabled': bool,
    'log_level': str,
    'ai_provider': str,
    'ai_model': str,
    'ai_base_url': str,
    'gemini_model': str,
    'ai_chunk_chars': int,
}


def load_admin_overrides() -> Dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with SETTINGS_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce(expected, value):
    if expected is bool:
        return bool(value)
    if expected is int:
        try:
            return int(value)
        except Exception:
            return 0
    if expected is str:
        return '' if value is None else str(value).strip()
    return value


def save_admin_overrides(patch: Dict[str, Any]) -> Dict[str, Any]:
    current = load_admin_overrides()
    for key, value in patch.items():
        if key not in SAFE_KEYS:
            continue
        current[key] = _coerce(SAFE_KEYS[key], value)
    with SETTINGS_FILE.open('w', encoding='utf-8') as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    return current
