from app_config import config
from http_client import post
from logger.logger import write_log

mshost = config['mshost']
mstoken = config['mstoken']


def get_downloading_list():
    url = f"{mshost}/api/v1/download/downloading"
    headers = {'Content-Type': 'application/json', 'Authorization': f'{mstoken}'}
    try:
        resp = post(url, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        write_log(f'获取下载中任务失败: {e}', level='ERROR')
        return None
