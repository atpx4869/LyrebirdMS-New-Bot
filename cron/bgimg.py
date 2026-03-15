import random

from app_config import config
from http_client import get
from logger.logger import write_log


def download_tmdb_top_movie_poster(api_key):
    try:
        url = f'https://api.themoviedb.org/3/movie/popular?api_key={api_key}&language=zh-CN&page=1'
        resp = get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        movies = data.get('results', [])[:10]
        movies_with_poster = [m for m in movies if m.get('poster_path')]
        if not movies_with_poster:
            return None
        selected_movie = random.choice(movies_with_poster)
        poster_url = f"https://image.tmdb.org/t/p/original{selected_movie.get('poster_path')}"
        img_resp = get(poster_url, timeout=10)
        img_resp.raise_for_status()
        with open('bgimg.jpg', 'wb') as f:
            f.write(img_resp.content)
        return 'bgimg.jpg'
    except Exception as e:
        write_log(f'下载 TMDB 海报失败: {e}', level='WARNING')
        return None


if __name__ == '__main__':
    tmdb_api_key = config.get('tmdb_api_key')
    if tmdb_api_key and config.get('tmdb_bg_enabled', False):
        download_tmdb_top_movie_poster(tmdb_api_key)
