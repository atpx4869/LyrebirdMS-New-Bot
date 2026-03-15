from pathlib import Path

from pyrogram import Client, enums

from app_config import config
from logger.logger import write_log

api_id = config['api_id']
api_hash = config['api_hash']
bot_token = config['bot_token']
proxy_mode = config.get('proxy_mode', False)
session_workdir = Path(config.get('session_workdir', 'runtime/pyrogram'))
session_workdir.mkdir(parents=True, exist_ok=True)

client_kwargs = dict(
    name=str(session_workdir / 'LyrebirdResbot'),
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token,
    workers=50,
    max_concurrent_transmissions=200,
    parse_mode=enums.ParseMode.MARKDOWN,
)

proxy = config.get('proxy') or {}
if proxy_mode and proxy:
    client_kwargs['proxy'] = proxy
    write_log(
        f"已启用 Telegram 代理: {proxy.get('scheme', 'unknown')}://{proxy.get('hostname')}:{proxy.get('port')}",
        level='INFO',
    )
else:
    write_log('Telegram 代理未启用，Pyrogram 将直连', level='WARNING')

app = Client(**client_kwargs)
