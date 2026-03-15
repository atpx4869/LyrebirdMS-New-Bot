from datetime import datetime, timezone, timedelta
import re

from init import app
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from mediasaber.downloadMedia import download_media_torrent, analyze_torrent
from sql.mspostgre import get_torrent_info_by_id
from sql.embybot import read_user_info, update_user_info_free, update_user_info
from sql.msbot import insert_download_data
from logger.logger import write_log
from app_config import config
from runtime_state import bump_counter, record_event
from task_manager import create_task, mark_failed, mark_running, mark_success, claim_retry_tasks

coinsname = config['coinsname']
coins_per_1GB = config['coins_per_1GB']
group_id = config['group']
_TZ = timezone(timedelta(hours=8))


async def _send_message_or_photo(chat_id, text: str, poster: str | None = None, reply_markup=None):
    if poster:
        try:
            return await app.send_photo(chat_id=chat_id, photo=poster, caption=text, reply_markup=reply_markup)
        except Exception as e:
            write_log(f'发送海报消息失败，回退纯文本: {e}', level='WARNING')
    return await app.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def process_download_request(chat_id: int, user_id: int, first_name: str, media_id: int, message_date=None, source: str = 'telegram', existing_task_id: str | None = None, notify_group: bool = True):
    task = None
    if existing_task_id:
        task = {'id': existing_task_id}
        mark_running(existing_task_id, worker=f'{source}-retry')
    else:
        task = create_task('download', f'download:{media_id}', {'media_id': media_id, 'chat_id': chat_id, 'user_id': user_id}, created_by=user_id)
        mark_running(task['id'], worker=source)

    bump_counter('download_requests')
    record_event('download_requested', user_id=user_id, media_id=media_id, source=source)
    write_log(f'用户 {user_id} 请求下载媒体ID: {media_id} source={source}')

    res = get_torrent_info_by_id(media_id)
    if res is None:
        write_log(f'媒体ID {media_id} 不存在')
        await app.send_message(chat_id=chat_id, text='种子不存在，请重新搜索。')
        mark_failed(task['id'], 'torrent_not_found')
        return False

    size_bytes = res.get('size', 0)
    size_gb = int(size_bytes / (1024 ** 3))
    if size_gb < 1:
        size_gb = 1
    title = res.get('title', '')
    description = res.get('description', '')
    torrent_id = res.get('torrent_id', '')
    tmdbid = res.get('tmdb_id', 0)
    imdbid = res.get('imdb_id', '')

    analysis_res = analyze_torrent(title, description, imdbid)
    poster = None
    task_name = title

    if analysis_res and analysis_res.get('code') == 20000:
        data = analysis_res.get('data', {})
        if data.get('archived'):
            write_log('种子已归档，拒绝下载')
            await app.send_message(chat_id=chat_id, text='该资源已经入库了，若搜不到请尝试英文名 / 繁体名 / 年份。')
            mark_failed(task['id'], 'already_archived')
            return False

        tmdb_media = data.get('tmdbMedia') or {}
        if tmdb_media:
            tmdbid = tmdb_media.get('id', tmdbid)
            poster = tmdb_media.get('poster')

        metadata = data.get('metadata') or {}
        p_name = tmdb_media.get('title') or tmdb_media.get('name') or metadata.get('cnName') or metadata.get('enName') or title
        p_year = metadata.get('year') or tmdb_media.get('year')
        if not p_year:
            release_date = tmdb_media.get('release_date') or tmdb_media.get('first_air_date')
            if release_date:
                p_year = release_date[:4]

        task_name = p_name
        b_season = metadata.get('beginSeason')
        b_episode = metadata.get('beginEpisode')
        e_episode = metadata.get('endEpisode')
        is_movie = (metadata.get('mediaType') == 'movie' or tmdb_media.get('mediaType') == 'movie')
        is_invalid_se = (b_season == 0 and b_episode == 0)
        if not is_movie and not is_invalid_se:
            if b_season is not None:
                task_name += f' S{b_season:02d}'
            if b_episode is not None:
                if e_episode and e_episode != b_episode:
                    task_name += f' E{b_episode:02d}-E{e_episode:02d}'
                else:
                    task_name += f' E{b_episode:02d}'
        if p_year:
            task_name += f' ({p_year})'
    else:
        write_log('种子识别失败或未找到匹配信息', level='WARNING')
        await app.send_message(chat_id=chat_id, text='该种子无法识别，请换一个种子试试。')
        mark_failed(task['id'], 'torrent_analysis_failed')
        return False

    user_info = read_user_info(user_id)
    if user_info is None or user_info[1] == 'd':
        write_log(f'用户 {user_id} 无有效账号或被禁用')
        await app.send_message(chat_id=chat_id, text='您没有 LyrebirdEmby 的有效账号，无法下载。')
        mark_failed(task['id'], 'user_invalid')
        return False

    if user_info[1] == 'a':
        cost_coins = max(1, int(size_gb * 0.5 * coins_per_1GB))
    else:
        cost_coins = max(1, int(size_gb * coins_per_1GB))

    if user_info[0] + user_info[4] < cost_coins:
        write_log(f'用户 {user_id} 余额不足. 需要: {cost_coins}, 拥有: {user_info[0] + user_info[4]}')
        await app.send_message(chat_id=chat_id, text=f'余额不足，当前下载预计需要 {cost_coins} 个{coinsname}。')
        mark_failed(task['id'], f'insufficient_balance:{cost_coins}')
        return False

    status = download_media_torrent(media_id)
    if not status:
        bump_counter('download_failed')
        record_event('download_failed', user_id=user_id, media_id=media_id, source=source)
        write_log('下载请求失败: 可能已存在相同任务', level='WARNING')
        await app.send_message(chat_id=chat_id, text='下载失败：短时间内可能已提交过相同种子，请稍后再试。')
        mark_failed(task['id'], 'submit_failed_or_duplicate')
        return False

    if user_info[4] - cost_coins >= 0:
        real_cost = 0
    else:
        real_cost = cost_coins - user_info[4]

    update_user_info_free(user_id, -cost_coins, user_info[4])
    task_payload = {
        'media_id': media_id,
        'torrent_id': torrent_id,
        'title': task_name,
        'chat_id': chat_id,
        'user_id': user_id,
        'tmdbid': tmdbid,
        'size_gb': size_gb,
        'real_cost': real_cost,
        'cost_coins': cost_coins,
        'source': source,
    }

    insert_download_data(
        title=title,
        torrent_id=torrent_id,
        telegram_id=user_id,
        telegram_chat_id=chat_id,
        cost_coins=cost_coins,
        size=size_gb,
        date=message_date or datetime.now(_TZ),
        tmdbid=tmdbid,
    )

    mark_success(task['id'], task_payload)

    bump_counter('download_success')
    record_event('download_success', user_id=user_id, media_id=media_id, title=task_name, source=source)
    write_log(f'下载请求成功, 扣除积分: {cost_coins} source={source}')

    msg_text = (
        f'✅ **下载任务已提交**\n\n'
        f'🎬 标题：{task_name}\n'
        f'📦 大小：{size_gb} GB\n'
        f'💰 本次实际扣费：{real_cost} 个{coinsname}\n\n'
        '下载完成并入库后会自动通知你。'
    )

    await _send_message_or_photo(chat_id=chat_id, poster=poster, text=msg_text)

    group_msg = (
        f'🎉 [{first_name}](tg://user?id={user_id}) 提交了新任务\n'
        f'🎬 {task_name}\n'
        f'📦 {size_gb} GB\n'
        f'💰 花费 {real_cost} 个{coinsname}\n'
        '输入 /rate 可查看下载进度。'
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f'+5 {coinsname}', callback_data=f'tip_{user_id}_5'),
            InlineKeyboardButton(f'+10 {coinsname}', callback_data=f'tip_{user_id}_10'),
        ]
    ])

    if notify_group and group_id:
        await _send_message_or_photo(chat_id=group_id, poster=poster, text=group_msg, reply_markup=keyboard)

    return True


async def download_retry_watcher():
    while True:
        try:
            retry_tasks = claim_retry_tasks('download')
            for task in retry_tasks:
                payload = task.get('payload') or {}
                media_id = int(payload.get('media_id') or 0)
                chat_id = int(payload.get('chat_id') or 0)
                user_id = int(payload.get('user_id') or task.get('created_by') or 0)
                first_name = str(payload.get('first_name') or f'用户{user_id}')
                if not media_id or not chat_id or not user_id:
                    mark_failed(task.get('id', ''), 'retry_payload_invalid')
                    continue
                await process_download_request(
                    chat_id=chat_id,
                    user_id=user_id,
                    first_name=first_name,
                    media_id=media_id,
                    source='admin-retry',
                    existing_task_id=task.get('id'),
                    notify_group=False,
                )
        except Exception as e:
            write_log(f'下载重试观察器异常: {e}', level='ERROR')
        import asyncio
        await asyncio.sleep(10)


@app.on_message(filters.regex(r"^/download_(\d+)$"))
async def download_command_handler(client, message):
    match = re.match(r"^/download_(\d+)$", message.text)
    if not match:
        await app.send_message(chat_id=message.chat.id, text='命令格式错误，请使用 /download_媒体ID')
        return
    media_id = int(match.group(1))
    await process_download_request(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        first_name=message.from_user.first_name,
        media_id=media_id,
        message_date=message.date,
        source='telegram',
    )


@app.on_callback_query(filters.regex(r'^tip_(\d+)_(\d+)$'))
async def tip_callback_handler(client, callback_query):
    requester_id = int(callback_query.matches[0].group(1))
    amount = int(callback_query.matches[0].group(2))
    clicker_id = callback_query.from_user.id

    if clicker_id == requester_id:
        await callback_query.answer('不能给自己打赏哦', show_alert=True)
        return

    user_info = read_user_info(clicker_id)
    if user_info is None or user_info[1] == 'd':
        await callback_query.answer('你还没有号哦', show_alert=True)
        return

    user_coins = user_info[0]
    if user_coins < amount:
        await callback_query.answer(f'余额不足，需要 {amount} {coinsname} (不包含免费额度)', show_alert=True)
        return

    update_user_info(clicker_id, -amount)
    update_user_info(requester_id, amount)
    bump_counter('tip_success')
    record_event('tip_success', from_user=clicker_id, to_user=requester_id, amount=amount)
    write_log(f'用户 {clicker_id} 打赏用户 {requester_id} {amount} {coinsname}')

    clicker_name = callback_query.from_user.first_name
    message = callback_query.message
    current_text = message.caption if message.caption else message.text
    new_line = f'\n{clicker_name} 打赏了 {amount} {coinsname}'
    new_text = current_text + new_line

    try:
        if message.caption:
            await message.edit_caption(caption=new_text, reply_markup=message.reply_markup)
        else:
            await message.edit_text(text=new_text, reply_markup=message.reply_markup)
    except Exception as e:
        write_log(f'更新打赏消息失败: {e}', level='WARNING')

    try:
        msg_link = message.link
        notify_text = f'{msg_link}\n{clicker_name} 给您打赏了 {amount} 个{coinsname}。'
        await app.send_message(chat_id=requester_id, text=notify_text)
    except Exception as e:
        write_log(f'发送打赏通知失败: {e}', level='WARNING')

    await callback_query.answer('打赏成功！')
