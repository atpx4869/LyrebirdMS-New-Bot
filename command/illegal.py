import json
from init import app
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup,InlineKeyboardButton

@app.on_message(filters.command('start') & ~filters.private)
async def no_public(_, msg):
    await app.send_message(msg.chat.id,
                           "这是一个私聊命令",
                           reply_markup=InlineKeyboardMarkup(
                        [
                        [InlineKeyboardButton(
                            "确认",
                            callback_data=f"delete_this_msg"
                        )]
                        ]
            )
        )
