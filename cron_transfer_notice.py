import time
import requests
import asyncio
import base64
import re
from init import app
from sql.msbot import create_notified_transfers_table, is_transfer_notified, mark_transfer_notified, get_recent_downloads_by_tmdbid
from logger.logger import write_log
from app_config import config
from http_client import get
from runtime_state import bump_counter, record_event, set_bot_status


mshost = config['mshost']
mstoken = config['mstoken']

def normalize_and_tokenize(text):
    if not text:
        return set()
    # Replace common separators with space
    text = re.sub(r'[._\-\[\]\(\)]', ' ', text)
    # Lowercase and split
    return set(text.lower().split())

def is_fuzzy_match(title1, title2, threshold=0.75):
    tokens1 = normalize_and_tokenize(title1)
    tokens2 = normalize_and_tokenize(title2)
    
    if not tokens1 or not tokens2:
        return False
        
    intersection = tokens1.intersection(tokens2)
    if not intersection:
        return False
        
    # Calculate match ratio based on the shorter title to be more inclusive
    # This helps when one title has extra tags like "Complete" or "Netflix" that the other might miss
    min_len = min(len(tokens1), len(tokens2))
    ratio = len(intersection) / min_len
    return ratio >= threshold

def get_transfer_history():
    url = f"{mshost}/api/v1/transferHistory/page?pageNum=1&pageSize=25"
    headers = {
        "Authorization": mstoken
    }
    try:
        resp = get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        write_log(f"获取整理记录失败: {e}")
        return None

async def check_and_notify():
    # write_log("开始检查整理记录...", level="DEBUG")
    history_data = get_transfer_history()
    if not history_data or history_data.get('code') != 20000:
        write_log(f"获取整理记录接口返回异常: {history_data}")
        return

    transfer_list = history_data.get('data', {}).get('list', [])
    
    for item in transfer_list:
        transfer_id = item.get('id')
        if not transfer_id:
            continue
            
        if is_transfer_notified(transfer_id):
            continue
            
        tmdb_id = item.get('tmdbId')
        title = item.get('title', 'Unknown')
        write_log(f"发现新整理记录: ID={transfer_id}, Title={title}, TMDB={tmdb_id}")

        if not tmdb_id:
            # 如果没有tmdbId，可能无法匹配，标记为已处理防止重复检查
            write_log(f"记录 {transfer_id} 无 TMDB ID, 跳过")
            mark_transfer_notified(transfer_id)
            continue
            
        # 查找最近2天的下载记录
        recent_downloads = get_recent_downloads_by_tmdbid(tmdb_id)
        
        # 即使没有找到下载记录，也可能是非bot下载的，需要标记为已处理
        # 但为了保险起见，先尝试匹配，匹配完再标记
        
        dir_path = item.get('dir', '')
        if not dir_path:
            mark_transfer_notified(transfer_id)
            continue
            
        # 获取最后一级目录名
        dir_last_part = dir_path.rstrip('/').split('/')[-1]
        
        # 获取源文件名
        path_source = item.get('pathSource', '')
        source_filename = path_source.split('/')[-1] if path_source else ''
        
        target_user_id = None
        
        if recent_downloads:
            for download in recent_downloads:
                db_title = download.get('title', '')
                if not db_title:
                    continue
                    
                # 将数据库中的title的值的空格均替换为.
                formatted_title = db_title.replace(' ', '.')
                
                # 尝试更宽松的匹配逻辑
                # 1. 检查目录名或源文件名是否包含数据库中的标题（替换空格为.后）
                # 2. 检查目录名或源文件名是否包含数据库中的标题（原始标题）
                # 3. 检查数据库标题是否包含在源文件名中（忽略大小写）
                # 4. 使用分词模糊匹配
                
                match_found = False
                if formatted_title in dir_last_part or (source_filename and formatted_title in source_filename):
                    match_found = True
                elif db_title in dir_last_part or (source_filename and db_title in source_filename):
                    match_found = True
                elif source_filename and db_title.lower() in source_filename.lower():
                    match_found = True
                elif is_fuzzy_match(db_title, dir_last_part) or (source_filename and is_fuzzy_match(db_title, source_filename)):
                    match_found = True
                
                if match_found:
                    target_user_id = download.get('telegram_id')
                    write_log(f"匹配成功! TransferID={transfer_id} -> UserID={target_user_id} (Title={db_title})")
                    break
        
        if target_user_id:
            # 发送通知
            year = item.get('year', '')
            media_type = item.get('mediaType', '')
            poster_url = item.get('poster', '')
            
            caption = f"🎉 **下载完成并入库**\n\n"
            caption += f"🎬 **{title} ({year})**\n"
            
            if media_type == 'tv':
                season = item.get('seasonNumber')
                episode = item.get('episodeNumber')
                caption += f"📺 第 {season} 季 第 {episode} 集\n"
            
            tmdb_link = ""
            if media_type == 'movie':
                tmdb_link = f"https://www.themoviedb.org/movie/{tmdb_id}"
            elif media_type == 'tv':
                tmdb_link = f"https://www.themoviedb.org/tv/{tmdb_id}"
            
            caption += f"\n🔗 [TMDB Link]({tmdb_link})"
            
            # 处理 Poster
            real_poster_url = poster_url
            if poster_url and '/image/200/' in poster_url:
                try:
                    b64_part = poster_url.split('/image/200/')[-1]
                    # base64 padding
                    missing_padding = len(b64_part) % 4
                    if missing_padding:
                        b64_part += '=' * (4 - missing_padding)
                    decoded_bytes = base64.urlsafe_b64decode(b64_part)
                    real_poster_url = decoded_bytes.decode('utf-8')
                except Exception as e:
                    write_log(f"Poster decode failed: {e}", level="WARNING")
            
            try:
                await app.send_photo(chat_id=int(target_user_id), photo=real_poster_url, caption=caption)
                bump_counter('transfer_notice_success')
                record_event('transfer_notice_sent', user_id=target_user_id, title=title)
                write_log(f'通知发送成功: User={target_user_id}, Media={title}')
            except Exception as e:
                bump_counter('transfer_notice_failed')
                write_log(f'发送通知失败: {e}', level='ERROR')
                try:
                    await app.send_message(chat_id=int(target_user_id), text=caption)
                except Exception as e2:
                    write_log(f"发送文本通知也失败: {e2}", level="ERROR")
        else:
            write_log(f"未找到匹配用户, 可能是非Bot下载或已过期. TransferID={transfer_id}")
            
        # 无论是否找到用户，都标记为已通知，避免重复处理
        mark_transfer_notified(transfer_id)

async def run_transfer_notice_loop():
    write_log('Cron Transfer Notice Service Started')
    record_event('transfer_notice_loop_started')
    create_notified_transfers_table()
    while True:
        try:
            await check_and_notify()
        except Exception as e:
            write_log(f"Cron loop error: {e}", level="ERROR")
        
        await asyncio.sleep(60)
