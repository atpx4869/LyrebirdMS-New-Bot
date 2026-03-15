import os

from app_config import config
from http_client import get
from logger.logger import write_log

REQUIRED_KEYS = ['api_id', 'api_hash', 'bot_token', 'mshost', 'mstoken', 'host', 'port', 'user', 'password', 'database']


def _check_mysql():
    try:
        import pymysql
        conn = pymysql.connect(host=config['host'], port=config['port'], user=config['user'], passwd=config['password'], db=config['database'], connect_timeout=5)
        conn.close()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _check_postgres():
    try:
        import psycopg2
        conn = psycopg2.connect(host=config['mspostgre_host'], port=config['mspostgre_port'], dbname=config['mspostgre_dbname'], user=config['mspostgre_user'], password=config['mspostgre_password'], connect_timeout=5)
        conn.close()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _check_redis():
    url = os.getenv('REDIS_URL', '').strip()
    if not url:
        return {'enabled': False, 'ok': True}
    try:
        import redis
        client = redis.from_url(url, decode_responses=True)
        client.ping()
        return {'enabled': True, 'ok': True}
    except Exception as e:
        return {'enabled': True, 'ok': False, 'error': str(e)}




def _check_http_service(name: str, url: str | None):
    if not url:
        return {'enabled': False, 'ok': True, 'skipped': True}
    try:
        resp = get(str(url), timeout=8, allow_redirects=True)
        return {'enabled': True, 'ok': resp.status_code < 500, 'status_code': resp.status_code, 'url': url}
    except Exception as e:
        return {'enabled': True, 'ok': False, 'url': url, 'error': str(e)}

def _check_ai_provider():
    provider = str(config.get('ai_provider', 'gemini')).strip().lower()
    if not config.get('translation_enabled', True):
        return {'enabled': False, 'ok': True, 'provider': provider}
    if provider in {'gemini', 'gemini_api'}:
        ok = bool(config.get('gemini_api_key') or config.get('ai_api_key'))
        return {'enabled': True, 'provider': provider, 'ok': ok, 'model': config.get('gemini_model') or config.get('ai_model'), 'error': '' if ok else '缺少 gemini_api_key / ai_api_key'}
    if provider == 'openai_compatible':
        ok = bool(config.get('ai_base_url')) and bool(config.get('ai_api_key')) and bool(config.get('ai_model'))
        return {'enabled': True, 'provider': provider, 'ok': ok, 'model': config.get('ai_model'), 'base_url': config.get('ai_base_url'), 'error': '' if ok else '缺少 ai_base_url / ai_api_key / ai_model'}
    return {'enabled': True, 'provider': provider, 'ok': False, 'error': '不支持的 ai_provider'}


def run_healthcheck(log_on_success: bool = True):
    missing = [k for k in REQUIRED_KEYS if config.get(k) in (None, '', [])]
    mysql = _check_mysql() if not missing else {'ok': False, 'error': 'missing config'}
    postgres = _check_postgres() if config.get('mspostgre_host') else {'ok': True, 'skipped': True}
    redis_status = _check_redis()
    ai_status = _check_ai_provider()
    movieserver = _check_http_service('movieserver', config.get('mshost'))
    emby = _check_http_service('emby', config.get('emby_host'))
    ai_provider = str(config.get('ai_provider', 'gemini'))
    result = {
        'ok': not missing and mysql.get('ok') and postgres.get('ok') and redis_status.get('ok') and ai_status.get('ok', True) and movieserver.get('ok', True) and emby.get('ok', True),
        'missing': missing,
        'proxy_mode': bool(config.get('proxy_mode', False)),
        'translation_enabled': bool(config.get('translation_enabled', True)),
        'transfer_notice_enabled': bool(config.get('transfer_notice_enabled', True)),
        'tmdb_bg_enabled': bool(config.get('tmdb_bg_enabled', False)),
        'ai_provider': ai_provider,
        'ai': ai_status,
        'mysql': mysql,
        'postgres': postgres,
        'redis': redis_status,
        'movieserver': movieserver,
        'emby': emby,
    }
    if missing:
        write_log(f'健康检查失败，缺少配置: {missing}', level='ERROR')
    elif result['ok'] and log_on_success:
        write_log('健康检查通过')
    elif not result['ok']:
        write_log(f'健康检查失败: {result}', level='ERROR')
    return result


if __name__ == '__main__':
    res = run_healthcheck(log_on_success=True)
    raise SystemExit(0 if res['ok'] else 1)
