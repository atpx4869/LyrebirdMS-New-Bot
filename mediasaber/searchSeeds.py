from app_config import config
from http_client import post
from logger.logger import write_log

mshost = config['mshost']
mstoken = config['mstoken']
search_timeout = config.get('search_timeout', 60)


def createTask_search_seeds(mediaId, title, mediaType, year, poster):
    url = f"{mshost}/api/v1/torrentSearch/media"
    headers = {'Authorization': f'{mstoken}', 'Content-Type': 'application/json'}
    payload = {
        'mediaSource': 200,
        'mediaId': mediaId,
        'title': title,
        'mediaType': mediaType,
        'year': year,
        'poster': poster,
    }
    try:
        response = post(url, headers=headers, json=payload, timeout=search_timeout).json()
        if response.get('message') != 'SUCCESS':
            write_log(f"创建种子搜索任务失败 mediaId={mediaId}: {response}", level='WARNING')
            return None
        return response.get('data')
    except Exception as e:
        write_log(f'创建种子搜索任务异常 mediaId={mediaId}: {e}', level='ERROR')
        return None


def getTask_search_seeds(taskId, keyword):
    url = f"{mshost}/api/v1/torrentSearch/page?pageNum=1&pageSize=500"
    headers = {'Authorization': f'{mstoken}', 'Content-Type': 'application/json'}
    payload = {'order': 100, 'keyword': keyword, 'options': {}, 'taskId': taskId}
    try:
        response = post(url, headers=headers, json=payload, timeout=search_timeout).json()
        if response.get('message') != 'SUCCESS':
            write_log(f"查询种子任务失败 taskId={taskId}: {response}", level='WARNING')
            return None
        return response.get('data')
    except Exception as e:
        write_log(f'查询种子任务异常 taskId={taskId}: {e}', level='ERROR')
        return None
