import os
import sys
import time
import asyncio
import subprocess
import shutil
import tempfile
from init import app
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sql.embybot import read_user_info, update_user_info
from logger.logger import write_log
from task_manager import create_task, mark_failed, mark_running, mark_success, claim_retry_tasks, get_task, update_task
from runtime_state import bump_counter, record_event, merge_state
from menu.startMenu import function_menu
from pyrogram.errors import ListenerTimeout
from app_config import config
from http_client import get, post

coinsname = config["coinsname"]
name = config["name"]
group = config.get("group", None)
gemini_gst_batchsize = config.get("gemini_gst_batchsize", 1000)
gemini_model = config.get("gemini_model", "gemini-2.5-flash")
# 队列、锁、任务集合
translation_queue = asyncio.Queue()
translation_lock = asyncio.Lock()
is_translation_worker_running = False
is_translation_retry_watcher_running = False
active_translation_tasks = set()

import gemini_srt_translator as gst
import gemini_srt_translator.logger as gst_logger
from translation_providers import translate_srt_file

# ...existing code...

def extract_subtitle(media_path, subtitle_index, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # 优先使用 ffmpeg 提取并转换为 srt，速度通常更快且兼容性更好
    tool_used = "ffmpeg"
    extract_command = [
        "ffmpeg", "-y",
        "-nostdin",
        "-i", media_path,
        "-map", f"0:{subtitle_index}",
        "-c:s", "srt",
        output_path
    ]
        
    process = subprocess.run(
        extract_command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900
    )
    if process.returncode != 0:
        raise Exception(f"字幕提取失败: {process.stderr}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise Exception("字幕提取失败: 提取的字幕文件为空或不存在")
    return tool_used

def translate_subtitle(input_file, output_file, api_key, progress_callback=None):
    write_log(f"准备开始翻译字幕: {input_file} -> {output_file}")
    provider = str(config.get('ai_provider', 'gemini')).strip().lower()

    if os.path.exists(output_file):
        try:
            os.remove(output_file)
            write_log(f"已删除存在的输出文件: {output_file}")
        except Exception as e:
            write_log(f"删除旧输出文件失败: {e}", level="WARNING")

    # 对 openai_compatible / gemini_api 走统一 provider 实现；对 gemini 保留旧 gst 兼容逻辑
    if provider in {'openai_compatible', 'gemini_api'}:
        used_provider, _ = translate_srt_file(input_file, output_file, progress_callback=progress_callback)
        return used_provider

    script_content = f"""
import gemini_srt_translator as gst
import gemini_srt_translator.logger as gst_logger
import sys
import os

sys.stdin = open(os.devnull, 'r')
gst.gemini_api_key = "{api_key}"
gst.target_language = "Chinese"
gst.input_file = "{input_file}"
gst.output_file = "{output_file}"
gst.model_name = "{gemini_model}"
gst.batch_size = {gemini_gst_batchsize}
gst.free_quota = True
gst.skip_upgrade = True
gst.quiet = False

def auto_input_prompt(message):
    if "start from" in message:
        return "1"
    elif "end at" in message:
        return "999999"
    elif "continue" in message.lower():
        return "y"
    else:
        return ""
gst_logger.input_prompt = auto_input_prompt

try:
    print("开始执行 gst.translate()...")
    gst.translate()
    print("gst.translate() 执行完成")
except Exception as e:
    print(f"Error: {{e}}", file=sys.stderr)
    sys.exit(1)
"""

    fd, script_path = tempfile.mkstemp(suffix='.py', text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(script_content)
        write_log(f"已生成隔离翻译脚本: {script_path}，开始执行...")
        process = subprocess.Popen([sys.executable, script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True)
        last_log_time = 0
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if not output:
                continue
            output_stripped = output.strip()
            if not output_stripped:
                continue
            is_error = "unexpected line" in output_stripped or "Error" in output_stripped
            current_time = time.time()
            if is_error:
                if "unexpected line" in output_stripped:
                    write_log(f"⚠️ 模型可能产生了幻觉或格式错误，正在重试... ({output_stripped})", level="WARNING")
                else:
                    write_log(f"GST输出: {output_stripped}")
            elif current_time - last_log_time >= 15:
                write_log(f"GST输出: {output_stripped}")
                last_log_time = current_time
                if progress_callback and "Translating:" in output_stripped:
                    try:
                        progress_callback(output_stripped)
                    except Exception as e:
                        write_log(f"进度回调执行失败: {e}", level="WARNING")
        return_code = process.wait()
        stderr_output = process.stderr.read()
        if stderr_output:
            write_log(f"GST错误输出: {stderr_output}", level="ERROR")
        if return_code != 0:
            error_msg = f"翻译子进程执行失败 (Code {return_code})"
            write_log(error_msg, level="ERROR")
            raise Exception(f"翻译失败: {stderr_output}")
        if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            error_msg = "翻译过程中出现错误，未能生成有效的翻译文件或文件为空"
            write_log(error_msg, level="ERROR")
            raise Exception(error_msg)
        write_log(f"翻译成功，输出文件大小: {os.path.getsize(output_file)} bytes")
        return 'gemini'
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)

def copy_subtitle_to_media_dir(source_path, media_path):
    media_dir = os.path.dirname(media_path)
    media_filename = os.path.basename(media_path)
    media_name_without_ext = os.path.splitext(media_filename)[0]
    new_subtitle_filename = f"{media_name_without_ext}.Gemini译中.srt"
    target_subtitle_path = os.path.join(media_dir, new_subtitle_filename)
    shutil.copy2(source_path, target_subtitle_path)
    return target_subtitle_path

def cleanup_temp_files(directory):
    if os.path.exists(directory):
        try:
            os.remove(directory)
        except Exception as e:
            write_log(f"清理临时文件失败: {str(e)}", level="WARNING")

async def translation_worker():
    global is_translation_worker_running
    is_translation_worker_running = True
    while True:
        try:
            task_data = await translation_queue.get()
            chat_id = task_data['chat_id']
            user_id = task_data['user_id']
            user_name = task_data['user_name']
            media_path = task_data['media_path']
            item_name = task_data['item_name']
            subtitle_index = task_data['subtitle_index']
            subtitle_title = task_data['subtitle_title']
            item_id = task_data['item_id']
            task_id = task_data.get('task_id', '')
            task_identifier = f"{media_path}_{subtitle_index}"
            
            if task_id:
                mark_running(task_id, worker='translation_worker')
            write_log(f"开始处理用户 {user_name}({user_id}) 的翻译任务: {item_name} - {subtitle_title}")
            await app.send_message(chat_id, f"您的翻译任务已开始处理\n影片: {item_name}\n字幕: {subtitle_title}")
            try:
                os.makedirs("src/subtitles", exist_ok=True)
                input_subtitle_path = f"src/subtitles/subtitles_{user_id}.srt"
                output_subtitle_path = f"src/subtitles/subtitles_translated_{user_id}.srt"
                async with translation_lock:
                    start_time = time.time()
                    
                    write_log(f"正在提取字幕: {media_path} (Index: {subtitle_index})")
                    tool_used = await app.run_sync(extract_subtitle, media_path, subtitle_index, input_subtitle_path)
                    write_log(f"字幕提取成功，使用工具: {tool_used}")
                    # await app.send_message(chat_id, f"字幕提取成功，正在翻译中...")
                    
                    provider_name = str(config.get('ai_provider', 'gemini'))
                    write_log(f"正在调用 AI 进行翻译 provider={provider_name}...")
                    
                    # 发送初始进度消息
                    progress_msg = await app.send_message(chat_id, "🚀 正在初始化翻译引擎...")
                    
                    # 定义进度回调函数
                    def update_progress(progress_text):
                        # 提取百分比和进度条
                        # 格式示例: Translating: |███░░░| 12% (300/2407)
                        try:
                            # 简单的防抖动，避免更新太频繁（虽然日志已经限流了，但这里再加一层保险）
                            # 注意：这里是在子线程调用的，不能直接 await，需要用 run_coroutine_threadsafe
                            # 但为了简化，我们只在日志输出时调用，频率已经很低了
                            
                            # 构造更友好的显示文本
                            # 提取百分比
                            import re
                            match = re.search(r'(\d+)%', progress_text)
                            percent = match.group(1) if match else "??"
                            
                            new_text = (
                                f"🚀 正在翻译中...\n"
                                f"影片: {item_name}\n"
                                f"进度: {percent}%\n"
                                f"```\n{progress_text}\n```"
                            )
                            
                            # 异步更新消息
                            asyncio.run_coroutine_threadsafe(
                                progress_msg.edit_text(new_text),
                                app.loop
                            )
                        except Exception as e:
                            pass # 忽略更新失败，不影响主流程

                    # 使用 run_in_executor 在线程池中运行同步的 translate_subtitle 函数，避免阻塞事件循环
                    used_provider = await asyncio.get_running_loop().run_in_executor(
                        None, 
                        translate_subtitle, 
                        input_subtitle_path, 
                        output_subtitle_path, 
                        config.get('gemini_api_key'),
                        update_progress
                    )
                    write_log(f"AI 翻译完成 provider={used_provider}")
                    
                    # 翻译完成后删除进度消息
                    try:
                        await progress_msg.delete()
                    except:
                        pass
                    
                    await app.run_sync(copy_subtitle_to_media_dir, output_subtitle_path, media_path)
                    write_log(f"字幕文件已复制到媒体目录")
                    
                    end_time = time.time()
                    elapsed_time = end_time - start_time
                    minutes, seconds = divmod(elapsed_time, 60)
                    time_str = f"{int(minutes)}分{int(seconds)}秒"

                    if task_id:
                        mark_success(task_id, {'provider': used_provider, 'item_name': item_name, 'subtitle_title': subtitle_title, 'elapsed': time_str})
                        record_event('translation_task_success', task_id=task_id, user_id=user_id, provider=used_provider)
                    if item_id:
                        emby_host = config['emby_host']
                        emby_api = config['emby_api']
                        # 使用计划任务刷新字幕，避免全量刷新带来的性能损耗
                        # 任务ID通常是扫描媒体库或刷新字幕的任务
                        scan_task_id = config.get('StrmAssistant_ScanSubtitle', '59c03072d51cbeb1f623b328f82c900a')
                        refresh_url = f"{emby_host}/emby/ScheduledTasks/Running/{scan_task_id}"
                        refresh_headers = {"X-Emby-Token": emby_api}
                        write_log(f"正在触发 Emby 媒体库扫描任务 (ID: {scan_task_id})...")
                        refresh_response = post(refresh_url, headers=refresh_headers, timeout=15)
                        
                        if refresh_response.status_code == 204:
                            await app.send_message(chat_id, f"原字幕: {subtitle_title}\n目标语言: 中文\n已触发媒体库扫描，稍后即可看到字幕")
                            # 发送翻译好的字幕文件给用户
                            try:
                                await app.send_document(
                                    chat_id, 
                                    document=output_subtitle_path, 
                                    caption=f"翻译完成的字幕文件: {item_name}"
                                )
                            except Exception as e:
                                write_log(f"发送字幕文件失败: {str(e)}", level="ERROR")
                                await app.send_message(chat_id, "字幕文件发送失败，请联系管理员")

                            log_msg = (
                                f"[{user_name}](tg://user?id={user_id}) 提交的任务已完成\n"
                                f"影片名称: {item_name}\n"
                                f"原字幕: {subtitle_title}\n"
                                f"目标语言: 简体中文\n"
                                f"总耗时: {time_str}\n"
                                f"Base on {used_provider} Translate"
                            )
                            write_log(log_msg)
                            if group:
                                await app.send_message(group, f"恭喜🎉 {log_msg}")
                        else:
                            error_msg = f"触发媒体库扫描失败，状态码: {refresh_response.status_code}"
                            write_log(error_msg, level="ERROR")
                            await app.send_message(chat_id, error_msg)
                    await app.run_sync(cleanup_temp_files, input_subtitle_path)
                    await app.run_sync(cleanup_temp_files, output_subtitle_path)
            except Exception as e:
                error_msg = f"翻译任务失败: {str(e)}"
                if task_id:
                    mark_failed(task_id, error_msg)
                task_record = get_task(task_id) if task_id else None
                payload = (task_record or {}).get('payload') or {}
                refund_cost = int(payload.get('cost') or task_data.get('cost') or 0)
                if refund_cost > 0 and not payload.get('cost_refunded'):
                    try:
                        update_user_info(user_id, refund_cost)
                        if task_id:
                            update_task(task_id, payload={**payload, 'cost_refunded': True})
                        await app.send_message(chat_id, f"翻译失败，已自动退回 {refund_cost}{coinsname}")
                        write_log(f"翻译失败已自动退款 user_id={user_id} cost={refund_cost} task_id={task_id}", level='WARNING')
                    except Exception as refund_error:
                        write_log(f"翻译失败退款异常 user_id={user_id} task_id={task_id}: {refund_error}", level='ERROR')
                record_event('translation_task_failed', task_id=task_id, user_id=user_id, error=str(e))
                write_log(error_msg, level="ERROR")
                await app.send_message(chat_id, f"翻译过程中发生错误: {str(e)}")
            finally:
                active_translation_tasks.discard(task_identifier)
            translation_queue.task_done()
            remaining = translation_queue.qsize()
            if remaining > 0:
                await app.send_message(chat_id, f"您的翻译任务已完成，队列中还有 {remaining} 个任务等待处理")
        except Exception as e:
            write_log(f"翻译队列处理循环发生严重错误: {str(e)}", level="ERROR")
            await asyncio.sleep(5)
    is_translation_worker_running = False


async def translation_retry_watcher():
    while True:
        try:
            retry_tasks = claim_retry_tasks('translation')
            for task in retry_tasks:
                payload = task.get('payload') or {}
                task_identifier = f"{payload.get('media_path')}_{payload.get('subtitle_index')}"
                if task_identifier in active_translation_tasks:
                    continue
                payload['task_id'] = task.get('id')
                active_translation_tasks.add(task_identifier)
                await translation_queue.put(payload)
                record_event('translation_task_requeued', task_id=task.get('id'))
            merge_state({'translation': {'queue_size': translation_queue.qsize(), 'active_tasks': len(active_translation_tasks), 'provider': str(config.get('ai_provider', 'gemini'))}})
        except Exception as e:
            write_log(f"翻译重试观察器异常: {e}", level="ERROR")
        await asyncio.sleep(10)


@app.on_callback_query(filters.regex("function_menu"))
async def functionMenu(client, callback, *_):
    await callback.message.reply("看看下面有什么要选的？👇", reply_markup=function_menu)

@app.on_callback_query(filters.regex("ai_translate"))
async def ai_translate(client, callback, *_):
    if not config.get("translation_enabled", True):
        await callback.answer("管理员未启用 AI 翻译功能", show_alert=True)
        return
    userinfo = read_user_info(callback.from_user.id)
    if userinfo is None or userinfo[1] == 'd':
        await callback.answer("您没有有效的账号，请先注册账号！", show_alert=True)
        return
    if userinfo[1] == "a":
        cost = 0
        await client.send_message(
            chat_id=callback.from_user.id,
            text=f"您为白名单，本次翻译任务已免费，请在60s内使用用户名为 {userinfo[3]} 的账号播放一下您想要被翻译的资源。"
        )
    else:
        cost = 5
        await client.send_message(
            chat_id=callback.from_user.id,
            text=f"本次翻译任务消费5{coinsname}，请在60s内使用用户名为 {userinfo[3]} 的账号播放一下您想要被翻译的资源。"
        )
    try:
        emby_host = config['emby_host']
        emby_api = config['emby_api']
        sessions_url = f"{emby_host}/emby/Sessions?IsPlaying=true"
        sessions_headers = {"X-Emby-Token": emby_api}
        emby_id = userinfo[2]
        subtitle_streams = []
        media_path = None
        playing_item = None
        for i in range(30):
            sessions_response = get(sessions_url, headers=sessions_headers, timeout=10)
            if sessions_response.status_code != 200:
                await client.send_message(callback.from_user.id, "无法连接Emby服务器,请稍后再试")
                return
            sessions_data = sessions_response.json()
            for session in sessions_data:
                if session.get('UserId') == emby_id and session.get('NowPlayingItem'):
                    playing_item = session['NowPlayingItem']
                    media_path = playing_item.get('Path')
                    if 'MediaStreams' in playing_item:
                        for stream in playing_item['MediaStreams']:
                            if stream.get('Type') == 'Subtitle' and stream.get('IsTextSubtitleStream', False):
                                subtitle_streams.append(stream)
                    break
            if playing_item:
                break
            await asyncio.sleep(1)
        if not playing_item or not media_path:
            await client.send_message(callback.from_user.id, "未检测到您有播放的影片，翻译任务已退出")
            return
        item_name = playing_item.get('Name', '未知内容')
        
        # 支持的视频格式列表
        supported_extensions = ('.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.mts', '.m2ts')
        if not media_path.lower().endswith(supported_extensions):
            await client.send_message(callback.from_user.id, f"该媒体格式暂不支持字幕提取翻译，目前支持: {', '.join(supported_extensions)}")
            return
        if not subtitle_streams:
            await client.send_message(callback.from_user.id, "当前视频无任何语言字幕，请自行上传字幕")
            return
        subtitle_buttons = []
        for stream in subtitle_streams:
            title = stream.get('DisplayTitle', f'字幕 {stream.get("Index")}')
            index = stream.get('Index')
            subtitle_buttons.append([InlineKeyboardButton(title, callback_data=f"subtitle_translate_{index}")])
        select_message = await client.send_message(
            callback.from_user.id,
            f"正在播放: {item_name}\n请选择要翻译的字幕,尽量为该影片的母语字幕翻译更准确\n翻译前先播放你想要翻译的字幕看看能否正常显示",
            reply_markup=InlineKeyboardMarkup(subtitle_buttons)
        )
        try:
            callback_query = await app.wait_for_callback_query(callback.from_user.id, filters=filters.regex("^subtitle_translate_"), timeout=60)
            if callback_query and callback_query.data:
                selected_subtitle_index = int(callback_query.data.split("_")[2])
                selected_subtitle = next((s for s in subtitle_streams if s.get('Index') == selected_subtitle_index), None)
                if not selected_subtitle:
                    await client.send_message(callback.from_user.id, "无法获取所选字幕信息，请重试")
                    return
                subtitle_title = selected_subtitle.get('DisplayTitle', '未知字幕')
                task_identifier = f"{media_path}_{selected_subtitle_index}"
                if task_identifier in active_translation_tasks:
                    await client.send_message(callback.from_user.id, "该任务已在队列中，请等待完成")
                    return
                task_record = create_task(
                    'translation',
                    f'{item_name} - {subtitle_title}',
                    {
                        'chat_id': callback.from_user.id,
                        'user_id': callback.from_user.id,
                        'user_name': callback.from_user.first_name,
                        'media_path': media_path,
                        'item_name': item_name,
                        'subtitle_index': selected_subtitle_index,
                        'subtitle_title': subtitle_title,
                        'item_id': playing_item.get('Id'),
                        'cost': cost,
                        'cost_refunded': False,
                    },
                    created_by=callback.from_user.id,
                )
                if cost > 0:
                    update_user_info(callback.from_user.id, -cost)
                write_log(f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id}) 提交的任务已加入队列\n影片名称: {item_name}\n原字幕: {subtitle_title}\n目标语言: 简体中文\nBase on {gemini_model} Translate")
                queue_size = translation_queue.qsize()
                task_data = {
                    'chat_id': callback.from_user.id,
                    'user_id': callback.from_user.id,
                    'user_name': callback.from_user.first_name,
                    'media_path': media_path,
                    'item_name': item_name,
                    'subtitle_index': selected_subtitle_index,
                    'subtitle_title': subtitle_title,
                    'item_id': playing_item.get('Id'),
                    'cost': cost,
                    'task_id': task_record['id']
                }
                active_translation_tasks.add(task_identifier)
                await translation_queue.put(task_data)
                global is_translation_worker_running
                if not is_translation_worker_running:
                    asyncio.create_task(translation_worker())
                merge_state({'translation': {'queue_size': translation_queue.qsize()+1, 'active_tasks': len(active_translation_tasks), 'provider': str(config.get('ai_provider', 'gemini'))}})
                record_event('translation_task_queued', task_id=task_record['id'], user_id=callback.from_user.id)
                await client.send_message(callback.from_user.id, f"您的翻译任务已加入队列，任务ID: {task_record['id']}，当前队列中有 {queue_size} 个任务等待")
        except asyncio.TimeoutError:
            await client.send_message(callback.from_user.id, "选择超时，已取消翻译操作")
            return
    except Exception as e:
        await client.send_message(callback.from_user.id, f"发生错误: {str(e)}")

@app.on_callback_query(filters.regex("upload_subtitle"))
async def upload_subtitle(client, callback, *_):
    userinfo = read_user_info(callback.from_user.id)
    if userinfo is None or userinfo[1] == 'd':
        await client.send_message(
            chat_id=callback.from_user.id,
            text=f"✨我是{name}下片机器人\n🍉你好鸭 [{callback.from_user.first_name}](tg://user?id={callback.from_user.id}) \n🥺您没有LyrebirdEmby的有效账号哟,请先注册。"
        )
        return
    try:
        emby_host = config.get('emby_host')
        emby_api = config.get('emby_api')
        if not emby_host or not emby_api:
            await client.send_message(callback.from_user.id, "Emby配置不完整，无法检测播放状态")
            return
        sessions_url = f"{emby_host}/emby/Sessions?IsPlaying=true"
        sessions_headers = {"X-Emby-Token": emby_api}
        emby_id = userinfo[2]
        media_path = None
        playing_item = None
        await client.send_message(callback.from_user.id, "请在60s内播放一下您想要上传字幕的视频")
        # 检测用户是否正在播放内容，最多等60秒
        for i in range(60):
            sessions_response = get(sessions_url, headers=sessions_headers, timeout=10)
            if sessions_response.status_code != 200:
                await client.send_message(callback.from_user.id, "无法连接Emby服务器,请稍后再试")
                return
            sessions_data = sessions_response.json()
            for session in sessions_data:
                if session.get('UserId') == emby_id and session.get('NowPlayingItem'):
                    playing_item = session['NowPlayingItem']
                    media_path = playing_item.get('Path')
                    break
            if playing_item and media_path:
                break
            await asyncio.sleep(1)
        if not playing_item or not media_path:
            await client.send_message(callback.from_user.id, "未检测到您有播放的影片，请先在Emby端播放目标视频后再点击本按钮")
            return
        file_name = os.path.basename(media_path)
        prefix = os.path.splitext(file_name)[0]
        media_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m4v']
        file_ext = os.path.splitext(media_path)[1].lower()
        if file_ext not in media_extensions:
            await client.send_message(callback.from_user.id, f"❌ 当前播放的文件不是支持的媒体文件格式（支持格式：{', '.join(media_extensions)}）")
            return
            
        item_name = playing_item.get('Name', file_name)
        item_id = playing_item.get('Id')
        poster_url = f"{emby_host}/emby/Items/{item_id}/Images/Primary"
        caption = f"✅ 检测到您正在播放: {item_name}"
        
        try:
            await client.send_photo(callback.from_user.id, photo=poster_url, caption=caption)
        except Exception:
            await client.send_message(callback.from_user.id, caption)
            
        # 让用户上传字幕文件
        subtitle = await client.ask(callback.from_user.id, "请上传字幕文件（支持.srt/.ass，最大5MB）\n输入 /cancel 取消本次操作", timeout=120)
        if getattr(subtitle, 'text', '') == "/cancel":
            await client.send_message(callback.from_user.id, "操作已取消")
            return
        if not hasattr(subtitle, 'document') or not subtitle.document:
            await client.send_message(callback.from_user.id, "❌ 请上传字幕文件")
            return
        if subtitle.document.file_size > 5 * 1024 * 1024:
            await client.send_message(callback.from_user.id, "❌ 字幕文件过大（最大支持5MB）")
            return
        file_ext = subtitle.document.file_name.split('.')[-1].lower()
        if file_ext not in ['srt', 'ass']:
            await client.send_message(callback.from_user.id, f"❌ 无法识别 {file_ext} 格式的字幕文件")
            return
        firstname = callback.from_user.first_name
        subtitle_ext = file_ext
        media_dir = os.path.dirname(media_path)
        zimu_name = prefix + '.由' + firstname + '上传.' + subtitle_ext
        target_path = os.path.join(media_dir, zimu_name)
        await subtitle.download(file_name=target_path)
        write_log(f"[{firstname}](tg://user?id={callback.from_user.id}) 上传字幕: {zimu_name}")
        await client.send_message(callback.from_user.id, f"✅ 字幕文件上传成功\n`{subtitle.document.file_name}`")
        # 刷新Emby媒体库
        try:
            # 使用计划任务刷新字幕，避免全量刷新带来的性能损耗
            # 任务ID通常是扫描媒体库或刷新字幕的任务
            scan_task_id = config.get('StrmAssistant_ScanSubtitle', '59c03072d51cbeb1f623b328f82c900a')
            refresh_url = f"{emby_host}/emby/ScheduledTasks/Running/{scan_task_id}"
            refresh_headers = {"X-Emby-Token": emby_api}
            write_log(f"正在触发 Emby 媒体库扫描任务 (ID: {scan_task_id})...")
            response = post(refresh_url, headers=refresh_headers, timeout=15)
            
            if response.status_code == 204:
                await client.send_message(callback.from_user.id, "✅ 已触发媒体库扫描，稍后即可看到字幕")
            else:
                await client.send_message(callback.from_user.id, f"⚠️ 触发媒体库扫描失败，状态码: {response.status_code}")
        except Exception as e:
            await client.send_message(callback.from_user.id, f"⚠️ 刷新Emby媒体库时出错: {str(e)}")
        # 群组通知
        if group:
            await client.send_message(
                chat_id=group,
                text=f"🎉 用户 [{firstname}](tg://user?id={callback.from_user.id}) 上传了字幕文件\n📁 名称: {subtitle.document.file_name}"
            )
    except ListenerTimeout:
        await client.send_message(callback.from_user.id, "操作超时")
        return
    
    
