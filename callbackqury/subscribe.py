import json
from init import app
from pyrogram import filters

@app.on_callback_query(filters.regex("sub_search"))
async def sub_search(client, callback, *_):
    await callback.answer(text="❕暂时无法订阅，如订阅请在群组请求管理员操作订阅，要求必须为在更新中的剧集", show_alert=True)