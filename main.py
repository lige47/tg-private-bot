import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ====== 你的大号 Telegram ID ======
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))  # ← 修改为你的真实 Telegram ID

# ====== 验证问题 ======
VERIFY_QUESTION = os.getenv("VERIFY_QUESTION", "请输入访问密码：")
VERIFY_ANSWER = os.getenv("VERIFY_ANSWER", "123456")

# 用户验证状态 & 用户临时上下文
user_verified = {}
waiting_reply_for = {}   # admin 回复时指向哪位用户


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # 管理员启动
    if uid == ADMIN_ID:
        await update.message.reply_text("管理员已连接。所有用户消息将转发给你。")
        return

    # 普通用户：需要先验证
    if not user_verified.get(uid):
        await update.message.reply_text(f"欢迎！\n{VERIFY_QUESTION}")
        return
    
    await update.message.reply_text("你已经验证过了，可以发送消息。")


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # ===== 未验证用户 =====
    if not user_verified.get(uid):
        if text == VERIFY_ANSWER:
            user_verified[uid] = True
            await update.message.reply_text("验证成功！你现在可以发送消息了。")
        else:
            await update.message.reply_text("验证失败，请重新输入密码。")
        return

    # ===== 已验证用户：转发给管理员 =====
    msg = f"来自用户 {uid} 的消息：\n{text}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

    # 保存上下文：管理员若回复，将回复此用户
    waiting_reply_for[ADMIN_ID] = uid


async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # 不是管理员 → 忽略
    if uid != ADMIN_ID:
        return

    # 管理员没有要回复的人
    if uid not in waiting_reply_for:
        await update.message.reply_text("没有需要回复的用户。")
        return

    target_uid = waiting_reply_for[uid]

    # 发送消息回用户
    await context.bot.send_message(chat_id=target_uid, text=f"管理员回复：\n{text}")

    await update.message.reply_text("已回复用户。")


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("请设置 BOT_TOKEN 环境变量！")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))

    # 管理员消息
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), handle_admin_reply))

    # 普通用户消息
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message))

    app.run_polling()


if __name__ == "__main__":
    main()
