from pyrogram import filters

from app_config import config
from healthcheck import run_healthcheck
from init import app
from runtime_state import read_state, record_event

ADMIN_PANEL_URL = config.get('admin_panel_url') or ''


@app.on_message(filters.command('status') & filters.private)
async def status_command_handler(_, msg):
    record_event('user_status', user_id=msg.from_user.id)
    health = run_healthcheck(log_on_success=False)
    state = read_state().get('bot') or {}
    lines = [
        f"**{config.get('name', 'LyrebirdMS Bot')} 状态**",
        f"Bot 状态：{state.get('status', 'unknown')}",
        f"心跳：{state.get('heartbeat', '暂无')}",
        f"代理模式：{'开启' if config.get('proxy_mode') else '关闭'}",
        f"翻译功能：{'开启' if config.get('translation_enabled', True) else '关闭'}",
        '',
        '**服务连通性**',
    ]
    for key, label in [('mysql','MySQL'),('postgres','PostgreSQL'),('redis','Redis'),('movieserver','MovieServer'),('emby','Emby'),('ai','AI')]:
        info = health.get(key) or {}
        ok = info.get('ok', False)
        detail = info.get('message') or info.get('detail') or ''
        lines.append(f"- {label}: {'OK' if ok else '异常'}{(' / ' + detail) if detail else ''}")
    lines += [
        '',
        '排障建议：',
        '- 如果 /start 无响应，先到管理面板看健康检查和最近错误。',
        '- 如果 Telegram 相关异常，优先检查代理和 bot_token。',
        '- 如果搜索或下载异常，优先检查 MovieServer 和数据库。',
    ]
    if ADMIN_PANEL_URL:
        lines += ['', f'管理面板：{ADMIN_PANEL_URL}']
    await app.send_message(msg.chat.id, '\n'.join(lines))
