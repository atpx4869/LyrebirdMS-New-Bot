import json
import os
import io
import zipfile
import difflib
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for, flash, get_flashed_messages, send_file

from admin_settings import save_admin_overrides
from app_config import CONFIG_PATH, config
from healthcheck import run_healthcheck
from logger.logger import logger, write_log
from runtime_state import merge_state, read_state, record_event
from session_store import session_store
from sql.embybot import admin_adjust_user, get_user_detail, get_user_stats, list_recent_users, search_users
from sql.msbot import get_download_by_torrent_id, get_download_stats, get_downloads_by_user, list_recent_downloads, search_downloads
from task_manager import delete_task, get_task, list_tasks, prune_tasks, request_retry, request_retry_failed, task_stats

app = Flask(__name__)
app.secret_key = os.getenv('ADMIN_PANEL_SECRET', str(config.get('admin_panel_token', 'change-me')) + '-panel')
TITLE = config.get('admin_panel_title', 'LyrebirdMS Bot 管理面板')
LOG_DIR = Path(os.getenv('LOG_DIR', Path(__file__).resolve().parent / 'logger'))
LOG_FILE = Path(os.getenv('LOG_FILE', LOG_DIR / 'bot.log'))
CRON_LOG_FILE = Path(os.getenv('CRON_LOG_FILE', LOG_DIR / 'cron.log'))
TOKEN = str(config.get('admin_panel_token', 'change-me'))
TZ = timezone(timedelta(hours=8))
BLACKSEEDS_FILE = Path(os.getenv('BLACKSEEDS_FILE', Path(__file__).resolve().parent / 'blackseeds.txt'))
ENV_PATH = Path(os.getenv('ENV_PATH', Path(__file__).resolve().parent / '.env'))
BOOTSTRAP_MODE = os.getenv('BOOTSTRAP_MODE', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}


def _tail(path: Path, lines: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding='utf-8', errors='ignore').splitlines()[-lines:]
    except Exception as e:
        return [f'读取失败: {e}']


def _blackseeds_lines(limit: int = 300) -> list[str]:
    if not BLACKSEEDS_FILE.exists():
        return []
    return [line.strip() for line in BLACKSEEDS_FILE.read_text(encoding='utf-8', errors='ignore').splitlines() if line.strip()][:limit]


def is_authed() -> bool:
    return session.get('admin_authed') is True


def require_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if TOKEN == 'change-me':
            session['admin_authed'] = True
        if is_authed():
            return func(*args, **kwargs)
        return redirect(url_for('login', next=request.path))
    return wrapper


def _redirect_with_message(endpoint: str, message: str, category: str = 'info', **values):
    flash(message, category)
    return redirect(url_for(endpoint, **values))


def _safe_config():
    data = dict(config)
    for key in ('bot_token', 'api_hash', 'gemini_api_key', 'ai_api_key', 'mstoken', 'password', 'emby_api', 'mspostgre_password', 'admin_panel_token'):
        if key in data and data[key]:
            data[key] = '***'
    proxy = dict(data.get('proxy') or {})
    if proxy.get('password'):
        proxy['password'] = '***'
    data['proxy'] = proxy
    return data


def _raw_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _status_payload():
    state = read_state()
    heartbeat = (((state.get('bot') or {}).get('heartbeat')) or '')
    stale = False
    if heartbeat:
        try:
            stale = (datetime.now(TZ) - datetime.fromisoformat(heartbeat)) > timedelta(minutes=3)
        except Exception:
            pass
    return {
        'title': TITLE,
        'proxy_mode': bool(config.get('proxy_mode')),
        'translation_enabled': bool(config.get('translation_enabled', True)),
        'transfer_notice_enabled': bool(config.get('transfer_notice_enabled', True)),
        'tmdb_bg_enabled': bool(config.get('tmdb_bg_enabled', False)),
        'ai_provider': str(config.get('ai_provider', 'gemini')),
        'ai_model': str(config.get('ai_model') or config.get('gemini_model') or ''),
        'bot_status': (state.get('bot') or {}).get('status', 'unknown'),
        'bot_heartbeat': heartbeat,
        'bot_stale': stale,
        'state': state,
        'download_stats': get_download_stats(),
        'user_stats': get_user_stats(),
        'task_stats': task_stats('translation'),
        'session_stats': session_store.stats(),
    }


def _writable_check(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / '.write-test'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return {"path": str(path), "ok": True}
    except Exception as e:
        return {"path": str(path), "ok": False, "error": str(e)}


def _preflight_payload() -> dict[str, Any]:
    raw = _raw_config()
    health = run_healthcheck(log_on_success=False)
    env_lines = _read_env_text().splitlines()
    env_map = {}
    for line in env_lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env_map[k.strip()] = v.strip()
    runtime_dir = os.getenv('RUNTIME_DIR', '/data/runtime')
    log_dir = os.getenv('LOG_DIR', '/data/logs')
    checks = [
        {"label": "config.json", "ok": CONFIG_PATH.exists(), "hint": f"路径: {CONFIG_PATH}"},
        {"label": "blackseeds.txt", "ok": BLACKSEEDS_FILE.exists(), "hint": f"路径: {BLACKSEEDS_FILE}"},
        {"label": "管理面板令牌", "ok": str(config.get('admin_panel_token') or env_map.get('ADMIN_PANEL_TOKEN') or '') not in {'', 'change-me'}, "hint": '请将 ADMIN_PANEL_TOKEN 改为强口令'},
        {"label": "MySQL 连通性", "ok": bool((health.get('mysql') or {}).get('ok')), "hint": str((health.get('mysql') or {}).get('error') or '正常')},
        {"label": "PostgreSQL 连通性", "ok": bool((health.get('postgres') or {}).get('ok')), "hint": str((health.get('postgres') or {}).get('error') or '正常')},
        {"label": "Redis 连通性", "ok": bool((health.get('redis') or {}).get('ok')), "hint": str((health.get('redis') or {}).get('error') or '正常')},
        {"label": "运行目录可写", "ok": _writable_check(runtime_dir).get('ok'), "hint": runtime_dir},
        {"label": "日志目录可写", "ok": _writable_check(log_dir).get('ok'), "hint": log_dir},
    ]
    if raw.get('proxy_mode'):
        proxy = raw.get('proxy') or {}
        checks.append({"label": "Telegram 代理", "ok": bool(proxy.get('hostname') and proxy.get('port')), "hint": f"{proxy.get('scheme','http')}://{proxy.get('hostname','')}:{proxy.get('port','')}"})
    if env_map.get('HTTP_PROXY') or env_map.get('HTTPS_PROXY'):
        checks.append({"label": "HTTP(S) 代理环境变量", "ok": True, "hint": (env_map.get('HTTPS_PROXY') or env_map.get('HTTP_PROXY') or '')})
    else:
        checks.append({"label": "HTTP(S) 代理环境变量", "ok": not raw.get('proxy_mode'), "hint": '若需要外网 API / 海报 / AI 访问，建议在 .env 填写 HTTP_PROXY / HTTPS_PROXY'})
    warnings = []
    if (config.get('admin_panel_token') or env_map.get('ADMIN_PANEL_TOKEN') or 'change-me') == 'change-me':
        warnings.append('当前仍在使用默认 ADMIN_PANEL_TOKEN，部署前建议立即修改。')
    if health.get('missing'):
        warnings.append('基础配置仍有缺失，Bot 可能无法正常启动。')
    if raw.get('proxy_mode') and not ((raw.get('proxy') or {}).get('hostname')):
        warnings.append('已启用 proxy_mode，但 Telegram 代理主机未填写。')
    ok = all(bool(item.get('ok')) for item in checks) and not warnings
    return {
        'ok': ok,
        'generated_at': datetime.now(TZ).isoformat(),
        'checks': checks,
        'warnings': warnings,
    }


def _setup_summary():
    health = run_healthcheck(log_on_success=False)
    missing = health.get('missing') or []
    actions = []
    if not CONFIG_PATH.exists():
        actions.append('未检测到 config.json，容器首次启动会自动生成模板。请先补齐 Telegram、MovieServer、Emby 参数。')
    if any(item in missing for item in ['api_id', 'api_hash', 'bot_token']):
        actions.append('先填写 Telegram 的 api_id / api_hash / bot_token。')
    if any(item in missing for item in ['mshost', 'mstoken']):
        actions.append('再填写 MovieServer 地址和 token。')
    if not health.get('mysql', {}).get('ok'):
        actions.append('MySQL 尚未就绪。若使用内置数据库，通常等几十秒后会自动恢复。')
    if not health.get('postgres', {}).get('ok'):
        actions.append('PostgreSQL 尚未就绪。若首次初始化，请等待数据库建表完成。')
    if not health.get('movieserver', {}).get('ok', True):
        actions.append('MovieServer 当前不可达，请检查地址是否为 NAS 可访问的内网地址。')
    if not health.get('emby', {}).get('ok', True):
        actions.append('Emby 当前不可达，请检查 emby_host 和 emby_api。')
    preflight = _preflight_payload()
    for item in preflight.get('warnings') or []:
        actions.append(item)
    bad_checks = [c for c in (preflight.get('checks') or []) if not c.get('ok')]
    for item in bad_checks[:3]:
        actions.append(f"部署检查未通过：{item.get('label')}（{item.get('hint')}）")
    if not actions:
        actions.append('基础配置已齐，建议先测试 /start、资源搜索和一次下载。')
    return {'bootstrap_mode': BOOTSTRAP_MODE, 'health': health, 'actions': actions, 'preflight': preflight}


def _diagnostics_payload():
    return {
        'generated_at': datetime.now(TZ).isoformat(),
        'status': _status_payload(),
        'health': run_healthcheck(log_on_success=False),
        'safe_config': _safe_config(),
        'safe_env': _safe_env_lines(),
        'events': (read_state().get('events') or [])[-50:],
        'session_stats': session_store.stats(),
        'tasks': list_tasks(task_type='translation', limit=100),
        'bot_log_tail': _tail(LOG_FILE, 200),
        'cron_log_tail': _tail(CRON_LOG_FILE, 120),
        'blackseeds_preview': _blackseeds_lines(200),
        'preflight': _preflight_payload(),
    }


def _save_blackseeds(content: str) -> int:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    BLACKSEEDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    BLACKSEEDS_FILE.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
    record_event('blackseeds_updated', lines=len(lines))
    write_log(f'管理员更新了 blackseeds 规则，当前 {len(lines)} 条')
    return len(lines)


def _write_config_json(raw: str) -> None:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError('config.json 顶层必须是对象')
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding='utf-8')
    record_event('config_updated_from_panel')
    merge_state({'admin_hints': {'reload_required': True, 'last_config_save_at': datetime.now(TZ).isoformat()}})
    write_log('管理员通过面板更新了 config.json')


def _read_env_text() -> str:
    if not ENV_PATH.exists():
        return ''
    return ENV_PATH.read_text(encoding='utf-8', errors='ignore')


def _write_env_text(raw: str) -> None:
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' in line:
            lines.append(line.rstrip())
            continue
        raise ValueError(f'无效的 .env 行: {line}')
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text('\n'.join(lines).rstrip() + ('\n' if lines else ''), encoding='utf-8')
    record_event('env_updated_from_panel')
    merge_state({'admin_hints': {'reload_required': True, 'last_env_save_at': datetime.now(TZ).isoformat()}})
    write_log('管理员通过面板更新了 .env 文件')


def _safe_env_lines() -> list[str]:
    lines = []
    for line in _read_env_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in line:
            lines.append(line)
            continue
        key, value = line.split('=', 1)
        upper_key = key.strip().upper()
        if any(token in upper_key for token in ['TOKEN', 'KEY', 'PASSWORD', 'SECRET']):
            value = '***'
        lines.append(f'{key}={value}')
    return lines



def _safe_text(path: Path) -> str:
    if not path.exists():
        return ''
    return path.read_text(encoding='utf-8', errors='ignore')


def _diff_text(current: str, backup: str, current_name: str, backup_name: str) -> str:
    lines = difflib.unified_diff(
        current.splitlines(),
        backup.splitlines(),
        fromfile=current_name,
        tofile=backup_name,
        lineterm=''
    )
    return '\n'.join(lines) or '无差异'


def _backup_diff_payload(backup_id: str) -> dict[str, str]:
    base = _ensure_backup_dir() / backup_id
    if not base.exists():
        raise FileNotFoundError('备份不存在')
    return {
        'config_diff': _diff_text(_safe_text(CONFIG_PATH), _safe_text(base / 'config.json'), 'current/config.json', f'{backup_id}/config.json'),
        'env_diff': _diff_text(_safe_text(ENV_PATH), _safe_text(base / '.env'), 'current/.env', f'{backup_id}/.env'),
        'blackseeds_diff': _diff_text(_safe_text(BLACKSEEDS_FILE), _safe_text(base / 'blackseeds.txt'), 'current/blackseeds.txt', f'{backup_id}/blackseeds.txt'),
    }


def _build_support_bundle() -> io.BytesIO:
    payload = _diagnostics_payload()
    state = read_state()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('status.json', json.dumps(payload.get('status', {}), ensure_ascii=False, indent=2))
        zf.writestr('health.json', json.dumps(payload.get('health', {}), ensure_ascii=False, indent=2))
        zf.writestr('safe-config.json', json.dumps(payload.get('safe_config', {}), ensure_ascii=False, indent=2))
        zf.writestr('safe-env.txt', '\n'.join(payload.get('safe_env', [])) + '\n')
        zf.writestr('events.json', json.dumps(payload.get('events', []), ensure_ascii=False, indent=2))
        zf.writestr('session-stats.json', json.dumps(payload.get('session_stats', {}), ensure_ascii=False, indent=2))
        zf.writestr('tasks.json', json.dumps(payload.get('tasks', []), ensure_ascii=False, indent=2))
        zf.writestr('runtime-state.json', json.dumps(state, ensure_ascii=False, indent=2))
        zf.writestr('blackseeds.txt', '\n'.join(payload.get('blackseeds_preview', [])) + '\n')
        zf.writestr('logs/bot-tail.log', '\n'.join(payload.get('bot_log_tail', [])) + '\n')
        zf.writestr('logs/cron-tail.log', '\n'.join(payload.get('cron_log_tail', [])) + '\n')
    buf.seek(0)
    return buf


BACKUP_DIR = Path(os.getenv('BACKUP_DIR', CONFIG_PATH.parent / 'backups'))


def _ensure_backup_dir() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def _backup_file(path: Path, target: Path) -> None:
    if path.exists():
        target.write_text(path.read_text(encoding='utf-8', errors='ignore'), encoding='utf-8')


def _create_backup(note: str = '') -> str:
    ts = datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
    backup_id = f'backup-{ts}'
    base = _ensure_backup_dir() / backup_id
    base.mkdir(parents=True, exist_ok=True)
    _backup_file(CONFIG_PATH, base / 'config.json')
    _backup_file(ENV_PATH, base / '.env')
    _backup_file(BLACKSEEDS_FILE, base / 'blackseeds.txt')
    meta = {
        'id': backup_id,
        'created_at': datetime.now(TZ).isoformat(),
        'note': note.strip(),
        'files': [name for name in ['config.json', '.env', 'blackseeds.txt'] if (base / name).exists()],
    }
    (base / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    record_event('backup_created', backup_id=backup_id, files=meta['files'])
    write_log(f'管理员创建配置备份 {backup_id}')
    return backup_id


def _list_backups(limit: int = 20) -> list[dict[str, Any]]:
    base = _ensure_backup_dir()
    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        meta_file = child / 'meta.json'
        meta = {'id': child.name, 'created_at': '', 'note': '', 'files': []}
        if meta_file.exists():
            try:
                meta.update(json.loads(meta_file.read_text(encoding='utf-8')))
            except Exception:
                pass
        meta['id'] = child.name
        items.append(meta)
        if len(items) >= limit:
            break
    return items


def _restore_backup(backup_id: str) -> None:
    base = _ensure_backup_dir() / backup_id
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError('备份不存在')
    _backup_file(base / 'config.json', CONFIG_PATH)
    _backup_file(base / '.env', ENV_PATH)
    _backup_file(base / 'blackseeds.txt', BLACKSEEDS_FILE)
    record_event('backup_restored', backup_id=backup_id)
    merge_state({'admin_hints': {'reload_required': True, 'last_restore_at': datetime.now(TZ).isoformat()}})
    write_log(f'管理员恢复了配置备份 {backup_id}')


def _delete_backup(backup_id: str) -> None:
    import shutil
    base = _ensure_backup_dir() / backup_id
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError('备份不存在')
    shutil.rmtree(base)
    record_event('backup_deleted', backup_id=backup_id)
    write_log(f'管理员删除了配置备份 {backup_id}')


def _env_form_defaults() -> dict[str, str]:
    result = {
        'HTTP_PROXY': os.getenv('HTTP_PROXY', ''),
        'HTTPS_PROXY': os.getenv('HTTPS_PROXY', ''),
        'NO_PROXY': os.getenv('NO_PROXY', ''),
        'ADMIN_PANEL_TOKEN': os.getenv('ADMIN_PANEL_TOKEN', str(config.get('admin_panel_token', 'change-me'))),
        'ADMIN_PANEL_TITLE': os.getenv('ADMIN_PANEL_TITLE', str(config.get('admin_panel_title', TITLE))),
        'LOG_LEVEL': os.getenv('LOG_LEVEL', str(config.get('log_level', 'INFO'))),
        'PROXY_MODE': os.getenv('PROXY_MODE', 'true' if config.get('proxy_mode') else 'false'),
        'BOOTSTRAP_MODE': os.getenv('BOOTSTRAP_MODE', 'false'),
    }
    if ENV_PATH.exists():
        for line in _read_env_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            k = key.strip()
            if k in result:
                result[k] = value.strip()
    return result


def _save_env_from_form() -> None:
    env_values = {
        'HTTP_PROXY': (request.form.get('HTTP_PROXY') or '').strip(),
        'HTTPS_PROXY': (request.form.get('HTTPS_PROXY') or '').strip(),
        'NO_PROXY': (request.form.get('NO_PROXY') or '').strip(),
        'ADMIN_PANEL_TOKEN': (request.form.get('ADMIN_PANEL_TOKEN') or '').strip() or 'change-me',
        'ADMIN_PANEL_TITLE': (request.form.get('ADMIN_PANEL_TITLE') or '').strip() or TITLE,
        'LOG_LEVEL': (request.form.get('LOG_LEVEL') or 'INFO').strip().upper(),
        'PROXY_MODE': 'true' if _parse_bool_form('PROXY_MODE') else 'false',
        'BOOTSTRAP_MODE': 'true' if _parse_bool_form('BOOTSTRAP_MODE') else 'false',
    }
    lines = ['# Generated by LyrebirdMS web panel']
    for k, v in env_values.items():
        if v:
            lines.append(f'{k}={v}')
    _write_env_text('\n'.join(lines) + '\n')


def _parse_bool_form(name: str) -> bool:
    return (request.form.get(name) or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int((value or '').strip())
    except Exception:
        return default


def _selected_ids(name: str) -> list[str]:
    values = request.form.getlist(name)
    result = []
    for value in values:
        item = (value or '').strip()
        if item and item not in result:
            result.append(item)
    return result


def _redirect_dashboard(section: str, **params) -> Any:
    values = {"section": section}
    values.update(params)
    return redirect(url_for('dashboard', **values))


def _config_tabs() -> list[dict[str, str]]:
    return [
        {"key": "wizard", "label": "安装向导"},
        {"key": "core", "label": "核心配置"},
        {"key": "services", "label": "服务与AI"},
        {"key": "env", "label": ".env / 代理"},
        {"key": "backups", "label": "备份恢复"},
        {"key": "advanced", "label": "高级编辑"},
    ]


def _wizard_steps_meta() -> list[dict[str, str]]:
    return [
        {"key": "telegram", "label": "1. Telegram"},
        {"key": "services", "label": "2. MovieServer / Emby"},
        {"key": "database", "label": "3. 数据库"},
        {"key": "network", "label": "4. 代理与网络"},
        {"key": "finish", "label": "5. 保存与验证"},
    ]


def _config_checklist() -> list[dict[str, Any]]:
    raw = _raw_config()
    proxy = raw.get("proxy") or {}
    checks = [
        {"label": "Telegram 鉴权", "ok": bool(raw.get("api_id") and raw.get("api_hash") and raw.get("bot_token")), "hint": "填写 api_id / api_hash / bot_token"},
        {"label": "MovieServer", "ok": bool(raw.get("mshost") and raw.get("mstoken")), "hint": "填写 MovieServer 地址和 token"},
        {"label": "Emby", "ok": bool(raw.get("emby_host") and raw.get("emby_api")), "hint": "填写 Emby 地址和 API Key"},
        {"label": "MySQL", "ok": bool(raw.get("host") and raw.get("user") and raw.get("database")), "hint": "确认 MySQL 主机、账号和库名"},
        {"label": "PostgreSQL", "ok": bool(raw.get("mspostgre_host") and raw.get("mspostgre_user") and raw.get("mspostgre_dbname")), "hint": "确认 PostgreSQL 主机、账号和库名"},
        {"label": "Telegram 代理", "ok": (not raw.get("proxy_mode")) or bool(proxy.get("hostname") and proxy.get("port")), "hint": "开启代理时请填写主机和端口"},
        {"label": "管理面板令牌", "ok": str(raw.get("admin_panel_token") or os.getenv("ADMIN_PANEL_TOKEN") or "") not in {"", "change-me"}, "hint": "改掉默认 change-me"},
        {"label": "日志目录", "ok": _writable_check(os.getenv('LOG_DIR', '/data/logs')).get('ok'), "hint": os.getenv('LOG_DIR', '/data/logs')},
        {"label": "运行目录", "ok": _writable_check(os.getenv('RUNTIME_DIR', '/data/runtime')).get('ok'), "hint": os.getenv('RUNTIME_DIR', '/data/runtime')},
    ]
    return checks


def _apply_builtin_compose_defaults() -> None:
    current = _raw_config()
    if not current:
        current = json.loads((Path(__file__).resolve().parent / 'config-example.json').read_text(encoding='utf-8'))
    current.setdefault('host', 'mysql')
    current.setdefault('port', 3306)
    current.setdefault('user', 'lyrebird')
    current.setdefault('password', 'lyrebird')
    current.setdefault('database', 'emby')
    current.setdefault('mspostgre_host', 'postgres')
    current.setdefault('mspostgre_port', 5432)
    current.setdefault('mspostgre_dbname', 'ms-bot')
    current.setdefault('mspostgre_user', 'lyrebird')
    current.setdefault('mspostgre_password', 'lyrebird')
    current.setdefault('admin_panel_enabled', True)
    current.setdefault('admin_panel_title', current.get('name', 'LyrebirdMS Bot') + ' 管理面板')
    if 'proxy_mode' not in current:
        current['proxy_mode'] = True
    _write_config_json(json.dumps(current, ensure_ascii=False, indent=2))


def _setup_form_defaults() -> dict[str, Any]:
    raw = _raw_config()
    proxy = raw.get('proxy') or {}
    admins = raw.get('admin') or []
    return {
        'name': raw.get('name', ''),
        'coinsname': raw.get('coinsname', '积分'),
        'api_id': raw.get('api_id', ''),
        'api_hash': raw.get('api_hash', ''),
        'bot_token': raw.get('bot_token', ''),
        'owner': raw.get('owner', ''),
        'admin': ','.join(str(x) for x in admins),
        'mshost': raw.get('mshost', ''),
        'mstoken': raw.get('mstoken', ''),
        'emby_host': raw.get('emby_host', ''),
        'emby_api': raw.get('emby_api', ''),
        'accountbot': raw.get('accountbot', ''),
        'group': raw.get('group', ''),
        'proxy_mode': bool(raw.get('proxy_mode', True)),
        'proxy_scheme': proxy.get('scheme', 'http'),
        'proxy_hostname': proxy.get('hostname', ''),
        'proxy_port': proxy.get('port', 7890),
        'proxy_username': proxy.get('username', ''),
        'proxy_password': proxy.get('password', ''),
        'use_builtin_db': str(raw.get('host', 'mysql')) in {'mysql', ''} and str(raw.get('mspostgre_host', 'postgres')) in {'postgres', ''},
    }


def _full_config_defaults() -> dict[str, Any]:
    raw = _raw_config()
    if not raw:
        try:
            raw = json.loads((Path(__file__).resolve().parent / 'config-example.json').read_text(encoding='utf-8'))
        except Exception:
            raw = {}
    proxy = raw.get('proxy') or {}
    admins = raw.get('admin') or []
    return {
        'name': raw.get('name', ''),
        'coinsname': raw.get('coinsname', '积分'),
        'coins_per_1GB': raw.get('coins_per_1GB', 1),
        'api_id': raw.get('api_id', ''),
        'api_hash': raw.get('api_hash', ''),
        'bot_token': raw.get('bot_token', ''),
        'group': raw.get('group', ''),
        'owner': raw.get('owner', ''),
        'admin': ','.join(str(x) for x in admins),
        'host': raw.get('host', 'mysql'),
        'port': raw.get('port', 3306),
        'user': raw.get('user', 'lyrebird'),
        'password': raw.get('password', 'lyrebird'),
        'database': raw.get('database', 'emby'),
        'mspostgre_host': raw.get('mspostgre_host', 'postgres'),
        'mspostgre_port': raw.get('mspostgre_port', 5432),
        'mspostgre_dbname': raw.get('mspostgre_dbname', 'ms-bot'),
        'mspostgre_user': raw.get('mspostgre_user', 'lyrebird'),
        'mspostgre_password': raw.get('mspostgre_password', 'lyrebird'),
        'mshost': raw.get('mshost', ''),
        'msuser': raw.get('msuser', ''),
        'mspwd': raw.get('mspwd', ''),
        'mstoken': raw.get('mstoken', ''),
        'emby_host': raw.get('emby_host', ''),
        'emby_api': raw.get('emby_api', ''),
        'accountbot': raw.get('accountbot', ''),
        'search_timeout': raw.get('search_timeout', 180),
        'request_timeout': raw.get('request_timeout', 30),
        'request_retries': raw.get('request_retries', 2),
        'translation_enabled': bool(raw.get('translation_enabled', True)),
        'transfer_notice_enabled': bool(raw.get('transfer_notice_enabled', True)),
        'tmdb_bg_enabled': bool(raw.get('tmdb_bg_enabled', False)),
        'tmdb_api_key': raw.get('tmdb_api_key', ''),
        'StrmAssistant_ScanSubtitle': raw.get('StrmAssistant_ScanSubtitle', ''),
        'gemini_gst_batchsize': raw.get('gemini_gst_batchsize', 300),
        'ai_provider': raw.get('ai_provider', 'gemini'),
        'gemini_model': raw.get('gemini_model', 'gemini-2.5-flash'),
        'gemini_api_key': raw.get('gemini_api_key', ''),
        'ai_base_url': raw.get('ai_base_url', ''),
        'ai_api_key': raw.get('ai_api_key', ''),
        'ai_model': raw.get('ai_model', 'gpt-4o-mini'),
        'ai_chunk_chars': raw.get('ai_chunk_chars', 2400),
        'proxy_mode': bool(raw.get('proxy_mode', True)),
        'proxy_scheme': proxy.get('scheme', 'http'),
        'proxy_hostname': proxy.get('hostname', ''),
        'proxy_port': proxy.get('port', 7890),
        'proxy_username': proxy.get('username', ''),
        'proxy_password': proxy.get('password', ''),
        'admin_panel_enabled': bool(raw.get('admin_panel_enabled', True)),
        'admin_panel_token': raw.get('admin_panel_token', 'change-me'),
        'admin_panel_title': raw.get('admin_panel_title', raw.get('name', 'LyrebirdMS Bot') + ' 管理面板'),
    }


def _save_full_config_from_form() -> None:
    current = _raw_config()
    if not current:
        current = json.loads((Path(__file__).resolve().parent / 'config-example.json').read_text(encoding='utf-8'))
    admins_raw = (request.form.get('admin') or '').replace('，', ',')
    admins = [int(item.strip()) for item in admins_raw.split(',') if item.strip().isdigit()]
    current.update({
        'name': (request.form.get('name') or '').strip(),
        'coinsname': (request.form.get('coinsname') or '积分').strip(),
        'coins_per_1GB': _parse_int(request.form.get('coins_per_1GB') or '1', 1),
        'api_id': _parse_int(request.form.get('api_id') or '0', 0),
        'api_hash': (request.form.get('api_hash') or '').strip(),
        'bot_token': (request.form.get('bot_token') or '').strip(),
        'group': _parse_int(request.form.get('group') or '0', 0),
        'owner': _parse_int(request.form.get('owner') or '0', 0),
        'admin': admins,
        'host': (request.form.get('host') or 'mysql').strip(),
        'port': _parse_int(request.form.get('port') or '3306', 3306),
        'user': (request.form.get('user') or '').strip(),
        'password': (request.form.get('password') or '').strip(),
        'database': (request.form.get('database') or '').strip(),
        'mspostgre_host': (request.form.get('mspostgre_host') or 'postgres').strip(),
        'mspostgre_port': _parse_int(request.form.get('mspostgre_port') or '5432', 5432),
        'mspostgre_dbname': (request.form.get('mspostgre_dbname') or '').strip(),
        'mspostgre_user': (request.form.get('mspostgre_user') or '').strip(),
        'mspostgre_password': (request.form.get('mspostgre_password') or '').strip(),
        'mshost': (request.form.get('mshost') or '').strip(),
        'msuser': (request.form.get('msuser') or '').strip(),
        'mspwd': (request.form.get('mspwd') or '').strip(),
        'mstoken': (request.form.get('mstoken') or '').strip(),
        'emby_host': (request.form.get('emby_host') or '').strip(),
        'emby_api': (request.form.get('emby_api') or '').strip(),
        'accountbot': (request.form.get('accountbot') or '').strip(),
        'search_timeout': _parse_int(request.form.get('search_timeout') or '180', 180),
        'request_timeout': _parse_int(request.form.get('request_timeout') or '30', 30),
        'request_retries': _parse_int(request.form.get('request_retries') or '2', 2),
        'translation_enabled': _parse_bool_form('translation_enabled'),
        'transfer_notice_enabled': _parse_bool_form('transfer_notice_enabled'),
        'tmdb_bg_enabled': _parse_bool_form('tmdb_bg_enabled'),
        'tmdb_api_key': (request.form.get('tmdb_api_key') or '').strip(),
        'StrmAssistant_ScanSubtitle': (request.form.get('StrmAssistant_ScanSubtitle') or '').strip(),
        'gemini_gst_batchsize': _parse_int(request.form.get('gemini_gst_batchsize') or '300', 300),
        'ai_provider': (request.form.get('ai_provider') or 'gemini').strip(),
        'gemini_model': (request.form.get('gemini_model') or '').strip(),
        'gemini_api_key': (request.form.get('gemini_api_key') or '').strip(),
        'ai_base_url': (request.form.get('ai_base_url') or '').strip(),
        'ai_api_key': (request.form.get('ai_api_key') or '').strip(),
        'ai_model': (request.form.get('ai_model') or '').strip(),
        'ai_chunk_chars': _parse_int(request.form.get('ai_chunk_chars') or '2400', 2400),
        'proxy_mode': _parse_bool_form('proxy_mode'),
        'admin_panel_enabled': _parse_bool_form('admin_panel_enabled'),
        'admin_panel_token': (request.form.get('admin_panel_token') or 'change-me').strip(),
        'admin_panel_title': (request.form.get('admin_panel_title') or '').strip(),
        'proxy': {
            'scheme': (request.form.get('proxy_scheme') or 'http').strip(),
            'hostname': (request.form.get('proxy_hostname') or '').strip(),
            'port': _parse_int(request.form.get('proxy_port') or '7890', 7890),
            'username': (request.form.get('proxy_username') or '').strip(),
            'password': (request.form.get('proxy_password') or '').strip(),
        },
    })
    _write_config_json(json.dumps(current, ensure_ascii=False, indent=2))


def _service_statuses() -> list[dict[str, Any]]:
    health = run_healthcheck(log_on_success=False)
    mapping = [
        ('mysql', 'MySQL'),
        ('postgres', 'PostgreSQL'),
        ('redis', 'Redis'),
        ('movieserver', 'MovieServer'),
        ('emby', 'Emby'),
        ('ai', 'AI Provider'),
    ]
    rows = []
    for key, label in mapping:
        item = health.get(key) or {}
        rows.append({
            'key': key,
            'label': label,
            'ok': bool(item.get('ok', False)),
            'enabled': item.get('enabled', True),
            'detail': item.get('error') or item.get('status_code') or item.get('provider') or item.get('model') or '',
            'action_url': url_for('service_check') if 'service_check' in globals() else '/services/check',
        })
    return rows


def _setup_step_fields():
    return [
        ('step1', 'Telegram / 基础', ['name', 'coinsname', 'api_id', 'api_hash', 'bot_token', 'owner', 'admin']),
        ('step2', '媒体服务', ['mshost', 'mstoken', 'emby_host', 'emby_api', 'accountbot', 'group']),
        ('step3', '代理与数据库', ['use_builtin_db', 'proxy_mode', 'proxy_scheme', 'proxy_hostname', 'proxy_port', 'proxy_username', 'proxy_password']),
    ]


def _save_setup_from_form() -> None:
    current = _raw_config()
    if not current:
        current = json.loads((Path(__file__).resolve().parent / 'config-example.json').read_text(encoding='utf-8'))
    proxy_mode = _parse_bool_form('proxy_mode')
    use_builtin_db = _parse_bool_form('use_builtin_db')
    admins_raw = (request.form.get('admin') or '').replace('，', ',')
    admins = [int(item.strip()) for item in admins_raw.split(',') if item.strip().isdigit()]
    current.update({
        'name': (request.form.get('name') or current.get('name') or 'LyrebirdMS Bot').strip(),
        'coinsname': (request.form.get('coinsname') or current.get('coinsname') or '积分').strip(),
        'api_id': _parse_int(request.form.get('api_id') or str(current.get('api_id') or ''), current.get('api_id', 0) or 0),
        'api_hash': (request.form.get('api_hash') or current.get('api_hash') or '').strip(),
        'bot_token': (request.form.get('bot_token') or current.get('bot_token') or '').strip(),
        'owner': _parse_int(request.form.get('owner') or str(current.get('owner') or ''), current.get('owner', 0) or 0),
        'admin': admins or current.get('admin') or [],
        'mshost': (request.form.get('mshost') or current.get('mshost') or '').strip(),
        'mstoken': (request.form.get('mstoken') or current.get('mstoken') or '').strip(),
        'emby_host': (request.form.get('emby_host') or current.get('emby_host') or '').strip(),
        'emby_api': (request.form.get('emby_api') or current.get('emby_api') or '').strip(),
        'accountbot': (request.form.get('accountbot') or current.get('accountbot') or '').strip(),
        'group': _parse_int(request.form.get('group') or str(current.get('group') or ''), current.get('group', 0) or 0),
        'proxy_mode': proxy_mode,
        'proxy': {
            'scheme': (request.form.get('proxy_scheme') or 'http').strip() or 'http',
            'hostname': (request.form.get('proxy_hostname') or '').strip(),
            'port': _parse_int(request.form.get('proxy_port') or '7890', 7890),
            'username': (request.form.get('proxy_username') or '').strip(),
            'password': (request.form.get('proxy_password') or '').strip(),
        },
    })
    if use_builtin_db:
        current.update({
            'host': 'mysql', 'port': 3306, 'user': 'lyrebird', 'password': 'lyrebird', 'database': 'emby',
            'mspostgre_host': 'postgres', 'mspostgre_port': 5432, 'mspostgre_dbname': 'ms-bot', 'mspostgre_user': 'lyrebird', 'mspostgre_password': 'lyrebird',
        })
    _write_config_json(json.dumps(current, ensure_ascii=False, indent=2))
    record_event('setup_form_saved', use_builtin_db=use_builtin_db, proxy_mode=proxy_mode)


LOGIN_TMPL = '''
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{{ title }}</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}.card{width:360px;background:#111827;padding:28px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.35)}input{width:100%;padding:12px;border-radius:10px;border:1px solid #374151;background:#0b1220;color:#fff;margin:10px 0 16px}button{width:100%;padding:12px;border:0;border-radius:10px;background:#2563eb;color:#fff;font-weight:600;cursor:pointer}.small{font-size:12px;color:#94a3b8}.err{color:#fda4af;margin-bottom:12px}</style></head><body><div class="card"><h2>{{ title }}</h2><p class="small">请输入管理员访问令牌</p>{% if error %}<div class="err">{{ error }}</div>{% endif %}<form method="post"><input type="password" name="token" placeholder="Admin Token"><button type="submit">登录</button></form>{% if weak %}<p class="small">当前仍使用默认令牌，建议尽快在 .env 中修改 ADMIN_PANEL_TOKEN。</p>{% endif %}</div></body></html>'''



DETAIL_TMPL = '''
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{{ title }}</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b1220;color:#dbeafe;margin:0}header{padding:18px 24px;background:#111827;border-bottom:1px solid #1f2937}.container{padding:24px;max-width:1100px;margin:0 auto}.card{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:18px;margin-bottom:16px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px;border-bottom:1px solid #1f2937;text-align:left;vertical-align:top}th{color:#93c5fd}a{color:#93c5fd;text-decoration:none}.muted{color:#94a3b8}pre{white-space:pre-wrap;word-break:break-word;background:#020617;color:#d1fae5;padding:14px;border-radius:12px;overflow:auto}input,select{padding:10px;border-radius:10px;border:1px solid #334155;background:#020617;color:#fff;box-sizing:border-box;width:100%}button{padding:10px 14px;border:0;border-radius:10px;background:#2563eb;color:#fff;cursor:pointer}.danger{background:#dc2626}</style></head><body>
<header><a href="{{ url_for('dashboard') }}">← 返回首页</a></header>
<div class="container">
<div class="card"><h2 style="margin-top:0">{{ heading }}</h2><pre>{{ detail_json }}</pre></div>
{% if user_form %}<div class="card"><h3 style="margin-top:0">管理员操作</h3><form method="post" action="{{ admin_action_url }}"><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px"><div><label>积分增减</label><input type="number" name="coins_delta" value="0"></div><div><label>免费额度增减</label><input type="number" name="free_delta" value="0"></div><div><label>账号状态</label><select name="level"><option value="">不修改</option><option value="a">a 白名单</option><option value="b">b 普通</option><option value="d">d 禁用</option></select></div></div><div style="margin-top:12px"><button type="submit">保存用户调整</button></div></form></div>{% endif %}
{% if task_actions %}<div class="card"><h3 style="margin-top:0">任务操作</h3><div style="display:flex;gap:12px;flex-wrap:wrap">{% if task_actions.retry_url %}<form method="post" action="{{ task_actions.retry_url }}"><button type="submit">请求重试</button></form>{% endif %}{% if task_actions.delete_url %}<form method="post" action="{{ task_actions.delete_url }}"><button type="submit" class="danger">删除记录</button></form>{% endif %}</div><p class="muted">重试会由 Bot 后台轮询执行；如果外部服务仍不可用，任务会再次进入失败状态。</p></div>{% endif %}
{% if rows is not none %}<div class="card"><h3 style="margin-top:0">关联记录</h3><table><thead><tr>{% for col in columns %}<th>{{ col }}</th>{% endfor %}</tr></thead><tbody>{% for row in rows %}<tr>{% for col in columns %}<td>{% if col == 'torrent_id' and row.get(col) %}<a href="{{ url_for('download_detail', torrent_id=row.get(col)) }}">{{ row.get(col, '') }}</a>{% else %}{{ row.get(col, '') }}{% endif %}</td>{% endfor %}</tr>{% endfor %}</tbody></table></div>{% endif %}
</div></body></html>'''

DASHBOARD_TMPL = '''
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{{ title }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#111827">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="mobile-web-app-capable" content="yes">
<meta name="format-detection" content="telephone=no">
<link rel="manifest" href="{{ url_for('manifest_webmanifest') }}">
<link rel="icon" href="{{ url_for('pwa_icon') }}" type="image/svg+xml">
<link rel="apple-touch-icon" href="{{ url_for('pwa_icon') }}">
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#111827;--panel-2:#020617;--line:#1f2937;--line-2:#334155;--text:#dbeafe;--muted:#94a3b8;--brand:#2563eb;--brand-2:#1d4ed8;--danger:#dc2626;--ok:#86efac;--warn:#fdba74;--shadow:0 10px 30px rgba(0,0,0,.22)}
html{scroll-behavior:smooth}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);margin:0;padding-bottom:92px}
header{padding:18px 24px;background:rgba(17,24,39,.92);backdrop-filter:blur(12px);position:sticky;top:0;border-bottom:1px solid var(--line);z-index:30}
.container{padding:20px;max-width:1500px;margin:0 auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:var(--shadow)}.wide{grid-column:1 / -1}
.muted{color:var(--muted)}.success{color:var(--ok)}.danger{color:#fca5a5}.warn-text{color:var(--warn)}.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.row-between{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center}
.badge{display:inline-flex;align-items:center;justify-content:center;padding:8px 12px;border-radius:999px;font-size:12px;font-weight:700;min-height:34px}.ok{background:#052e16;color:var(--ok)}.warn{background:#3f1d0d;color:var(--warn)}.bad{background:#450a0a;color:#fca5a5}
pre,textarea{white-space:pre-wrap;word-break:break-word;background:var(--panel-2);color:#d1fae5;padding:14px;border-radius:12px;max-height:420px;overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:#93c5fd}
input,select,textarea{padding:12px;border-radius:12px;border:1px solid var(--line-2);background:var(--panel-2);color:#fff;box-sizing:border-box}input[type=text],input[type=password],input[type=number],input[type=file]{width:100%}textarea{width:100%;min-height:180px}
button,.btn-link{padding:12px 14px;border:0;border-radius:12px;background:var(--brand);color:#fff;cursor:pointer;min-height:44px;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}.btn-link.secondary,button.secondary{background:#334155}button.danger,.btn-link.danger{background:var(--danger)}
a{color:#93c5fd;text-decoration:none}.tiny{font-size:12px}.callout{padding:14px;border-radius:14px;background:#172554;border:1px solid #1d4ed8}.warning{background:#3f1d0d;border-color:#ea580c}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.kpi{background:var(--panel-2);border:1px solid #1e293b;border-radius:14px;padding:14px}.kpi .label{color:#93c5fd;font-size:12px}.kpi .value{font-size:28px;font-weight:700;margin-top:8px}.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.form-grid .span2{grid-column:span 2}small{color:var(--muted)}
.top-actions a,.top-actions button{padding:10px 12px;border-radius:12px;background:#0f172a;border:1px solid var(--line);color:#cbd5e1}.sticky-nav{position:sticky;top:74px;z-index:20}.mobile-nav{position:fixed;left:0;right:0;bottom:0;padding:10px 12px calc(10px + env(safe-area-inset-bottom));background:rgba(2,6,23,.96);backdrop-filter:blur(12px);border-top:1px solid var(--line);display:none;z-index:40}.mobile-nav .nav-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.mobile-nav a{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:10px 6px;border-radius:14px;background:#0f172a;color:#cbd5e1;font-size:12px;min-height:54px}.mobile-nav a.active{background:var(--brand-2);color:#fff}.pwa-banner{display:flex;gap:12px;align-items:center;justify-content:space-between;margin:0 0 16px}.pill{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;background:#0f172a;border:1px solid var(--line);color:#cbd5e1}.toolbar{display:flex;gap:10px;flex-wrap:wrap}.section-title{scroll-margin-top:110px}.fab{position:fixed;right:16px;bottom:92px;z-index:45;display:flex;flex-direction:column;gap:10px}.fab a,.fab button{width:54px;height:54px;border-radius:999px;background:var(--brand);box-shadow:var(--shadow);border:0;color:#fff;font-size:22px}.toast-stack{position:fixed;top:82px;right:16px;z-index:60;display:flex;flex-direction:column;gap:10px;max-width:360px}.toast{padding:12px 14px;border-radius:14px;border:1px solid var(--line);background:#0f172a;box-shadow:var(--shadow)}.toast.success{border-color:#166534}.toast.error{border-color:#991b1b}.toast.warning{border-color:#b45309}.wizard-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.install-sheet{display:none;position:fixed;left:0;right:0;bottom:78px;z-index:50;padding:0 14px}.install-sheet .inner{background:#111827;border:1px solid var(--line);box-shadow:var(--shadow);border-radius:18px;padding:16px}.seg{display:inline-flex;background:#0f172a;border:1px solid var(--line);border-radius:999px;padding:4px;gap:4px;overflow:auto}.seg a{padding:8px 12px;border-radius:999px;color:#cbd5e1;white-space:nowrap}.seg a.active{background:var(--brand-2);color:#fff}.confirm-note{font-size:12px;color:var(--warn)}
@media (max-width: 860px){header{padding:14px 16px}.container{padding:14px}.grid{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}.form-grid .span2{grid-column:span 1}.top-actions{display:none}.sticky-nav{top:62px}.mobile-nav{display:block}table{display:block;overflow-x:auto;white-space:nowrap}.kpi .value{font-size:24px}.card{padding:16px}.badge{padding:8px 10px}.toast-stack{left:14px;right:14px;top:72px;max-width:none}.fab{right:14px;bottom:88px}}
</style></head><body>
<header id="top"><div class="row-between"><div><h2 style="margin:0">{{ title }}</h2><div class="muted">NAS 管理台 / Bot + 面板同容器 / 开箱即用优先</div></div><div class="row top-actions"><a href="{{ url_for('dashboard') }}">首页</a><a href="{{ url_for('api_status') }}">状态 JSON</a><a href="{{ url_for('api_health') }}">健康 JSON</a><a href="{{ url_for('api_preflight') }}">部署检查</a><a href="{{ url_for('download_diagnostics') }}">诊断包 JSON</a><a href="{{ # }}">导出支持包</a><button id="theme-toggle" type="button" class="secondary">切换主题</button><label class="pill" style="cursor:pointer"><input id="auto-refresh" type="checkbox" style="width:auto;margin:0 6px 0 0">自动刷新</label><button id="install-app-btn" type="button" class="secondary" style="display:none">安装到主屏幕</button><a href="{{ url_for('logout') }}">退出</a></div></div></header>
<div class="container">
{% if flashes %}<div class="toast-stack">{% for category, message in flashes %}<div class="toast {{ category }}">{{ message }}</div>{% endfor %}</div>{% endif %}
<div id="install-sheet" class="install-sheet"><div class="inner"><div class="row-between"><div><b>安装到手机主屏幕</b><div class="muted tiny">安装后可像独立应用一样打开，移动端操作更顺手。</div></div><button type="button" class="secondary" id="close-install-sheet">稍后</button></div><div class="row" style="margin-top:12px"><button id="install-app-btn-sheet" type="button">立即安装</button><a class="btn-link secondary" href="#config-center">先去配置</a></div></div></div>
<div class="card wide pwa-banner"><div class="row"><span class="pill">📱 已适配移动端</span><span id="net-status" class="pill">网络检测中…</span><span class="pill">PWA 可安装</span><span class="pill">支持包导出</span></div><div class="toolbar"><a class="btn-link secondary" href="#config-center">快速到配置</a><a class="btn-link secondary" href="#task-ops">快速到任务</a><a class="btn-link secondary" href="{{ # }}">导出支持包</a></div></div>
<div class="card wide sticky-nav"><div class="seg">
  <a class="badge {{ 'ok' if section=='overview' else 'warn' }}" href="{{ url_for('dashboard', section='overview') }}">总览</a>
  <a class="badge {{ 'ok' if section=='downloads' else 'warn' }}" href="{{ url_for('dashboard', section='downloads', dq=dq, dts=dts) }}">下载与任务</a>
  <a class="badge {{ 'ok' if section=='users' else 'warn' }}" href="{{ url_for('dashboard', section='users', uq=uq) }}">用户</a>
  <a class="badge {{ 'ok' if section=='config' else 'warn' }}" href="{{ url_for('dashboard', section='config', config_tab=config_tab) }}">配置中心</a>
  <a class="badge {{ 'ok' if section=='logs' else 'warn' }}" href="{{ url_for('dashboard', section='logs') }}">日志与诊断</a>
</div></div>
<div class="fab"><a href="#top" title="回到顶部">↑</a><button type="button" id="refresh-btn" title="刷新状态">⟳</button></div>
{% if hints.reload_required %}<div class="card wide"><div class="callout warning"><b>提示：</b>你最近已经修改过 config.json。为了让 Bot 进程完整加载新配置，建议保存后在 NAS 里重启一次容器。</div></div>{% endif %}
<div class="grid">
{% if section=='overview' %}<div class="card wide">
  <div class="row-between"><h3 style="margin:0">总览</h3><div class="row"><span class="badge {{ 'ok' if status.bot_status == 'running' and not status.bot_stale else 'warn' if status.bot_status == 'starting' else 'bad' }}">{{ status.bot_status }}</span><span class="muted">心跳：{{ status.bot_heartbeat or '暂无' }}</span></div></div>
  <div class="kpis">
    <div class="kpi"><div class="label">24h 下载</div><div class="value">{{ status.download_stats.last_24h_downloads }}</div></div>
    <div class="kpi"><div class="label">累计下载</div><div class="value">{{ status.download_stats.total_downloads }}</div></div>
    <div class="kpi"><div class="label">用户总数</div><div class="value">{{ status.user_stats.total_users }}</div></div>
    <div class="kpi"><div class="label">翻译任务</div><div class="value">{{ status.task_stats.total }}</div></div>
    <div class="kpi"><div class="label">会话缓存</div><div class="value">{{ status.session_stats.keys }}</div></div>
  </div>
</div>{% endif %}

{% if section=='overview' and (setup.bootstrap_mode or setup.health.missing or not setup.health.ok) %}
<div class="card wide"><h3>首次启动 / 配置引导</h3><div class="callout warning"><div class="row"><div class="badge {{ 'bad' if setup.bootstrap_mode else 'warn' }}">{{ '引导模式' if setup.bootstrap_mode else '待完善' }}</div><div class="muted">config 路径：{{ config_path }}</div></div><ul>{% for item in setup.actions %}<li>{{ item }}</li>{% endfor %}</ul></div></div>
<div class="card wide"><h3>部署前检查</h3><div class="grid">{% for item in preflight.checks %}<div class="card" style="padding:14px"><div class="row-between"><b>{{ item.label }}</b><span class="badge {{ 'ok' if item.ok else 'bad' }}">{{ '通过' if item.ok else '待处理' }}</span></div><div class="muted tiny" style="margin-top:8px">{{ item.hint }}</div></div>{% endfor %}</div>{% if preflight.warnings %}<div class="callout warning" style="margin-top:12px"><ul>{% for item in preflight.warnings %}<li>{{ item }}</li>{% endfor %}</ul></div>{% endif %}</div>
{% endif %}

{% if section in ['overview','config'] %}<div class="card wide">
  <div class="row-between"><h3 style="margin:0">快速配置向导</h3><small>适合第一次部署或小白用户，填完后保存并重启容器</small></div><div class="row" style="margin:10px 0 14px">{% for step_id, step_name, fields in setup_steps %}<span class="badge warn">{{ loop.index }}. {{ step_name }}</span>{% endfor %}</div>
  <form method="post" action="{{ url_for('save_setup_form') }}">
    <div class="form-grid">
      <div><label>机器人名称</label><input type="text" name="name" value="{{ setup_form.name }}"></div>
      <div><label>积分名称</label><input type="text" name="coinsname" value="{{ setup_form.coinsname }}"></div>
      <div><label>Telegram api_id</label><input type="number" name="api_id" value="{{ setup_form.api_id }}"></div>
      <div><label>Telegram api_hash</label><input type="text" name="api_hash" value="{{ setup_form.api_hash }}"></div>
      <div class="span2"><label>Telegram bot_token</label><input type="text" name="bot_token" value="{{ setup_form.bot_token }}"></div>
      <div><label>Owner 用户ID</label><input type="number" name="owner" value="{{ setup_form.owner }}"></div>
      <div><label>Admin 列表（逗号分隔）</label><input type="text" name="admin" value="{{ setup_form.admin }}"></div>
      <div><label>MovieServer 地址</label><input type="text" name="mshost" value="{{ setup_form.mshost }}"></div>
      <div><label>MovieServer Token</label><input type="text" name="mstoken" value="{{ setup_form.mstoken }}"></div>
      <div><label>Emby 地址</label><input type="text" name="emby_host" value="{{ setup_form.emby_host }}"></div>
      <div><label>Emby API Key</label><input type="text" name="emby_api" value="{{ setup_form.emby_api }}"></div>
      <div><label>账号机器人链接</label><input type="text" name="accountbot" value="{{ setup_form.accountbot }}"></div>
      <div><label>群组ID（可选）</label><input type="number" name="group" value="{{ setup_form.group }}"></div>
      <div class="span2 row"><label><input type="checkbox" name="use_builtin_db" value="1" {% if setup_form.use_builtin_db %}checked{% endif %}> 使用 Compose 内置 MySQL / PostgreSQL 默认配置</label><label><input type="checkbox" name="proxy_mode" value="1" {% if setup_form.proxy_mode %}checked{% endif %}> Telegram 启用代理</label></div>
      <div><label>代理协议</label><select name="proxy_scheme"><option value="http" {% if setup_form.proxy_scheme=='http' %}selected{% endif %}>http</option><option value="socks5" {% if setup_form.proxy_scheme=='socks5' %}selected{% endif %}>socks5</option></select></div>
      <div><label>代理主机</label><input type="text" name="proxy_hostname" value="{{ setup_form.proxy_hostname }}"></div>
      <div><label>代理端口</label><input type="number" name="proxy_port" value="{{ setup_form.proxy_port }}"></div>
      <div><label>代理用户名（可选）</label><input type="text" name="proxy_username" value="{{ setup_form.proxy_username }}"></div>
      <div class="span2"><label>代理密码（可选）</label><input type="password" name="proxy_password" value="{{ setup_form.proxy_password }}"></div>
    </div>
    <div class="row" style="margin-top:14px"><button type="submit">保存向导配置</button><small>保存后会覆盖磁盘上的 config.json，但不会立即热重载 Bot。</small></div>
  </form>
</div>{% endif %}

{% if section in ['overview','logs'] %}<div class="card"><h3>服务健康</h3><pre>{{ health_json }}</pre></div>
<div class="card"><h3>运行事件</h3><pre>{{ events_text }}</pre></div>{% endif %}

{% if section in ['overview','config'] %}<div class="card">
  <h3>功能开关</h3>
  <form method="post" action="{{ url_for('update_features') }}">
    <div class="row"><label><input type="checkbox" name="translation_enabled" {% if status.translation_enabled %}checked{% endif %}> 启用翻译</label></div>
    <div class="row"><label><input type="checkbox" name="transfer_notice_enabled" {% if status.transfer_notice_enabled %}checked{% endif %}> 启用入库通知</label></div>
    <div class="row"><label><input type="checkbox" name="tmdb_bg_enabled" {% if status.tmdb_bg_enabled %}checked{% endif %}> 启用 TMDB 背景图</label></div>
    <div><label>日志等级</label><select name="log_level"><option {% if safe_config.log_level=='DEBUG' %}selected{% endif %}>DEBUG</option><option {% if safe_config.log_level=='INFO' %}selected{% endif %}>INFO</option><option {% if safe_config.log_level=='WARNING' %}selected{% endif %}>WARNING</option><option {% if safe_config.log_level=='ERROR' %}selected{% endif %}>ERROR</option></select></div>
    <div class="row" style="margin-top:14px"><button type="submit">保存开关</button></div>
  </form>
</div>

<div class="card">
  <h3>AI 设置</h3>
  <form method="post" action="{{ url_for('update_ai_settings') }}">
    <div><label>Provider</label><select name="ai_provider"><option value="gemini" {% if safe_config.ai_provider=='gemini' %}selected{% endif %}>gemini</option><option value="gemini_api" {% if safe_config.ai_provider=='gemini_api' %}selected{% endif %}>gemini_api</option><option value="openai_compatible" {% if safe_config.ai_provider=='openai_compatible' %}selected{% endif %}>openai_compatible</option></select></div>
    <div><label>Gemini Model</label><input type="text" name="gemini_model" value="{{ safe_config.gemini_model or '' }}"></div>
    <div><label>AI Model</label><input type="text" name="ai_model" value="{{ safe_config.ai_model or '' }}"></div>
    <div><label>AI Base URL</label><input type="text" name="ai_base_url" value="{{ safe_config.ai_base_url or '' }}"></div>
    <div><label>Chunk Size</label><input type="number" name="ai_chunk_chars" value="{{ safe_config.ai_chunk_chars or 2400 }}"></div>
    <div class="row" style="margin-top:14px"><button type="submit">保存 AI 设置</button></div>
  </form>
</div>

<div class="card">
  <h3>会话缓存</h3>
  <div class="muted">后端：{{ status.session_stats.backend }} / keys：{{ status.session_stats.keys }}</div>
  <form method="post" action="{{ url_for('clear_sessions') }}" style="margin-top:14px"><button class="danger" data-confirm="确认清空全部会话缓存？" type="submit">清空会话缓存</button></form>
</div>{% endif %}

{% if section in ['downloads','overview'] %}<div class="card wide">
  <div class="row-between"><h3 style="margin:0">下载记录</h3><form method="get" class="row"><input type="text" name="dq" value="{{ dq }}" placeholder="标题 / torrent_id / 用户ID"><button type="submit">搜索</button></form></div>
  <table><thead><tr><th>时间</th><th>标题</th><th>用户</th><th>扣费</th><th>大小</th><th>TMDB</th></tr></thead><tbody>{% for item in downloads %}<tr><td>{{ item.date }}</td><td><a href="{{ url_for('download_detail', torrent_id=item.torrent_id) }}">{{ item.title }}</a></td><td><a href="{{ url_for('user_detail', user_id=item.telegram_id) }}">{{ item.telegram_id }}</a></td><td>{{ item.cost_coins }}</td><td>{{ item.size }}</td><td>{{ item.tmdbid }}</td></tr>{% else %}<tr><td colspan="6" class="muted">暂无记录</td></tr>{% endfor %}</tbody></table>
</div>

<div id="task-ops" class="card wide section-title">
  <div class="row-between"><h3 style="margin:0">下载提交流水</h3><div class="row"><form method="post" action="{{ url_for('prune_download_tasks') }}"><button class="secondary" type="submit">清理旧记录</button></form></div></div>
  <div class="row" style="margin:12px 0"><form method="post" action="{{ url_for('retry_failed_download_tasks') }}"><button type="submit">批量重试失败下载</button></form></div>
  <form method="post" action="{{ url_for('bulk_task_action') }}">
  <input type="hidden" name="section" value="downloads">
  <div class="row" style="margin:8px 0 12px"><select name="action"><option value="retry">批量请求重试</option><option value="delete">批量删除记录</option></select><button class="secondary" type="submit">执行勾选项</button><small>建议只对 failed / success 记录操作。</small></div>
  <table><thead><tr><th><input type="checkbox" onclick="document.querySelectorAll('.download-task-checkbox').forEach(x=>x.checked=this.checked)"></th><th>更新时间</th><th>标题</th><th>状态</th><th>尝试</th><th>错误</th><th>用户</th><th>媒体ID</th><th>操作</th></tr></thead><tbody>{% for item in download_tasks %}<tr><td><input class="download-task-checkbox" type="checkbox" name="task_ids" value="{{ item.id }}"></td><td>{{ item.updated_at }}</td><td><a href="{{ url_for('task_detail', task_id=item.id) }}">{{ item.result.title or item.title }}</a></td><td>{{ item.status }}</td><td>{{ item.attempts }}</td><td style="max-width:320px">{{ item.error }}</td><td><a href="{{ url_for('user_detail', user_id=item.created_by or item.payload.user_id) }}">{{ item.created_by or item.payload.user_id }}</a></td><td>{{ item.payload.media_id }}</td><td><div class="row">{% if item.status in ['failed','retry_requested'] %}<form method="post" action="{{ url_for('retry_download_task', task_id=item.id) }}"><button type="submit">重试</button></form>{% endif %}{% if item.status in ['success','failed'] %}<form method="post" action="{{ url_for('delete_task_route', task_id=item.id) }}"><button class="secondary danger" data-confirm="确认删除这条记录？" type="submit">删除</button></form>{% endif %}</div></td></tr>{% else %}<tr><td colspan="9" class="muted">暂无下载提交流水</td></tr>{% endfor %}</tbody></table></form>
</div>{% endif %}

{% if section in ['users','overview'] %}<div class="card wide">
  <div class="row-between"><h3 style="margin:0">用户记录</h3><form method="get" class="row"><input type="text" name="uq" value="{{ uq }}" placeholder="用户ID / 名称"><button type="submit">搜索</button></form></div>
  <form method="post" action="{{ url_for('bulk_update_users') }}">
    <div class="row" style="margin:8px 0 12px"><input type="number" name="coins_delta" placeholder="积分增减，如 10 / -10"><input type="number" name="free_delta" placeholder="免费额度增减"><select name="level"><option value="">状态不变</option><option value="a">白名单</option><option value="b">普通</option><option value="d">禁用</option></select><button class="secondary" type="submit">批量更新勾选用户</button></div>
    <table><thead><tr><th><input type="checkbox" onclick="document.querySelectorAll('.user-checkbox').forEach(x=>x.checked=this.checked)"></th><th>用户ID</th><th>名称</th><th>状态</th><th>积分</th><th>免费额度</th></tr></thead><tbody>{% for item in users %}<tr><td><input class="user-checkbox" type="checkbox" name="user_ids" value="{{ item.tg or item.telegram_id or item.user_id or '' }}"></td><td><a href="{{ url_for('user_detail', user_id=item.tg or item.telegram_id or item.user_id or '') }}">{{ item.tg or item.telegram_id or item.user_id or '' }}</a></td><td>{{ item.name or item.telegram_name or item.username or '' }}</td><td>{{ item.lv or item.state or item.status or '' }}</td><td>{{ item.iv or item.coins or item.balance or '' }}</td><td>{{ item.free or item.permonthfree or '' }}</td></tr>{% else %}<tr><td colspan="6" class="muted">暂无记录</td></tr>{% endfor %}</tbody></table>
  </form>
</div>{% endif %}

{% if section in ['downloads','overview'] %}<div class="card wide">
  <div class="row-between"><h3 style="margin:0">翻译任务</h3><form method="get" class="row"><input type="text" name="tq" value="{{ tq }}" placeholder="任务标题 / 用户 / 媒体路径"><select name="ts"><option value="">全部状态</option>{% for opt in ['queued','running','success','failed','retry_requested'] %}<option value="{{ opt }}" {% if ts==opt %}selected{% endif %}>{{ opt }}</option>{% endfor %}</select><button type="submit">筛选</button></form></div>
  <div class="row" style="margin:12px 0"><form method="post" action="{{ url_for('retry_failed_tasks') }}"><button class="secondary" type="submit">批量重试失败翻译任务</button></form></div>
  <form method="post" action="{{ url_for('bulk_task_action') }}">
    <input type="hidden" name="section" value="downloads">
    <div class="row" style="margin:8px 0 12px"><select name="action"><option value="retry">批量请求重试</option><option value="delete">批量删除记录</option></select><button class="secondary" type="submit">执行勾选项</button></div>
    <table><thead><tr><th><input type="checkbox" onclick="document.querySelectorAll('.translation-task-checkbox').forEach(x=>x.checked=this.checked)"></th><th>更新时间</th><th>标题</th><th>状态</th><th>尝试次数</th><th>重试次数</th><th>错误</th><th>操作</th></tr></thead><tbody>{% for item in tasks %}<tr><td><input class="translation-task-checkbox" type="checkbox" name="task_ids" value="{{ item.id }}"></td><td>{{ item.updated_at }}</td><td><a href="{{ url_for('task_detail', task_id=item.id) }}">{{ item.title }}</a></td><td>{{ item.status }}</td><td>{{ item.attempts }}</td><td>{{ item.retry_count }}</td><td style="max-width:320px">{{ item.error }}</td><td>{% if item.status in ['failed','retry_requested'] %}<form method="post" action="{{ url_for('retry_task', task_id=item.id) }}"><button type="submit">重试</button></form>{% endif %}</td></tr>{% else %}<tr><td colspan="8" class="muted">暂无翻译任务</td></tr>{% endfor %}</tbody></table>
  </form>
</div>{% endif %}

{% if section=='config' %}<div class="card wide sticky-nav"><div class="seg">{% for tab in config_tabs %}<a class="{{ 'active' if config_tab==tab.key else '' }}" href="{{ url_for('dashboard', section='config', config_tab=tab.key, wizard_step=wizard_step) }}">{{ tab.label }}</a>{% endfor %}</div></div>

{% if config_tab=='wizard' %}<div class="card wide section-title" id="config-center">
  <div class="row-between"><h3 style="margin:0">首次安装向导</h3><div class="row"><form method="post" action="{{ url_for('apply_compose_preset') }}"><button class="secondary" type="submit">应用内置 Compose 预设</button></form><form method="post" action="{{ url_for('validate_config') }}"><button type="submit">校验当前配置</button></form></div></div>
  <div class="callout" style="margin-top:12px"><b>建议顺序：</b>先填 Telegram，再填 MovieServer / Emby，再确认数据库和代理，最后保存并校验。
  </div>
  <div class="seg" style="margin-top:12px">{% for item in wizard_meta %}<a class="{{ 'active' if wizard_step==item.key else '' }}" href="{{ url_for('dashboard', section='config', config_tab='wizard', wizard_step=item.key) }}">{{ item.label }}</a>{% endfor %}</div>
  <div class="wizard-grid" style="margin-top:14px">{% for item in checklist %}<div class="card" style="padding:14px"><div class="row-between"><b>{{ item.label }}</b><span class="badge {{ 'ok' if item.ok else 'bad' }}">{{ '已配置' if item.ok else '待补充' }}</span></div><div class="muted tiny" style="margin-top:8px">{{ item.hint }}</div></div>{% endfor %}</div>
  <form method="post" action="{{ url_for('save_setup_form') }}" style="margin-top:16px">
    {% if wizard_step=='telegram' %}<div class="form-grid">
      <div><label>机器人名称</label><input type="text" name="name" value="{{ setup_form.name }}"></div>
      <div><label>积分名称</label><input type="text" name="coinsname" value="{{ setup_form.coinsname }}"></div>
      <div><label>api_id</label><input type="number" name="api_id" value="{{ setup_form.api_id }}"></div>
      <div><label>api_hash</label><input type="text" name="api_hash" value="{{ setup_form.api_hash }}"></div>
      <div class="span2"><label>bot_token</label><input type="text" name="bot_token" value="{{ setup_form.bot_token }}"></div>
      <div><label>Owner</label><input type="number" name="owner" value="{{ setup_form.owner }}"></div>
      <div><label>Admin 列表</label><input type="text" name="admin" value="{{ setup_form.admin }}"></div>
      <div><label>群组ID</label><input type="number" name="group" value="{{ setup_form.group }}"></div>
    </div>{% elif wizard_step=='services' %}<div class="form-grid">
      <div class="span2"><label>MovieServer 地址</label><input type="text" name="mshost" value="{{ setup_form.mshost }}"></div>
      <div class="span2"><label>MovieServer Token</label><input type="text" name="mstoken" value="{{ setup_form.mstoken }}"></div>
      <div class="span2"><label>Emby 地址</label><input type="text" name="emby_host" value="{{ setup_form.emby_host }}"></div>
      <div class="span2"><label>Emby API Key</label><input type="text" name="emby_api" value="{{ setup_form.emby_api }}"></div>
      <div><label>Account Bot</label><input type="text" name="accountbot" value="{{ setup_form.accountbot }}"></div>
    </div>{% elif wizard_step=='database' %}<div class="form-grid">
      <div class="span2"><div class="callout"><b>当前模式：</b>{% if setup_form.use_builtin_db %}正在使用 Compose 内置数据库预设。{% else %}当前配置未使用默认内置数据库地址。{% endif %}</div></div>
      <div class="span2"><small>数据库详细参数可在“核心配置”页继续修改。</small></div>
    </div>{% elif wizard_step=='network' %}<div class="form-grid">
      <div class="row"><label><input type="checkbox" name="proxy_mode" value="1" {% if setup_form.proxy_mode %}checked{% endif %}> 启用 Telegram 代理</label></div>
      <div><label>代理协议</label><select name="proxy_scheme"><option value="http" {% if setup_form.proxy_scheme=='http' %}selected{% endif %}>http</option><option value="socks5" {% if setup_form.proxy_scheme=='socks5' %}selected{% endif %}>socks5</option></select></div>
      <div><label>代理主机</label><input type="text" name="proxy_hostname" value="{{ setup_form.proxy_hostname }}"></div>
      <div><label>代理端口</label><input type="number" name="proxy_port" value="{{ setup_form.proxy_port }}"></div>
      <div><label>代理用户名</label><input type="text" name="proxy_username" value="{{ setup_form.proxy_username }}"></div>
      <div><label>代理密码</label><input type="password" name="proxy_password" value="{{ setup_form.proxy_password }}"></div>
    </div>{% else %}<div class="callout warning">保存后建议立刻点击“校验当前配置”，确认缺失项和异常服务都已经消除。接着重启容器，并测试 Bot 的 /start、/help、/status。</div>{% endif %}
    <div class="row" style="margin-top:14px"><button type="submit">保存当前步骤</button><a class="btn-link secondary" href="{{ url_for('dashboard', section='config', config_tab='core') }}">去核心配置</a><a class="btn-link secondary" href="{{ url_for('dashboard', section='config', config_tab='services') }}">去服务与AI</a></div>
  </form>
</div>{% endif %}

{% if config_tab=='core' %}<div id="config-center" class="card wide section-title">
  <div class="row-between"><h3 style="margin:0">核心配置中心</h3><div class="row"><a href="{{ url_for('export_config') }}">导出当前配置</a></div></div><div class="callout warning" style="margin-top:12px"><b>安全提示：</b>保存完整配置时会自动创建一份备份，出问题可以在下方“配置备份与恢复”里直接回滚。</div>
  <form method="post" action="{{ url_for('save_full_config_with_backup') }}">
    <div class="form-grid">
      <div><label>机器人名称</label><input type="text" name="name" value="{{ full_form.name }}"></div>
      <div><label>积分名称</label><input type="text" name="coinsname" value="{{ full_form.coinsname }}"></div>
      <div><label>每 1GB 扣费积分</label><input type="number" name="coins_per_1GB" value="{{ full_form.coins_per_1GB }}"></div>
      <div><label>群组ID</label><input type="number" name="group" value="{{ full_form.group }}"></div>
      <div><label>api_id</label><input type="number" name="api_id" value="{{ full_form.api_id }}"></div>
      <div><label>api_hash</label><input type="text" name="api_hash" value="{{ full_form.api_hash }}"></div>
      <div class="span2"><label>bot_token</label><input type="text" name="bot_token" value="{{ full_form.bot_token }}"></div>
      <div><label>Owner</label><input type="number" name="owner" value="{{ full_form.owner }}"></div>
      <div><label>Admin 列表</label><input type="text" name="admin" value="{{ full_form.admin }}"></div>
      <div><label>MySQL Host</label><input type="text" name="host" value="{{ full_form.host }}"></div>
      <div><label>MySQL Port</label><input type="number" name="port" value="{{ full_form.port }}"></div>
      <div><label>MySQL User</label><input type="text" name="user" value="{{ full_form.user }}"></div>
      <div><label>MySQL Password</label><input type="password" name="password" value="{{ full_form.password }}"></div>
      <div><label>MySQL Database</label><input type="text" name="database" value="{{ full_form.database }}"></div>
      <div><label>PostgreSQL Host</label><input type="text" name="mspostgre_host" value="{{ full_form.mspostgre_host }}"></div>
      <div><label>PostgreSQL Port</label><input type="number" name="mspostgre_port" value="{{ full_form.mspostgre_port }}"></div>
      <div><label>PostgreSQL DB</label><input type="text" name="mspostgre_dbname" value="{{ full_form.mspostgre_dbname }}"></div>
      <div><label>PostgreSQL User</label><input type="text" name="mspostgre_user" value="{{ full_form.mspostgre_user }}"></div>
      <div><label>PostgreSQL Password</label><input type="password" name="mspostgre_password" value="{{ full_form.mspostgre_password }}"></div>
      <div><label>MovieServer 地址</label><input type="text" name="mshost" value="{{ full_form.mshost }}"></div>
      <div><label>MovieServer 用户名</label><input type="text" name="msuser" value="{{ full_form.msuser }}"></div>
      <div><label>MovieServer 密码</label><input type="password" name="mspwd" value="{{ full_form.mspwd }}"></div>
      <div><label>MovieServer Token</label><input type="text" name="mstoken" value="{{ full_form.mstoken }}"></div>
      <div><label>Emby 地址</label><input type="text" name="emby_host" value="{{ full_form.emby_host }}"></div>
      <div><label>Emby API</label><input type="text" name="emby_api" value="{{ full_form.emby_api }}"></div>
      <div><label>账号机器人链接</label><input type="text" name="accountbot" value="{{ full_form.accountbot }}"></div>
      <div><label>搜索超时</label><input type="number" name="search_timeout" value="{{ full_form.search_timeout }}"></div>
      <div><label>请求超时</label><input type="number" name="request_timeout" value="{{ full_form.request_timeout }}"></div>
      <div><label>请求重试次数</label><input type="number" name="request_retries" value="{{ full_form.request_retries }}"></div>
      <div><label>字幕批量大小</label><input type="number" name="gemini_gst_batchsize" value="{{ full_form.gemini_gst_batchsize }}"></div>
      <div><label>字幕扫描任务ID</label><input type="text" name="StrmAssistant_ScanSubtitle" value="{{ full_form.StrmAssistant_ScanSubtitle }}"></div>
      <div><label>TMDB API Key</label><input type="text" name="tmdb_api_key" value="{{ full_form.tmdb_api_key }}"></div>
      <div><label>AI Provider</label><select name="ai_provider"><option value="gemini" {% if full_form.ai_provider=='gemini' %}selected{% endif %}>gemini</option><option value="gemini_api" {% if full_form.ai_provider=='gemini_api' %}selected{% endif %}>gemini_api</option><option value="openai_compatible" {% if full_form.ai_provider=='openai_compatible' %}selected{% endif %}>openai_compatible</option></select></div>
      <div><label>Gemini Model</label><input type="text" name="gemini_model" value="{{ full_form.gemini_model }}"></div>
      <div><label>Gemini API Key</label><input type="password" name="gemini_api_key" value="{{ full_form.gemini_api_key }}"></div>
      <div><label>AI Base URL</label><input type="text" name="ai_base_url" value="{{ full_form.ai_base_url }}"></div>
      <div><label>AI API Key</label><input type="password" name="ai_api_key" value="{{ full_form.ai_api_key }}"></div>
      <div><label>AI Model</label><input type="text" name="ai_model" value="{{ full_form.ai_model }}"></div>
      <div><label>AI Chunk Chars</label><input type="number" name="ai_chunk_chars" value="{{ full_form.ai_chunk_chars }}"></div>
      <div class="row"><label><input type="checkbox" name="translation_enabled" value="1" {% if full_form.translation_enabled %}checked{% endif %}> 启用翻译</label></div>
      <div class="row"><label><input type="checkbox" name="transfer_notice_enabled" value="1" {% if full_form.transfer_notice_enabled %}checked{% endif %}> 启用入库通知</label></div>
      <div class="row"><label><input type="checkbox" name="tmdb_bg_enabled" value="1" {% if full_form.tmdb_bg_enabled %}checked{% endif %}> 启用 TMDB 背景图</label></div>
      <div class="row"><label><input type="checkbox" name="proxy_mode" value="1" {% if full_form.proxy_mode %}checked{% endif %}> 启用 Telegram 代理</label></div>
      <div><label>代理协议</label><select name="proxy_scheme"><option value="http" {% if full_form.proxy_scheme=='http' %}selected{% endif %}>http</option><option value="socks5" {% if full_form.proxy_scheme=='socks5' %}selected{% endif %}>socks5</option></select></div>
      <div><label>代理主机</label><input type="text" name="proxy_hostname" value="{{ full_form.proxy_hostname }}"></div>
      <div><label>代理端口</label><input type="number" name="proxy_port" value="{{ full_form.proxy_port }}"></div>
      <div><label>代理用户名</label><input type="text" name="proxy_username" value="{{ full_form.proxy_username }}"></div>
      <div><label>代理密码</label><input type="password" name="proxy_password" value="{{ full_form.proxy_password }}"></div>
      <div class="row"><label><input type="checkbox" name="admin_panel_enabled" value="1" {% if full_form.admin_panel_enabled %}checked{% endif %}> 启用面板</label></div>
      <div><label>面板标题</label><input type="text" name="admin_panel_title" value="{{ full_form.admin_panel_title }}"></div>
      <div><label>面板访问令牌</label><input type="text" name="admin_panel_token" value="{{ full_form.admin_panel_token }}"></div>
    </div>
    <div class="row" style="margin-top:14px"><button type="submit">保存完整配置</button><small>这张表覆盖 config.json 里的主要字段，保存后建议重启容器。</small></div>
  </form>
  <form method="post" action="{{ url_for('import_config') }}" enctype="multipart/form-data" style="margin-top:16px">
    <div class="row"><input type="file" name="config_file" accept="application/json"><button class="secondary" type="submit">导入配置备份</button></div>
  </form>
</div>{% endif %}

{% if config_tab=='services' %}<div class="card wide">
  <div class="row-between"><h3 style="margin:0">服务检测中心</h3><form method="post" action="{{ url_for('service_check') }}"><input type="hidden" name="service" value="all"><button class="secondary" type="submit">重新检测全部服务</button></form></div>
  <table><thead><tr><th>服务</th><th>启用</th><th>状态</th><th>详情</th><th>操作</th></tr></thead><tbody>{% for item in service_rows %}<tr><td>{{ item.label }}</td><td>{{ '是' if item.enabled else '否' }}</td><td><span class="badge {{ 'ok' if item.ok else 'bad' }}">{{ '正常' if item.ok else '异常' }}</span></td><td>{{ item.detail }}</td><td><form method="post" action="{{ url_for('service_check') }}"><input type="hidden" name="service" value="{{ item.key }}"><button class="secondary" type="submit">单独检测</button></form></td></tr>{% endfor %}</tbody></table>
</div>{% endif %}

{% if config_tab=='env' %}<div class="card wide">
  <div class="row-between"><h3 style="margin:0">运行环境 .env</h3><div class="row"><a href="{{ url_for('export_env') }}">导出 .env</a></div></div>
  <form method="post" action="{{ url_for('save_env') }}">
    <div class="form-grid">
      <div class="span2"><label>HTTP_PROXY</label><input type="text" name="HTTP_PROXY" value="{{ env_form.HTTP_PROXY }}"></div>
      <div class="span2"><label>HTTPS_PROXY</label><input type="text" name="HTTPS_PROXY" value="{{ env_form.HTTPS_PROXY }}"></div>
      <div class="span2"><label>NO_PROXY</label><input type="text" name="NO_PROXY" value="{{ env_form.NO_PROXY }}"></div>
      <div class="row"><label><input type="checkbox" name="PROXY_MODE" value="1" {% if env_form.PROXY_MODE in ['1','true','True','yes','on'] %}checked{% endif %}> 启用代理模式</label></div>
      <div class="row"><label><input type="checkbox" name="BOOTSTRAP_MODE" value="1" {% if env_form.BOOTSTRAP_MODE in ['1','true','True','yes','on'] %}checked{% endif %}> 启用引导模式</label></div>
      <div><label>LOG_LEVEL</label><select name="LOG_LEVEL">{% for opt in ['DEBUG','INFO','WARNING','ERROR'] %}<option value="{{ opt }}" {% if env_form.LOG_LEVEL==opt %}selected{% endif %}>{{ opt }}</option>{% endfor %}</select></div>
      <div><label>ADMIN_PANEL_TITLE</label><input type="text" name="ADMIN_PANEL_TITLE" value="{{ env_form.ADMIN_PANEL_TITLE }}"></div>
      <div><label>ADMIN_PANEL_TOKEN</label><input type="text" name="ADMIN_PANEL_TOKEN" value="{{ env_form.ADMIN_PANEL_TOKEN }}"></div>
    </div>
    <div class="row" style="margin-top:14px"><button type="submit">保存 .env 常用项</button><small>保存后建议重启容器，代理与日志等级会在下次启动时完全生效。</small></div>
  </form>
  <details style="margin-top:16px"><summary>查看 / 编辑原始 .env</summary><form method="post" action="{{ url_for('save_env_raw') }}" style="margin-top:12px"><textarea name="env_text">{{ env_text }}</textarea><div class="row" style="margin-top:14px"><button class="secondary" type="submit">保存原始 .env</button></div></form><h4>脱敏预览</h4><pre>{{ safe_env_text }}</pre></details>
</div>{% endif %}

{% if config_tab=='backups' %}<div class="card wide"><h3>配置备份与恢复</h3><form method="post" action="{{ url_for('create_backup') }}"><div class="row"><input type="text" name="note" placeholder="备份备注（可选）"><button type="submit">创建当前配置备份</button></div></form><table style="margin-top:12px"><thead><tr><th>备份ID</th><th>创建时间</th><th>文件</th><th>备注</th><th>操作</th></tr></thead><tbody>{% for item in backups %}<tr><td>{{ item.id }}</td><td>{{ item.created_at }}</td><td>{{ ', '.join(item.files or []) }}</td><td>{{ item.note }}</td><td><div class="row"><a class="btn-link secondary" href="{{ url_for('backup_diff_detail', backup_id=item.id) }}">差异预览</a><form method="post" action="{{ url_for('restore_backup', backup_id=item.id) }}"><button class="secondary" data-confirm="确认恢复这个备份？会覆盖当前 config/.env/blackseeds。" type="submit">恢复</button></form><form method="post" action="{{ url_for('delete_backup_route', backup_id=item.id) }}"><button class="danger" data-confirm="确认删除这个备份？删除后无法恢复。" type="submit">删除</button></form></div></td></tr>{% else %}<tr><td colspan="5" class="muted">暂无备份</td></tr>{% endfor %}</tbody></table></div>{% endif %}

{% if config_tab=='advanced' %}<div class="card wide">
  <h3>blackseeds 规则</h3>
  <form method="post" action="{{ url_for('save_blackseeds') }}"><textarea name="content">{{ blackseeds_text }}</textarea><div class="row" style="margin-top:14px"><button type="submit">保存规则</button><small>每行一条，保存后立刻生效。</small></div></form>
</div>

<div class="card wide">
  <h3>config.json 原始编辑</h3>
  <form method="post" action="{{ url_for('save_config_json') }}"><textarea name="config_json">{{ editable_config_json }}</textarea><div class="row" style="margin-top:14px"><button type="submit">保存 config.json</button><small>适合高级用户；普通场景优先用上方“完整配置中心”和“.env 常用项”。</small></div></form>
</div>{% endif %}{% endif %}

{% if section in ['logs','overview'] %}<div class="card"><h3>Bot 日志</h3><pre>{{ bot_log_text }}</pre></div>
<div class="card"><h3>Cron 日志</h3><pre>{{ cron_log_text }}</pre></div>
<div class="card"><h3>安全配置摘要</h3><pre>{{ safe_config_json }}</pre></div>
</div>{% endif %}</div></div><div class="mobile-nav"><div class="nav-grid">
  <a class="{{ 'active' if section=='overview' else '' }}" href="{{ url_for('dashboard', section='overview') }}">总览</a>
  <a class="{{ 'active' if section=='downloads' else '' }}" href="{{ url_for('dashboard', section='downloads') }}">任务</a>
  <a class="{{ 'active' if section=='users' else '' }}" href="{{ url_for('dashboard', section='users') }}">用户</a>
  <a class="{{ 'active' if section=='config' else '' }}" href="{{ url_for('dashboard', section='config', config_tab=config_tab) }}">配置</a>
  <a class="{{ 'active' if section=='logs' else '' }}" href="{{ url_for('dashboard', section='logs') }}">日志</a>
</div></div>
<script>
if ('serviceWorker' in navigator) { window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {})); }
const net = document.getElementById('net-status');
const updateNet = () => { if (!net) return; net.textContent = navigator.onLine ? '在线' : '离线'; net.className = 'pill ' + (navigator.onLine ? '' : 'warn'); };
window.addEventListener('online', updateNet); window.addEventListener('offline', updateNet); updateNet();
let deferredPrompt = null;
const installBtn = document.getElementById('install-app-btn');
const installBtnSheet = document.getElementById('install-app-btn-sheet');
const installSheet = document.getElementById('install-sheet');
const closeInstallSheet = document.getElementById('close-install-sheet');
const showInstallUI = () => { if (installBtn) installBtn.style.display = 'inline-flex'; if (installSheet) installSheet.style.display = 'block'; };
const triggerInstall = async () => { if (!deferredPrompt) return; deferredPrompt.prompt(); try { await deferredPrompt.userChoice; } catch(e) {} deferredPrompt = null; if (installBtn) installBtn.style.display='none'; if (installSheet) installSheet.style.display='none'; };
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferredPrompt = e; if (localStorage.getItem('lyrebird-install-dismissed') !== '1') showInstallUI(); });
if (installBtn) installBtn.addEventListener('click', triggerInstall);
if (installBtnSheet) installBtnSheet.addEventListener('click', triggerInstall);
if (closeInstallSheet) closeInstallSheet.addEventListener('click', () => { localStorage.setItem('lyrebird-install-dismissed', '1'); if (installSheet) installSheet.style.display='none'; });
const refreshBtn = document.getElementById('refresh-btn'); if (refreshBtn) refreshBtn.addEventListener('click', () => window.location.reload());
setTimeout(() => { document.querySelectorAll('.toast').forEach(el => el.remove()); }, 4000);
const forms = Array.from(document.querySelectorAll('form'));
forms.forEach(form => {
  const hasEditor = form.querySelector('textarea') || form.querySelector('input[type=text],input[type=password],input[type=number],select');
  if (!hasEditor) return;
  let dirty = false;
  form.addEventListener('change', () => { dirty = true; });
  form.addEventListener('submit', () => { dirty = false; });
  window.addEventListener('beforeunload', (e) => { if (!dirty) return; e.preventDefault(); e.returnValue = ''; });
});
document.querySelectorAll('form button.danger, form .danger').forEach(btn => {
  btn.addEventListener('click', (e) => { if (!confirm(btn.dataset.confirm || '确认执行这个高风险操作？')) { e.preventDefault(); } });
});

const bulkForms = document.querySelectorAll('form');
document.querySelectorAll('[data-confirm]').forEach(btn => {
  btn.addEventListener('click', (e) => { if (!confirm(btn.getAttribute('data-confirm'))) e.preventDefault(); });
});
document.querySelectorAll('[data-autosubmit-select]').forEach(sel => {
  sel.addEventListener('change', () => { if (sel.form) sel.form.submit(); });
});
</script></body></html>'''


@app.route('/offline')
def offline_page():
    return Response('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>离线中</title><style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#0b1220;color:#dbeafe;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}.card{max-width:420px;background:#111827;border:1px solid #1f2937;border-radius:18px;padding:22px}a{color:#93c5fd}</style><div class="card"><h2 style="margin-top:0">当前离线</h2><p>管理面板未能连接到 NAS。你仍可查看部分已缓存页面；恢复网络后刷新即可。</p><p><a href="/">返回首页</a></p></div></html>', mimetype='text/html; charset=utf-8')


@app.route('/manifest.webmanifest')
def manifest_webmanifest():
    payload = {
        'name': TITLE,
        'short_name': 'LyrebirdMS',
        'start_url': url_for('dashboard', _external=False),
        'scope': '/',
        'display': 'standalone',
        'background_color': '#0b1220',
        'theme_color': '#111827',
        'description': 'LyrebirdMS Bot NAS 管理面板，支持移动端安装与快捷运维。',
        'icons': [{'src': url_for('pwa_icon', _external=False), 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any maskable'}],
        'shortcuts': [
            {'name': '总览', 'short_name': '总览', 'url': url_for('dashboard', section='overview', _external=False)},
            {'name': '下载任务', 'short_name': '任务', 'url': url_for('dashboard', section='downloads', _external=False)},
            {'name': '配置中心', 'short_name': '配置', 'url': url_for('dashboard', section='config', _external=False)},
            {'name': '日志诊断', 'short_name': '日志', 'url': url_for('dashboard', section='logs', _external=False)},
        ],
    }
    return Response(json.dumps(payload, ensure_ascii=False), mimetype='application/manifest+json')


@app.route('/sw.js')
def pwa_sw():
    js = """
const CACHE_NAME = 'lyrebird-admin-v2';
const CORE = ['/', '/offline', '/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(CORE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).then(resp => {
      const copy = resp.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
      return resp;
    }).catch(async () => (await caches.match(event.request)) || caches.match('/offline')));
    return;
  }
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
    return;
  }
  event.respondWith(fetch(event.request).then(resp => {
    const copy = resp.clone();
    caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
    return resp;
  }).catch(() => caches.match(event.request)));
});
"""
    return Response(js, mimetype='application/javascript')


@app.route('/icon.svg')
def pwa_icon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#0f172a"/><rect x="56" y="56" width="400" height="400" rx="72" fill="#111827" stroke="#2563eb" stroke-width="18"/><path d="M164 318l64-124 54 76 40-56 58 104H164z" fill="#60a5fa"/><circle cx="198" cy="184" r="28" fill="#93c5fd"/><text x="256" y="402" font-size="52" text-anchor="middle" fill="#dbeafe" font-family="Arial, sans-serif">MS</text></svg>'
    return Response(svg, mimetype='image/svg+xml')


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        if (request.form.get('token') or '') == TOKEN or TOKEN == 'change-me':
            session['admin_authed'] = True
            return redirect(request.args.get('next') or url_for('dashboard'))
        error = '访问令牌错误'
    return render_template_string(LOGIN_TMPL, title=TITLE, error=error, weak=(TOKEN == 'change-me'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@require_auth
def dashboard():
    section = (request.args.get('section') or 'overview').strip()
    config_tab = (request.args.get('config_tab') or 'wizard').strip()
    wizard_step = (request.args.get('wizard_step') or 'telegram').strip()
    dq = (request.args.get('dq') or '').strip()
    uq = (request.args.get('uq') or '').strip()
    tq = (request.args.get('tq') or '').strip()
    ts = (request.args.get('ts') or '').strip()
    dts = (request.args.get('dts') or '').strip()
    status = _status_payload()
    health = run_healthcheck(log_on_success=False)
    setup = _setup_summary()
    downloads = search_downloads(dq, 50) if dq else list_recent_downloads(20)
    users = search_users(uq, 50) if uq else list_recent_users(20)
    tasks = list_tasks(task_type='translation', status=ts or None, query=tq or None, limit=80)
    download_tasks = list_tasks(task_type='download', status=dts or None, query=dq or None, limit=80)
    state = read_state()
    events = ((state.get('events') or [])[-40:])
    editable_config = '{}'
    if CONFIG_PATH.exists():
        editable_config = CONFIG_PATH.read_text(encoding='utf-8', errors='ignore')
    return render_template_string(
        DASHBOARD_TMPL,
        title=TITLE,
        status=status,
        setup=setup,
        setup_form=_setup_form_defaults(),
        setup_steps=_setup_step_fields(),
        full_form=_full_config_defaults(),
        service_rows=_service_statuses(),
        config_path=str(CONFIG_PATH),
        health_json=json.dumps(health, ensure_ascii=False, indent=2),
        events_text='\n'.join(json.dumps(item, ensure_ascii=False) for item in events) or '暂无事件',
        downloads=downloads,
        users=users,
        tasks=tasks,
        download_tasks=download_tasks,
        dq=dq,
        uq=uq,
        tq=tq,
        ts=ts,
        dts=dts,
        safe_config=_safe_config(),
        safe_config_json=json.dumps(_safe_config(), ensure_ascii=False, indent=2),
        editable_config_json=editable_config,
        blackseeds_text='\n'.join(_blackseeds_lines()) or '',
        bot_log_text='\n'.join(_tail(LOG_FILE, 220)) or '暂无日志',
        cron_log_text='\n'.join(_tail(CRON_LOG_FILE, 120)) or '暂无日志',
        hints=(read_state().get('admin_hints') or {}),
        env_text=_read_env_text(),
        safe_env_text='\n'.join(_safe_env_lines()) or '暂无 .env',
        env_form=_env_form_defaults(),
        backups=_list_backups(),
        config_tabs=_config_tabs(),
        config_tab=config_tab,
        wizard_step=wizard_step,
        wizard_meta=_wizard_steps_meta(),
        checklist=_config_checklist(),
        preflight=setup.get('preflight') or _preflight_payload(),
        preflight_json=json.dumps((setup.get('preflight') or _preflight_payload()), ensure_ascii=False, indent=2),
        section=section,
        flashes=get_flashed_messages(with_categories=True),
    )


@app.route('/setup/save', methods=['POST'])
@require_auth
def save_setup_form():
    try:
        _save_setup_from_form()
    except Exception as e:
        write_log(f'管理员保存快速配置向导失败: {e}', level='ERROR')
        return Response(f'保存失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '快速配置向导已保存', section='config', config_tab='wizard')


@app.route('/features', methods=['POST'])
@require_auth
def update_features():
    patch = {
        'translation_enabled': bool(request.form.get('translation_enabled')),
        'transfer_notice_enabled': bool(request.form.get('transfer_notice_enabled')),
        'tmdb_bg_enabled': bool(request.form.get('tmdb_bg_enabled')),
        'log_level': (request.form.get('log_level') or 'INFO').upper(),
    }
    saved = save_admin_overrides(patch)
    merge_state({'features': saved})
    record_event('feature_flags_updated', patch=saved)
    try:
        logger.setLevel(saved.get('log_level', 'INFO'))
    except Exception:
        pass
    write_log(f'管理员更新了功能开关: {saved}')
    return _redirect_with_message('dashboard', '功能开关已更新', section='overview')


@app.route('/ai-settings', methods=['POST'])
@require_auth
def update_ai_settings():
    patch = {
        'ai_provider': (request.form.get('ai_provider') or 'gemini').strip(),
        'gemini_model': (request.form.get('gemini_model') or '').strip(),
        'ai_model': (request.form.get('ai_model') or '').strip(),
        'ai_base_url': (request.form.get('ai_base_url') or '').strip(),
        'ai_chunk_chars': int((request.form.get('ai_chunk_chars') or '2400').strip() or '2400'),
    }
    saved = save_admin_overrides(patch)
    merge_state({'ai_settings': saved})
    record_event('ai_settings_updated', patch=patch)
    write_log(f'管理员更新了 AI 设置 provider={patch.get("ai_provider")} model={patch.get("ai_model") or patch.get("gemini_model")}')
    return _redirect_with_message('dashboard', 'AI 配置已更新', section='config')


@app.route('/tasks/<task_id>/retry', methods=['POST'])
@require_auth
def retry_task(task_id):
    task = get_task(task_id)
    if task:
        request_retry(task_id)
        record_event('admin_retry_task', task_id=task_id, remote_addr=request.remote_addr)
        write_log(f'管理员请求重试任务 task_id={task_id}')
    return _redirect_with_message('dashboard', f'已请求重试任务 {task_id}', section='downloads')


@app.route('/tasks/retry-failed', methods=['POST'])
@require_auth
def retry_failed_tasks():
    changed = request_retry_failed('translation', limit=20)
    record_event('admin_retry_failed_tasks', changed=changed)
    write_log(f'管理员批量请求重试失败翻译任务 count={changed}', level='WARNING')
    return _redirect_with_message('dashboard', f'已批量请求重试失败翻译任务 {changed} 条', section='downloads')


@app.route('/tasks/download/<task_id>/retry', methods=['POST'])
@require_auth
def retry_download_task(task_id):
    task = get_task(task_id)
    if task and task.get('type') == 'download':
        request_retry(task_id)
        record_event('admin_retry_download_task', task_id=task_id, remote_addr=request.remote_addr)
        write_log(f'管理员请求重试下载任务 task_id={task_id}', level='WARNING')
    return _redirect_with_message('dashboard', f'已请求重试下载任务 {task_id}', section='downloads')


@app.route('/tasks/download/retry-failed', methods=['POST'])
@require_auth
def retry_failed_download_tasks():
    changed = request_retry_failed('download', limit=20)
    record_event('admin_retry_failed_download_tasks', changed=changed)
    write_log(f'管理员批量请求重试失败下载任务 count={changed}', level='WARNING')
    return _redirect_with_message('dashboard', f'已批量请求重试失败下载任务 {changed} 条', section='downloads')


@app.route('/sessions/clear', methods=['POST'])
@require_auth
def clear_sessions():
    removed = session_store.clear()
    record_event('admin_clear_sessions', removed=removed)
    write_log(f'管理员清空会话缓存 removed={removed}', level='WARNING')
    return _redirect_with_message('dashboard', f'已清空会话缓存 {removed} 项', section='overview')


@app.route('/blackseeds/save', methods=['POST'])
@require_auth
def save_blackseeds():
    count = _save_blackseeds(request.form.get('content') or '')
    record_event('admin_saved_blackseeds', count=count)
    return _redirect_with_message('dashboard', f'blackseeds 已保存，共 {count} 条', section='config')


@app.route('/config/save', methods=['POST'])
@require_auth
def save_config_json():
    raw = request.form.get('config_json') or ''
    try:
        _write_config_json(raw)
    except Exception as e:
        write_log(f'管理员保存 config.json 失败: {e}', level='ERROR')
        return Response(f'保存失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', 'config.json 已保存', section='config')



@app.route('/tasks/<task_id>/delete', methods=['POST'])
@require_auth
def delete_task_route(task_id):
    removed = delete_task(task_id)
    record_event('admin_delete_task', task_id=task_id, removed=removed)
    write_log(f'管理员删除任务 task_id={task_id} removed={removed}', level='WARNING')
    return _redirect_with_message('dashboard', f'任务已删除：{task_id}', section='downloads')


@app.route('/tasks/download/prune', methods=['POST'])
@require_auth
def prune_download_tasks():
    removed = prune_tasks(task_type='download', keep=120)
    record_event('admin_prune_download_tasks', removed=removed)
    write_log(f'管理员清理下载提交流水 removed={removed}', level='WARNING')
    return _redirect_with_message('dashboard', f'已清理下载提交流水 {removed} 条', section='downloads')


@app.route('/tasks/bulk', methods=['POST'])
@require_auth
def bulk_task_action():
    section = (request.form.get('section') or 'tasks').strip()
    action = (request.form.get('action') or '').strip()
    ids = _selected_ids('task_ids')
    changed = 0
    for task_id in ids:
        task = get_task(task_id)
        if not task:
            continue
        if action == 'retry':
            request_retry(task_id)
            changed += 1
        elif action == 'delete':
            changed += 1 if delete_task(task_id) else 0
    record_event('admin_bulk_task_action', section=section, action=action, changed=changed, ids=ids[:30])
    write_log(f'管理员批量操作任务 section={section} action={action} changed={changed}', level='WARNING')
    flash(f'批量操作已执行：{action} / {changed} 条', 'success')
    return _redirect_dashboard(section)


@app.route('/users/bulk-update', methods=['POST'])
@require_auth
def bulk_update_users():
    ids = _selected_ids('user_ids')
    coins_delta = _parse_int(request.form.get('coins_delta') or '0', 0)
    free_delta = _parse_int(request.form.get('free_delta') or '0', 0)
    level = (request.form.get('level') or '').strip()
    changed = 0
    for user_id in ids:
        ok = admin_adjust_user(user_id, coins_delta=coins_delta, free_delta=free_delta, level=level or None)
        changed += 1 if ok else 0
    level_text = level if level else 'unchanged'
    record_event('admin_bulk_update_users', changed=changed, user_ids=ids[:30], coins_delta=coins_delta, free_delta=free_delta, level=level)
    write_log(f'管理员批量调整用户 count={changed} coins_delta={coins_delta} free_delta={free_delta} level={level_text}', level='WARNING')
    return _redirect_with_message('dashboard', f'用户批量操作已执行：{changed} 个', section='users')


@app.route('/config/save-and-backup', methods=['POST'])
@require_auth
def save_full_config_with_backup():
    try:
        backup_id = _create_backup('保存完整配置前自动备份')
        _save_full_config_from_form()
        record_event('config_saved_with_backup', backup_id=backup_id)
    except Exception as e:
        write_log(f'管理员保存完整配置并备份失败: {e}', level='ERROR')
        return Response(f'保存失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '配置操作已完成', section='config')


@app.route('/users/<user_id>')
@require_auth
def user_detail(user_id):
    detail = get_user_detail(user_id)
    rows = get_downloads_by_user(user_id, limit=50)
    return render_template_string(
        DETAIL_TMPL,
        title=TITLE,
        heading=f'用户详情 / {user_id}',
        detail_json=json.dumps(detail or {'user_id': user_id, 'found': False}, ensure_ascii=False, indent=2, default=str),
        rows=rows,
        columns=['date', 'title', 'torrent_id', 'cost_coins', 'size', 'tmdbid'],
        admin_action_url=url_for('update_user_detail', user_id=user_id),
        user_form=True,
        task_actions=None,
    )


@app.route('/downloads/<torrent_id>')
@require_auth
def download_detail(torrent_id):
    detail = get_download_by_torrent_id(torrent_id)
    rows = None
    columns = []
    if detail and detail.get('telegram_id'):
        rows = get_downloads_by_user(detail['telegram_id'], limit=20)
        columns = ['date', 'title', 'torrent_id', 'cost_coins', 'size', 'tmdbid']
    return render_template_string(
        DETAIL_TMPL,
        title=TITLE,
        heading=f'下载详情 / {torrent_id}',
        detail_json=json.dumps(detail or {'torrent_id': torrent_id, 'found': False}, ensure_ascii=False, indent=2, default=str),
        rows=rows,
        columns=columns,
        admin_action_url=None,
        user_form=False,
        task_actions=None,
    )


@app.route('/tasks/<task_id>')
@require_auth
def task_detail(task_id):
    detail = get_task(task_id)
    retry_url = None
    delete_url = None
    if detail:
        if detail.get('status') in {'failed', 'retry_requested'}:
            retry_url = url_for('retry_download_task', task_id=task_id) if detail.get('type') == 'download' else url_for('retry_task', task_id=task_id)
        if detail.get('status') in {'failed', 'success'}:
            delete_url = url_for('delete_task_route', task_id=task_id)
    return render_template_string(
        DETAIL_TMPL,
        title=TITLE,
        heading=f'任务详情 / {task_id}',
        detail_json=json.dumps(detail or {'task_id': task_id, 'found': False}, ensure_ascii=False, indent=2, default=str),
        rows=None,
        columns=[],
        admin_action_url=None,
        user_form=False,
        task_actions={'retry_url': retry_url, 'delete_url': delete_url},
    )




@app.route('/users/<user_id>/update', methods=['POST'])
@require_auth
def update_user_detail(user_id):
    coins_delta = _parse_int(request.form.get('coins_delta') or '0', 0)
    free_delta = _parse_int(request.form.get('free_delta') or '0', 0)
    level = (request.form.get('level') or '').strip()
    ok = admin_adjust_user(user_id, coins_delta=coins_delta, free_delta=free_delta, level=level or None)
    record_event('admin_update_user', user_id=user_id, ok=ok, coins_delta=coins_delta, free_delta=free_delta, level=level)
    write_log(f'管理员调整用户 tg={user_id} ok={ok} coins_delta={coins_delta} free_delta={free_delta} level={level or "unchanged"}', level='WARNING')
    return _redirect_with_message('user_detail', '用户信息已更新', user_id=user_id)

@app.route('/config/full-save', methods=['POST'])
@require_auth
def save_full_config():
    try:
        _save_full_config_from_form()
    except Exception as e:
        write_log(f'管理员保存完整配置失败: {e}', level='ERROR')
        return Response(f'保存失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '完整配置已保存', section='config')


@app.route('/config/export')
@require_auth
def export_config():
    raw = _raw_config()
    return Response(
        json.dumps(raw, ensure_ascii=False, indent=2),
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=config.export.json'}
    )


@app.route('/config/import', methods=['POST'])
@require_auth
def import_config():
    file = request.files.get('config_file')
    if not file:
        return Response('未选择文件', status=400, mimetype='text/plain; charset=utf-8')
    raw = file.read().decode('utf-8', errors='ignore')
    try:
        _write_config_json(raw)
    except Exception as e:
        write_log(f'管理员导入 config.json 失败: {e}', level='ERROR')
        return Response(f'导入失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    record_event('config_imported_from_panel')
    return _redirect_with_message('dashboard', '配置导入完成', section='config')


@app.route('/services/check', methods=['POST'])
@require_auth
def service_check():
    name = (request.form.get('service') or 'all').strip()
    health = run_healthcheck(log_on_success=False)
    record_event('admin_service_check', service=name, health=health)
    write_log(f'管理员触发服务检测 service={name}')
    return _redirect_with_message('dashboard', f'服务检测已执行：{name}', section='config')


@app.route('/env/save', methods=['POST'])
@require_auth
def save_env():
    try:
        _save_env_from_form()
    except Exception as e:
        write_log(f'管理员保存 .env 失败: {e}', level='ERROR')
        return Response(f'保存失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '.env 常用项已保存', section='config')


@app.route('/env/raw-save', methods=['POST'])
@require_auth
def save_env_raw():
    raw = request.form.get('env_text') or ''
    try:
        _write_env_text(raw)
    except Exception as e:
        write_log(f'管理员保存原始 .env 失败: {e}', level='ERROR')
        return Response(f'保存失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '.env 原始内容已保存', section='config')


@app.route('/env/export')
@require_auth
def export_env():
    return Response(
        _read_env_text(),
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=.env.export'}
    )


@app.route('/backups/create', methods=['POST'])
@require_auth
def create_backup():
    note = (request.form.get('note') or '').strip()
    try:
        _create_backup(note)
    except Exception as e:
        return Response(f'创建备份失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '配置操作已完成', section='config')


@app.route('/backups/<backup_id>/restore', methods=['POST'])
@require_auth
def restore_backup(backup_id):
    try:
        _restore_backup(backup_id)
    except Exception as e:
        return Response(f'恢复备份失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '配置操作已完成', section='config')


@app.route('/backups/<backup_id>/delete', methods=['POST'])
@require_auth
def delete_backup_route(backup_id):
    try:
        _delete_backup(backup_id)
    except Exception as e:
        return Response(f'删除备份失败: {e}', status=400, mimetype='text/plain; charset=utf-8')
    return _redirect_with_message('dashboard', '配置操作已完成', section='config')


@app.route('/config/apply-compose-preset', methods=['POST'])
@require_auth
def apply_compose_preset():
    try:
        _create_backup('apply compose preset')
        _apply_builtin_compose_defaults()
    except Exception as e:
        write_log(f'应用内置 Compose 预设失败: {e}', level='ERROR')
        return _redirect_with_message('dashboard', f'应用预设失败: {e}', 'error', section='config', config_tab='wizard')
    return _redirect_with_message('dashboard', '已应用内置 Compose 预设，请继续补齐 Telegram / MovieServer / Emby。', 'success', section='config', config_tab='wizard')


@app.route('/config/validate', methods=['POST'])
@require_auth
def validate_config():
    health = run_healthcheck(log_on_success=False)
    missing = health.get('missing') or []
    broken = [k for k, v in health.items() if isinstance(v, dict) and not v.get('ok', True)]
    if missing or broken:
        msg = f"配置未通过：缺少 {', '.join(missing) if missing else '无'}；异常服务 {', '.join(broken) if broken else '无'}"
        return _redirect_with_message('dashboard', msg, 'warning', section='config', config_tab='wizard')
    return _redirect_with_message('dashboard', '配置校验通过，建议现在重启容器并测试 /start /status。', 'success', section='config', config_tab='wizard')


@app.route('/api/preflight')
@require_auth
def api_preflight():
    return jsonify(_preflight_payload())


@app.route('/diagnostics.json')
@require_auth
def download_diagnostics():
    payload = _diagnostics_payload()
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=lyrebird-diagnostics.json'}
    )


@app.route('/api/status')
@require_auth
def api_status():
    return jsonify(_status_payload())


@app.route('/api/health')
@require_auth
def api_health():
    return jsonify(run_healthcheck(log_on_success=False))


@app.route('/api/logs')
@require_auth
def api_logs():
    return jsonify({'bot_log': _tail(LOG_FILE, 300), 'cron_log': _tail(CRON_LOG_FILE, 120)})


@app.route('/api/config')
@require_auth
def api_config():
    return jsonify(_safe_config())


@app.route('/api/downloads')
@require_auth
def api_downloads():
    q = (request.args.get('q') or '').strip()
    return jsonify(search_downloads(q, 100) if q else list_recent_downloads(100))


@app.route('/api/users')
@require_auth
def api_users():
    q = (request.args.get('q') or '').strip()
    return jsonify(search_users(q, 100) if q else list_recent_users(100))


@app.route('/api/tasks')
@require_auth
def api_tasks():
    q = (request.args.get('q') or '').strip()
    status = (request.args.get('status') or '').strip() or None
    task_type = (request.args.get('type') or 'translation').strip()
    return jsonify(list_tasks(task_type=task_type, status=status, query=q or None, limit=200))


@app.route('/api/env')
@require_auth
def api_env():
    return jsonify({'safe_env': _safe_env_lines(), 'env_path': str(ENV_PATH)})


@app.route('/api/blackseeds')
@require_auth
def api_blackseeds():
    return jsonify({'lines': _blackseeds_lines(500)})


if __name__ == '__main__':
    merge_state({'web_admin': {'status': 'running', 'started_at': datetime.now(TZ).isoformat()}})
    write_log(f'管理面板已启动: http://{config.get("admin_panel_host")}:{config.get("admin_panel_port")}')
    app.run(host=config.get('admin_panel_host', '0.0.0.0'), port=int(config.get('admin_panel_port', 47521)))
