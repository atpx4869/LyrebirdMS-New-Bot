from app_config import config
from http_client import get
from logger.logger import write_log

mshost = config['mshost']
mstoken = config['mstoken']
search_timeout = config.get('search_timeout', 60)


def search_media(query):
    url = f"{mshost}/api/v1/media/search?mediaSource=200&keyword={query}"
    headers = {'Authorization': f'{mstoken}'}
    try:
        resp = get(url, timeout=search_timeout, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        write_log(f'搜索媒体失败 query={query}: {e}', level='ERROR')
        return None
