import asyncio, math
from app_config import config
from mediasaber.searchMedia import search_media
from mediasaber.searchSeeds import createTask_search_seeds, getTask_search_seeds
from init import app
from sql.embybot import read_user_info
from pyrogram import filters
from pyrogram.errors import InputUserDeactivated
from pyrogram.enums import ListenerTypes
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from datetime import datetime, timedelta, timezone
from logger.logger import write_log
from runtime_state import bump_counter, record_event
from session_store import session_store

name = config['name']
coinsname = config['coinsname']
search_timeout = config.get('search_timeout', 60)

# 放在文件顶部或用到前
def format_size(size):
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"
    else:
        return f"{size / (1024 * 1024):.2f} MB"

def format_pubdate(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# 构造每一页的内容，增加TMDB超链接
def build_caption(item, idx, total):
    title = item.get('title', '')
    year = item.get('year', '')
    vote = item.get('vote', '')
    overview = item.get('overview', '')
    type_ = item.get('type', '')
    tmdbid = item.get('id', '') or item.get('tmdbId', '') or item.get('tmdb_id', '')
    tmdb_link = ""
    if tmdbid:
        tmdb_link = f"\n[[TMDBLink](https://www.themoviedb.org/{'movie' if type_ == '电影' or type_ == 'movie' else 'tv'}/{tmdbid})]"
    caption = f"【{title}】({year})\n类型: {type_}\n评分: {vote}\n\n{overview}{tmdb_link}\n\n第{idx+1}/{total}条结果"
    return caption

def build_keyboard(idx, total):
    btns = []
    if idx > 0:
        btns.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"seed_prev_{idx-1}"))
    btns.append(InlineKeyboardButton("✅ 确定", callback_data=f"seed_ok_{idx}"))
    if idx < total - 1:
        btns.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"seed_next_{idx+1}"))
    return InlineKeyboardMarkup([btns])

# 构造种子分页内容
def build_seeds_caption(seeds, page, page_size, total, filter_keyword=None):
    msg_lines = []
    start = page * page_size
    end = min(start + page_size, total)
    for idx, seed in enumerate(seeds[start:end], start=1+start):
        title = seed.get("title", "无")
        description = seed.get("description", "无")
        id = seed.get("id", "无")
        size = format_size(seed.get("size", 0))
        seeders = seed.get("seeders", 0)
        labels = "、".join(seed.get("labels", [])) if seed.get("labels") else "无"
        pubdate = format_pubdate(seed.get("pubDate", 0)) if seed.get("pubDate") else "无"
        msg_lines.append(
            f"【{idx}】标题：{title}\n"
            f"描述：{description}\n"
            f"大小：{size}\n"
            f"上传人数：{seeders}\n"
            f"标签：{labels}\n"
            f"发布日期：{pubdate}\n"
            f"下载指令：/download_{id}"
        )
    caption = "\n\n".join(msg_lines)
    caption += f"\n\n第{page+1}/{max(1, math.ceil(total/page_size))}页，共{total}个种子"
    if filter_keyword:
        caption += f"\n\n当前过滤词：{filter_keyword}"
    return caption

def build_seeds_keyboard(page, total, page_size):
    btns = []
    max_page = max(0, math.ceil(total / page_size) - 1)
    if page > 0:
        btns.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"seeds_page_{page-1}"))
    if page < max_page:
        btns.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"seeds_page_{page+1}"))
    return InlineKeyboardMarkup([btns]) if btns else None

# 会话缓存：优先 Redis，其次 runtime/session_cache.json
DEFAULT_POSTER = "bgimg.jpg"


def _cache_key(user_id):
    return f"search:{user_id}"


def _get_cache(user_id):
    return session_store.get(_cache_key(user_id)) or {}


def _set_cache(user_id, value):
    session_store.set(_cache_key(user_id), value)


def _update_cache(user_id, patch):
    return session_store.update(_cache_key(user_id), patch)


# ========== 修正：种子翻页按钮不再触发seeds_search主入口 ==========
@app.on_callback_query(filters.regex(r"seeds_search"))
async def seeds_search(_, msg):
    userinfo = read_user_info(msg.from_user.id)
    bump_counter('seed_search_enter')
    record_event('seed_search_enter', user_id=msg.from_user.id)
    try:
        result = await app.ask(
            chat_id=msg.message.chat.id,
            text=f"👋您当前免费下片消费额度为{userinfo[4]}个{coinsname}\n⏳请在120s内发送你的 求片名称\n❌提前终止请输入 /cancel",
            timeout=120,
            listener_type=ListenerTypes.MESSAGE,
        )
        if not result or result.text == "/cancel" or result.text == "":
            await app.send_message(
                chat_id=msg.message.chat.id,
                text=f"已取消搜索"
            )
            return
        if " " in result.text:
            search_text = result.text.split(" ")[0]
            keyword = result.text.split(" ")[1]
        else:
            search_text = result.text
            keyword = ""
        write_log(f'用户 {msg.from_user.id} 开始搜索: {search_text}')
        # 发送初始消息
        message = await app.send_message(
            chat_id=msg.message.chat.id,
            text=f"正在为您搜索: {search_text}\n🔍"
        )

        searching = True
        min_dot_count = 5  # 最多5个点

        # 用于存储搜索结果
        media_info = None

        async def search_task_func():
            nonlocal media_info
            loop = asyncio.get_event_loop()
            media_info = await loop.run_in_executor(None, search_media, search_text)

        async def update_message():
            dot_count = 0
            while searching:
                dots = '.' * (dot_count + 1)
                try:
                    await message.edit_text(f"正在为您搜索: {search_text}\n🔍{dots}")
                except InputUserDeactivated:
                    break
                except Exception:
                    pass
                dot_count = (dot_count + 1) % min_dot_count
                await asyncio.sleep(1)

        # 启动消息更新任务
        update_task = asyncio.create_task(update_message())
        # 启动搜索任务
        search_task = asyncio.create_task(search_task_func())

        try:
            dot_round = 0
            max_dot_round = int(search_timeout / 5) + 2 # 稍微多一点余量
            while True:
                # 等待5个点
                for _ in range(min_dot_count):
                    await asyncio.sleep(1)
                if search_task.done():
                    break
                dot_round += 1
                if dot_round >= max_dot_round:
                    # 已经等了max_dot_round轮5个点还没结果，判定为超时
                    await app.send_message(
                        chat_id=msg.message.chat.id,
                        text="搜索失败：索引器响应超时，请稍后重试。"
                    )
                    searching = False
                    await asyncio.sleep(0.1)
                    update_task.cancel()
                    try:
                        await update_task
                    except asyncio.CancelledError:
                        pass
                    return
            # 等待搜索任务完成
            await search_task
            # 搜索完成后，编辑消息为"搜索完成✅"
            try:
                await message.edit_text(f"搜索完成✅")
            except Exception:
                pass
        finally:
            searching = False
            await asyncio.sleep(0.1)  # 等待最后一次编辑完成
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass

        # 搜索完成后可以在这里处理media_info，比如发送结果等
        if media_info is None:
            await app.send_message(
                chat_id=msg.message.chat.id,
                text='搜索失败：索引器状态异常，请稍后重试。'
            )
            return
        if media_info['data']['total'] == 0:
            await app.send_message(
                chat_id=msg.message.chat.id,
                text='未找到相关资源，建议尝试英文名、繁体名或加上年份。'
            )
            return

        # 取出搜索结果列表
        results = media_info['data']['list']
        total = media_info['data']['total']
        if not results:
            await app.send_message(
                chat_id=msg.message.chat.id,
                text="未找到相关资源"
            )
            return

        # 缓存本次搜索结果，key为用户id
        _set_cache(msg.from_user.id, {
            "results": results,
            "search_text": search_text,
            "keyword": keyword
        })

        # 发送第一页
        first_idx = 0
        first_item = results[first_idx]
        poster_url = first_item.get('poster') or first_item.get('posterProxy') or None
        caption = build_caption(first_item, first_idx, total)
        keyboard = build_keyboard(first_idx, total)

        sent_msg = await app.send_photo(
            chat_id=msg.message.chat.id,
            photo=poster_url if poster_url else DEFAULT_POSTER,
            caption=caption,
            reply_markup=keyboard
        )

        # 记录消息id用于后续翻页
        _update_cache(msg.from_user.id, {"message_id": sent_msg.id})
        _update_cache(msg.from_user.id, {"chat_id": msg.message.chat.id})

    except Exception as e:
        await app.send_message(
            chat_id=msg.message.chat.id,
            text='搜索已退出，请重新开始。'
        )
        write_log(f'搜索流程异常 user={msg.from_user.id}: {e}', level='ERROR')

# 处理翻页和确定按钮
@app.on_callback_query(filters.regex(r"seed_(prev|next|ok)_(\d+)"))
async def seeds_pagination_handler(client, callback_query):
    user_id = callback_query.from_user.id
    data = callback_query.data
    action, idx = data.split("_")[1], int(data.split("_")[2])

    # 检查缓存
    cache = _get_cache(user_id)
    if not cache:
        await callback_query.answer("会话已过期，请重新搜索", show_alert=True)
        return

    results = cache["results"]
    total = len(results)
    if idx < 0 or idx >= total:
        await callback_query.answer("无效的页码", show_alert=True)
        return

    item = results[idx]
    poster_url = item.get('poster') or item.get('posterProxy') or None
    caption = build_caption(item, idx, total)
    keyboard = build_keyboard(idx, total)

    # 只允许编辑自己发的消息
    chat_id = cache["chat_id"]
    message_id = cache["message_id"]

    if action in ["prev", "next"]:
        try:
            await client.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaPhoto(
                    media=poster_url if poster_url else "https://dummyimage.com/300x450/cccccc/ffffff&text=No+Image",
                    caption=caption
                ),
                reply_markup=keyboard
            )
        except Exception as e:
            await callback_query.answer("翻页失败", show_alert=True)
            return
        await callback_query.answer()
    elif action == "ok":
        # 这里可以处理用户确定选择的逻辑
        keyword = cache.get("keyword", "")

        # 发送初始消息
        search_title = item['title']
        message = await client.send_message(
            chat_id=chat_id,
            text=f"正在为您搜索 {search_title} 种子\n🔍"
        )

        searching = True
        min_dot_count = 5  # 最多5个点

        async def update_message():
            dot_count = 0
            while searching:
                dots = '.' * (dot_count + 1)
                try:
                    await message.edit_text(f"正在为您搜索 {search_title} 种子\n🔍{dots}")
                except Exception:
                    pass
                dot_count = (dot_count + 1) % min_dot_count
                await asyncio.sleep(1)

        # 启动消息更新任务
        update_task = asyncio.create_task(update_message())

        # 提交索引任务（只需一次，不需要多次尝试）
        loop = asyncio.get_event_loop()
        taskId = await loop.run_in_executor(None, createTask_search_seeds, item['id'], item['title'], item['type'], item['year'], item['poster'])
        if taskId is None:
            searching = False
            await asyncio.sleep(0.1)
            update_task.cancel()
            await client.send_message(
                chat_id=chat_id,
                text="索引器找不到索引任务"
            )
            return

        # 获取种子列表，重试逻辑放在这里
        max_retry = search_timeout
        retry_count = 0
        seeds_data = None
        try:
            # 提交任务后，第一次获取种子列表要等待2秒
            await asyncio.sleep(2)
            while retry_count < max_retry:
                seeds_data = await loop.run_in_executor(None, getTask_search_seeds, taskId, keyword)
                # 修正：有些索引器返回的list长度小于total，强制以list长度为准
                if seeds_data is not None and "list" in seeds_data and isinstance(seeds_data["list"], list):
                    if "total" not in seeds_data or seeds_data["total"] != len(seeds_data["list"]):
                        seeds_data["total"] = len(seeds_data["list"])
                
                if seeds_data is not None and seeds_data.get('total', 0) > 0:
                    break
                
                retry_count += 1
                await asyncio.sleep(1)
        finally:
            searching = False
            await asyncio.sleep(0.1)
            update_task.cancel()
        if seeds_data is None:
            try:
                await message.edit_text("获取种子列表失败")
            except:
                await client.send_message(
                    chat_id=chat_id,
                    text="获取种子列表失败"
                )
            return
        if seeds_data.get('total', 0) == 0:
            try:
                await message.edit_text("该资源未找到任何种子")
            except:
                await client.send_message(
                    chat_id=chat_id,
                    text="该资源未找到任何种子"
                )
            return
        seeds_total = seeds_data['total']
        seeds_list = seeds_data['list']

        # 缓存种子列表和分页信息
        _update_cache(user_id, {"seeds_list": seeds_list})
        _update_cache(user_id, {"filtered_seeds_list": seeds_list}) # 初始化过滤列表为全部
        _update_cache(user_id, {"filter_keyword": None}) # 初始化过滤词为空
        _update_cache(user_id, {"seeds_total": seeds_total})
        _update_cache(user_id, {"seeds_page": 0})

        # 发送第一页种子
        page_size = 5
        page = 0
        caption = build_seeds_caption(seeds_list, page, page_size, seeds_total)
        keyboard = build_seeds_keyboard(page, seeds_total, page_size)
        
        try:
            sent_msg = await message.edit_text(
                text=caption,
                reply_markup=keyboard
            )
        except:
            sent_msg = await client.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=keyboard
            )
        
        _update_cache(user_id, {"seeds_message_id": sent_msg.id})
        _update_cache(user_id, {"seeds_chat_id": chat_id})

# 种子分页回调
@app.on_callback_query(filters.regex(r"^seeds_page_(\d+)$"))
async def seeds_page_callback(client, callback_query):
    user_id = callback_query.from_user.id
    cache = _get_cache(user_id)
    if not cache or "seeds_list" not in cache:
        await callback_query.answer("会话已过期，请重新搜索", show_alert=True)
        return
    try:
        page = int(callback_query.data.split("_")[-1])
    except Exception:
        await callback_query.answer("无效的页码", show_alert=True)
        return

    # 使用过滤后的列表
    seeds_list = cache.get("filtered_seeds_list", cache["seeds_list"])
    seeds_total = len(seeds_list)
    filter_keyword = cache.get("filter_keyword")
    
    page_size = 5
    max_page = max(0, math.ceil(seeds_total / page_size) - 1)

    # 校正页码范围
    if page < 0:
        page = 0
    if page > max_page:
        page = max_page

    # 更新缓存中的当前页码
    cache = _update_cache(user_id, {"seeds_page": page})

    caption = build_seeds_caption(seeds_list, page, page_size, seeds_total, filter_keyword)
    keyboard = build_seeds_keyboard(page, seeds_total, page_size)
    chat_id = cache["seeds_chat_id"]
    message_id = cache["seeds_message_id"]
    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=caption,
            reply_markup=keyboard
        )
    except Exception as e:
        # 处理消息已被删除或内容未变等异常
        await callback_query.answer("翻页失败", show_alert=True)
        return
    await callback_query.answer()

@app.on_message(filters.command("filter"))
async def filter_seeds_handler(client, message):
    user_id = message.from_user.id
    cache = _get_cache(user_id)
    
    # 检查是否回复了消息
    if not message.reply_to_message:
        await message.reply_text("您需要回复对应的种子信息才能过滤！")
        return

    # 检查是否有活跃的种子搜索会话，且回复的消息ID匹配
    if not cache or "seeds_list" not in cache or "seeds_message_id" not in cache or message.reply_to_message.id != cache["seeds_message_id"]:
        await message.reply_text("您需要回复对应的种子信息才能过滤！")
        return

    # 解析参数
    args = message.text.split(maxsplit=1)
    keyword = args[1] if len(args) > 1 else None
    
    original_seeds = cache["seeds_list"]
    
    if keyword:
        # 执行过滤
        filtered_seeds = []
        kw_lower = keyword.lower()
        for seed in original_seeds:
            title = seed.get("title", "").lower()
            desc = seed.get("description", "").lower()
            if kw_lower in title or kw_lower in desc:
                filtered_seeds.append(seed)
        
        cache = _update_cache(user_id, {"filtered_seeds_list": filtered_seeds, "filter_keyword": keyword, "seeds_total": len(filtered_seeds)}) # 更新总数以便分页计算
    else:
        # 清除过滤
        cache = _update_cache(user_id, {"filtered_seeds_list": original_seeds, "filter_keyword": None, "seeds_total": len(original_seeds)})

    # 重置页码
    cache = _update_cache(user_id, {"seeds_page": 0})
    page = 0
    page_size = 5
    seeds_list = cache["filtered_seeds_list"]
    seeds_total = len(seeds_list)
    filter_keyword = cache.get("filter_keyword")

    caption = build_seeds_caption(seeds_list, page, page_size, seeds_total, filter_keyword)
    keyboard = build_seeds_keyboard(page, seeds_total, page_size)
    
    chat_id = cache["seeds_chat_id"]
    message_id = cache["seeds_message_id"]
    
    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=caption,
            reply_markup=keyboard
        )
    except Exception as e:
        await message.reply_text("请用\"回复的方式\"回复有效的种子集合信息！")
