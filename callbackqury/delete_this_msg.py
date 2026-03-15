import json
from init import app
from pyrogram import filters
from pyrogram.errors import MessageDeleteForbidden,QueryIdInvalid
from logger.logger import write_log

@app.on_callback_query(filters.regex("delete_this_msg"))
async def delete_this_msg(client, callback, *_):
    try:
        # 删除消息
        msg = callback.message
        if msg:
            try:
                await msg.delete()
            except MessageDeleteForbidden:
                pass  # 忽略删除权限错误，可能消息不是机器人发送的
            except Exception as e:
                write_log(f"删除消息错误: {e}")
                
        # 回答回调查询
        try:
            await callback.answer(text="Done✅", show_alert=False)
        except QueryIdInvalid:
            # 查询ID可能已过期，静默失败
            pass
        except Exception as e:
            write_log(f"回答回调查询错误: {e}")
    except Exception as e:
        write_log(f"delete_this_msg 函数出错: {e}")