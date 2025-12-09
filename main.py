import os
from telegram import Update
from telegram.ext import (
ApplicationBuilder,
CommandHandler,
MessageHandler,
ContextTypes,
filters,
)


VERIFY_QUESTION = os.getenv("VERIFY_QUESTION", "请输入访问密码：")
VERIFY_ANSWER = os.getenv("VERIFY_ANSWER", "123456")


# 记录用户验证状态与当前话题
user_verified = {}
user_topic = {}




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id


if user_verified.get(user_id):
await update.message.reply_text("你已经验证过了，可以继续聊天。")
return


await update.message.reply_text(f"欢迎！\n{VERIFY_QUESTION}")




async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
text = update.message.text.strip()


# --- 未验证用户必须先回答 ---
if not user_verified.get(user_id):
if text == VERIFY_ANSWER:
user_verified[user_id] = True
await update.message.reply_text("验证成功！你现在可以和我聊天了。")
else:
await update.message.reply_text("验证失败，请重新输入密码。")
return


# --- 已验证：处理话题 ---
topic = user_topic.get(user_id, "general")


response = f"你当前的话题是：{topic}\n你说：{text}"
await update.message.reply_text(response)




async def set_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id


if not user_verified.get(user_id):
await update.message.reply_text("请先通过验证再设置话题。")
return


if len(context.args) == 0:
await update.message.reply_text("用法： /topic <话题名>")
return


topic_name = context.args[0]
user_topic[user_id] = topic_name


await update.message.reply_text(f"已切换话题为：{topic_name}")




# ---- Bot 主程序 ----
def main():
token = os.getenv("BOT_TOKEN")
if not token:
raise ValueError("请设置 BOT_TOKEN 环境变量。")


app = ApplicationBuilder().token(token).build()


app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("topic", set_topic))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


app.run_polling()




if __name__ == "__main__":
main()
