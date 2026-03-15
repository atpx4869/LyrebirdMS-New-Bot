from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app_config import config

name = config['name']
coinsname = config['coinsname']
accountbot = config['accountbot']

none_account_menu = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton('账号📒', url=accountbot)],
        [InlineKeyboardButton('关闭❌', callback_data='delete_this_msg')],
    ]
)

normal_user_menu = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton('搜索资源⚡️', callback_data='seeds_search'),
            InlineKeyboardButton('下载进度📊', callback_data='searchRate'),
        ],
        [
            InlineKeyboardButton('订阅更新🏄🏻', callback_data='sub_search'),
            InlineKeyboardButton('更多功能🎯', callback_data='function_menu'),
        ],
        [InlineKeyboardButton('关闭❌', callback_data='delete_this_msg')],
    ]
)

function_menu = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton('AI 翻译字幕🤖', callback_data='ai_translate')],
        [InlineKeyboardButton('上传字幕📝', callback_data='upload_subtitle')],
        [InlineKeyboardButton('关闭❌', callback_data='delete_this_msg')],
    ]
)
