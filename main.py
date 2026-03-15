import asyncio

from pyrogram import idle

from app_config import config
from cron_transfer_notice import run_transfer_notice_loop
from init import app
from logger.logger import log_exception, write_log
from runtime_state import bump_counter, record_event, set_bot_status, set_feature_flags
from callbackqury.function_menu import translation_retry_watcher
from command.download import download_retry_watcher

# 导入 handler
from command import *  # noqa: F401,F403
from callbackqury import *  # noqa: F401,F403


async def heartbeat_loop():
    while True:
        set_bot_status('running')
        set_feature_flags(
            {
                'translation_enabled': bool(config.get('translation_enabled', True)),
                'transfer_notice_enabled': bool(config.get('transfer_notice_enabled', True)),
                'tmdb_bg_enabled': bool(config.get('tmdb_bg_enabled', False)),
            }
        )
        await asyncio.sleep(30)


async def main():
    set_bot_status('starting')
    record_event('bot_starting')
    write_log('Bot 正在启动...')
    await app.start()
    set_bot_status('running')
    bump_counter('bot_starts')
    record_event('bot_started')
    write_log('Bot 启动成功，开始监听消息')

    asyncio.create_task(heartbeat_loop())
    if config.get('translation_enabled', True):
        asyncio.create_task(translation_retry_watcher())
        write_log('翻译任务重试观察器已启动')

    asyncio.create_task(download_retry_watcher())
    write_log('下载任务重试观察器已启动')

    if config.get('transfer_notice_enabled', True):
        asyncio.create_task(run_transfer_notice_loop())
        write_log('整理入库通知后台任务已启动')
    else:
        write_log('整理入库通知后台任务已禁用', level='WARNING')

    await idle()
    set_bot_status('stopping')
    record_event('bot_stopping')
    write_log('收到停止信号，正在关闭 Bot...')
    await app.stop()
    set_bot_status('stopped')
    record_event('bot_stopped')


if __name__ == '__main__':
    try:
        app.run(main())
    except Exception:
        set_bot_status('error')
        record_event('bot_crashed')
        log_exception('Bot 启动失败')
        raise
