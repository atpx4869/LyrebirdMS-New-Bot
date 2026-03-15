from init import app
from pyrogram import filters
from sql.embybot import read_user_info
from menu.startMenu import none_account_menu, normal_user_menu
from logger.logger import write_log
from app_config import config
from runtime_state import bump_counter, record_event

name = config['name']
coinsname = config['coinsname']
bgimg = 'bgimg.jpg'
accountbot = config['accountbot']


def _welcome_caption(msg, userinfo):
    first_name = msg.from_user.first_name or '朋友'
    mention = f'[{first_name}](tg://user?id={msg.from_user.id})'
    if userinfo is None or userinfo[1] == 'd':
        return (
            f'✨ **欢迎使用 {name} 下载机器人**\n\n'
            f'👋 你好，{mention}\n'
            '当前没有检测到可用账号，暂时无法下片。\n\n'
            '你可以先点击下方按钮查看账号说明，开通后再回来使用。'
        )

    white = '⭐️ 白名单用户\n' if userinfo[1] == 'a' else ''
    return (
        f'✨ **欢迎使用 {name} 下载机器人**\n\n'
        f'👋 你好，{mention}\n'
        f'{white}'
        f'💰 当前余额：**{userinfo[0]} {coinsname}**\n'
        f'🎁 免费额度：**{userinfo[4]} {coinsname}**\n\n'
        '你可以直接点按钮开始搜索，也可以发送 `/rate` 查看当前下载进度。\n\n'
        '常用命令：`/help` 查看说明，`/status` 查看服务状态。'
    )


@app.on_message(filters.command('start') & filters.private)
async def start(_, msg):
    bump_counter('start_commands')
    record_event('user_start', user_id=msg.from_user.id)
    write_log(f'用户 {msg.from_user.id} ({msg.from_user.first_name}) 触发 /start 命令')
    userinfo = read_user_info(msg.from_user.id)
    if userinfo is None or userinfo[1] == 'd':
        write_log(f'用户 {msg.from_user.id} 无有效账号')
        await app.send_photo(
            chat_id=msg.chat.id,
            photo=bgimg,
            caption=_welcome_caption(msg, userinfo),
            reply_markup=none_account_menu,
        )
        return

    write_log(f'用户 {msg.from_user.id} 验证通过, 余额: {userinfo[0]}')
    await app.send_photo(
        chat_id=msg.chat.id,
        photo=bgimg,
        caption=_welcome_caption(msg, userinfo),
        reply_markup=normal_user_menu,
    )


@app.on_message(filters.command('help') & filters.private)
async def help_command(_, msg):
    record_event('user_help', user_id=msg.from_user.id)
    help_text = (
        f'**{name} 使用说明**\n\n'
        '1. 点击 **搜索资源⚡️**，输入片名开始搜索。\n'
        '2. 选择媒体后，机器人会列出可下载种子。\n'
        '3. 发送 `/download_种子ID` 开始下载。\n'
        '4. 发送 `/rate` 查看当前下载进度。\n'
        '5. 如启用了字幕功能，可在“更多功能”里使用翻译或上传字幕。\n\n'
        '排障建议：\n'
        '- 如果搜索无结果，可尝试英文名、繁体名或年份。\n'
        '- 如果长时间无响应，先发送 `/status` 看服务连通性。\n- 如果仍异常，请联系管理员检查代理、MovieServer 和数据库状态。'
    )
    await app.send_message(chat_id=msg.chat.id, text=help_text)
