from init import app
from pyrogram import filters
from pyrogram.types import CallbackQuery
from pyrogram.enums import ChatType

from app_config import config
from mediasaber.searchRate import get_downloading_list
from runtime_state import bump_counter, record_event

coinsname = config.get('coinsname', '金币')


def format_progress(progress):
    try:
        percent = float(progress) * 100
        return f'{percent:.1f}%'
    except Exception:
        return '未知'


def build_private_caption(item):
    title = item.get('title', '未知标题')
    year = item.get('year', '未知年份')
    season_episode = item.get('seasonEpisode', '')
    progress = format_progress(item.get('progress', 0))
    speed = item.get('downloadSpeed') or item.get('speed') or '未知速率'
    if season_episode:
        return f'🎬 **{title}** ({year})\n📺 {season_episode}\n📈 进度：{progress}\n⚡ 速度：{speed}'
    return f'🎬 **{title}** ({year})\n📈 进度：{progress}\n⚡ 速度：{speed}'


def build_group_line(item):
    title = item.get('title', '未知标题')
    season_episode = item.get('seasonEpisode', '')
    progress = format_progress(item.get('progress', 0))
    return f'- {title} {season_episode} {progress}'.strip()


@app.on_message(filters.command('rate'))
async def rate_command_handler(client, msg):
    bump_counter('rate_commands')
    record_event('user_rate', user_id=msg.from_user.id)
    downloading_list = get_downloading_list()
    if downloading_list is None:
        await app.send_message(chat_id=msg.chat.id, text='获取下载进度失败，请稍后重试。')
        return
    if not downloading_list or not downloading_list.get('data'):
        await app.send_message(chat_id=msg.chat.id, text='当前没有正在下载的任务。')
        return

    is_private = msg.chat.type == ChatType.PRIVATE
    data = downloading_list['data']
    if is_private:
        for item in data:
            poster = item.get('poster')
            caption = build_private_caption(item)
            if poster:
                try:
                    await app.send_photo(chat_id=msg.chat.id, photo=poster, caption=caption)
                except Exception:
                    await app.send_message(chat_id=msg.chat.id, text=caption)
            else:
                await app.send_message(chat_id=msg.chat.id, text=caption)
    else:
        msg_text = '📦 **当前下载任务**\n\n' + '\n'.join(build_group_line(item) for item in data)
        await app.send_message(chat_id=msg.chat.id, text=msg_text.strip())


@app.on_callback_query(filters.regex('searchRate'))
async def search_rate_callback_handler(client, callback_query: CallbackQuery):
    downloading_list = get_downloading_list()
    if downloading_list is None:
        await callback_query.answer('获取下载进度失败，请稍后重试。', show_alert=True)
        return
    if not downloading_list or not downloading_list.get('data'):
        await callback_query.answer('当前没有正在下载的任务。', show_alert=True)
        return

    for item in downloading_list['data']:
        poster = item.get('poster')
        caption = build_private_caption(item)
        if poster:
            try:
                await app.send_photo(chat_id=callback_query.from_user.id, photo=poster, caption=caption)
            except Exception:
                await app.send_message(chat_id=callback_query.from_user.id, text=caption)
        else:
            await app.send_message(chat_id=callback_query.from_user.id, text=caption)
    await callback_query.answer('已发送下载进度')
