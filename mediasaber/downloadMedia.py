from app_config import config
from http_client import post
from logger.logger import write_log

mshost = config['mshost']
mstoken = config['mstoken']


def download_media_torrent(media_id):
    url = f"{mshost}/api/v1/download/mediaTorrent"
    headers = {'Authorization': mstoken, 'Content-Type': 'application/json'}
    payload = {'downloaderId': 1, 'paramsId': 1, 'directoryId': 0, 'id': media_id}
    try:
        response = post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get('message') != 'SUCCESS':
            write_log(f'下载媒体种子接口返回失败 media_id={media_id}: {data}', level='WARNING')
            return None
        return data
    except Exception as e:
        write_log(f'下载媒体种子失败 media_id={media_id}: {e}', level='ERROR')
        return None


def analyze_torrent(title, subtitle, imdbid):
    url = f"{mshost}/api/v1/torrent/analysis"
    headers = {'Authorization': mstoken, 'Content-Type': 'application/json'}
    payload = {'dirTag': '', 'imdbId': imdbid or '', 'subtitle': subtitle or '', 'title': title or ''}
    try:
        response = post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        write_log(f'识别种子失败 title={title}: {e}', level='ERROR')
        return None
