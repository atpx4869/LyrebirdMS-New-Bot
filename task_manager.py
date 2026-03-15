import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app_config import config
from logger.logger import write_log

_RUNTIME_DIR = Path(config.get('runtime_dir', Path(__file__).resolve().parent / 'runtime'))
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
_TASKS_FILE = Path(os.getenv('TASKS_FILE', _RUNTIME_DIR / 'tasks.json'))
_LOCK = threading.Lock()
_TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_TZ).isoformat()


def _read() -> Dict[str, Any]:
    if not _TASKS_FILE.exists():
        return {'tasks': []}
    try:
        data = json.loads(_TASKS_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            data.setdefault('tasks', [])
            return data
    except Exception:
        pass
    return {'tasks': []}


def _write(data: Dict[str, Any]) -> None:
    tmp = _TASKS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(_TASKS_FILE)


def create_task(task_type: str, title: str, payload: Dict[str, Any], created_by: str | int | None = None) -> Dict[str, Any]:
    task = {
        'id': uuid.uuid4().hex[:12],
        'type': task_type,
        'title': title,
        'status': 'queued',
        'payload': payload,
        'result': {},
        'error': '',
        'created_by': str(created_by or ''),
        'attempts': 0,
        'retry_count': 0,
        'created_at': _now(),
        'updated_at': _now(),
        'started_at': '',
        'finished_at': '',
        'retry_requested_at': '',
        'last_worker': '',
    }
    with _LOCK:
        data = _read()
        data['tasks'].append(task)
        data['tasks'] = data['tasks'][-2000:]
        _write(data)
    return task




def delete_task(task_id: str) -> bool:
    with _LOCK:
        data = _read()
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        changed = len(data["tasks"]) != before
        if changed:
            _write(data)
        return changed


def prune_tasks(task_type: str | None = None, keep: int = 300) -> int:
    with _LOCK:
        data = _read()
        tasks = data['tasks']
        if task_type:
            keepers = [t for t in tasks if t.get('type') != task_type]
            scoped = [t for t in tasks if t.get('type') == task_type]
            scoped.sort(key=lambda x: x.get('updated_at') or x.get('created_at') or '', reverse=True)
            new_tasks = keepers + scoped[:keep]
        else:
            tasks.sort(key=lambda x: x.get('updated_at') or x.get('created_at') or '', reverse=True)
            new_tasks = tasks[:keep]
        removed = len(tasks) - len(new_tasks)
        if removed > 0:
            data['tasks'] = new_tasks
            _write(data)
        return max(0, removed)

def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        for task in _read()['tasks']:
            if task.get('id') == task_id:
                return task
    return None


def update_task(task_id: str, **patch: Any) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _read()
        for task in data['tasks']:
            if task.get('id') == task_id:
                task.update(patch)
                task['updated_at'] = _now()
                _write(data)
                return task
    return None


def mark_running(task_id: str, worker: str = '') -> Optional[Dict[str, Any]]:
    task = get_task(task_id)
    attempts = int((task or {}).get('attempts', 0)) + 1
    return update_task(task_id, status='running', started_at=_now(), last_worker=worker, attempts=attempts, error='')


def mark_success(task_id: str, result: Dict[str, Any] | None = None) -> Optional[Dict[str, Any]]:
    return update_task(task_id, status='success', finished_at=_now(), result=result or {}, error='')


def mark_failed(task_id: str, error: str) -> Optional[Dict[str, Any]]:
    return update_task(task_id, status='failed', finished_at=_now(), error=str(error))


def request_retry(task_id: str) -> Optional[Dict[str, Any]]:
    task = get_task(task_id)
    if not task:
        return None
    retry_count = int(task.get('retry_count', 0)) + 1
    return update_task(task_id, status='retry_requested', retry_requested_at=_now(), retry_count=retry_count, finished_at='')


def request_retry_failed(task_type: str | None = None, limit: int = 100) -> int:
    changed = 0
    with _LOCK:
        data = _read()
        for task in reversed(data['tasks']):
            if changed >= limit:
                break
            if task_type and task.get('type') != task_type:
                continue
            if task.get('status') != 'failed':
                continue
            task['status'] = 'retry_requested'
            task['retry_requested_at'] = _now()
            task['retry_count'] = int(task.get('retry_count', 0)) + 1
            task['updated_at'] = _now()
            task['finished_at'] = ''
            changed += 1
        if changed:
            _write(data)
    return changed


def claim_retry_tasks(task_type: str) -> List[Dict[str, Any]]:
    claimed: List[Dict[str, Any]] = []
    with _LOCK:
        data = _read()
        changed = False
        for task in data['tasks']:
            if task.get('type') == task_type and task.get('status') == 'retry_requested':
                task['status'] = 'queued'
                task['updated_at'] = _now()
                claimed.append(task.copy())
                changed = True
        if changed:
            _write(data)
    return claimed


def list_tasks(task_type: str | None = None, status: str | None = None, query: str | None = None, limit: int = 100) -> List[Dict[str, Any]]:
    tasks = _read()['tasks']
    if task_type:
        tasks = [t for t in tasks if t.get('type') == task_type]
    if status:
        tasks = [t for t in tasks if t.get('status') == status]
    if query:
        q = query.lower()
        tasks = [t for t in tasks if q in json.dumps(t, ensure_ascii=False).lower()]
    tasks.sort(key=lambda x: x.get('updated_at') or x.get('created_at') or '', reverse=True)
    return tasks[:limit]


def task_stats(task_type: str | None = None) -> Dict[str, Any]:
    tasks = list_tasks(task_type=task_type, limit=2000)
    counts: Dict[str, int] = {}
    for t in tasks:
        counts[t.get('status', 'unknown')] = counts.get(t.get('status', 'unknown'), 0) + 1
    return {
        'total': len(tasks),
        'counts': counts,
        'failed_recent': len([t for t in tasks[:100] if t.get('status') == 'failed']),
        'running': counts.get('running', 0),
        'queued': counts.get('queued', 0) + counts.get('retry_requested', 0),
    }
