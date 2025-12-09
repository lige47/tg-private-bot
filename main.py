import os
import json
import asyncio
from pathlib import Path
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- 配置（必填环境变量） ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))           # 目标群（已开启论坛/Topics）的 chat_id
VERIFY_QUESTION = os.getenv("VERIFY_QUESTION", "请输入访问密码：")
VERIFY_ANSWER = os.getenv("VERIFY_ANSWER", "123456")
# 强制把文件路径指向我们刚刚挂载的 /data 目录
PERSIST_FILE = Path("/data/topic_mapping.json")

if not BOT_TOKEN:
    raise RuntimeError("请设置 BOT_TOKEN 环境变量")
if GROUP_ID == 0:
    raise RuntimeError("请设置 GROUP_ID 环境变量（论坛群的 chat_id）")

# ---------- 内存映射（并尝试从文件恢复） ----------
# user_id -> message_thread_id
user_to_thread = {}
# message_thread_id -> user_id
thread_to_user = {}

if PERSIST_FILE.exists():
    try:
        data = json.loads(PERSIST_FILE.read_text(encoding="utf-8"))
        user_to_thread = {int(k): int(v) for k, v in data.get("user_to_thread", {}).items()}
        thread_to_user = {int(k): int(v) for k, v in data.get("thread_to_user", {}).items()}
    except Exception:
        user_to_thread = {}
        thread_to_user = {}

def persist_mapping():
    data = {
        "user_to_thread": {str(k): v for k, v in user_to_thread.items()},
        "thread_to_user": {str(k): v for k, v in thread_to_user.items()},
    }
    PERSIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- 验证状态（内存） ----------
user_verified = {}   # user_id -> bool

# ---------- 帮助函数 ----------
async def _create_topic_for_user(bot, user_id: int, title: str) -> int:
    """
    在论坛群中为 user 创建一个 topic（forum topic），并返回 message_thread_id。
    依赖 Bot API 的 createForumTopic 方法（python-telegram-bot 在较新版本支持）。
    如果创建失败会抛出异常。
    """
    # 名称长度有限制，所以尽量短
    safe_title = title[:40]
    # create_forum_topic 返回一个 ForumTopic 对象，包含 message_thread_id
    resp = await bot.create_forum_topic(chat_id=GROUP_ID, name=safe_title)
    # resp.message_thread_id 应该存在
    thread_id = getattr(resp, "message_thread_id", None)
    if thread_id is None:
        # 兼容：有些版本可能返回 dict
        thread_id = resp.get("message_thread_id") if isinstance(resp, dict) else None
    if thread_id is None:
        raise RuntimeError("创建 topic 未返回 message_thread_id")
    return int(thread_id)

async def _ensure_thread_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, display: str) -> int:
    if user_id in user_to_thread:
        return user_to_thread[user_id]
    # 创建 topic（如果群已满或权限问题会抛错）
    thread_id = await _create_topic_for_user(context.bot, user_id, f"user_{user_id}_{display}")
    user_to_thread[user_id] = thread_id
    thread_to_user[thread_id] = user_id
    persist_mapping()
    return thread_id

def _display_name_from_update(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "匿名"
    name = u.full_name or u.username or str(u.id)
    return name.replace("\n", " ")

# ---------- 处理器 ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        return
    if user_verified.get(uid):
        await update.message.reply_text("你已经验证过了，可以发送消息。")
        return
    await update.message.reply_text(VERIFY_QUESTION)

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    1) 若未验证，判断答案；通过后标记。
    2) 若验证通过，把用户消息转发到群里的对应 topic（自动创建 topic）。
    """
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    text = update.message.text or ""
    display = _display_name_from_update(update)

    # 验证流程
    if not user_verified.get(uid):
        if text.strip() == VERIFY_ANSWER:
            user_verified[uid] = True
            await update.message.reply_text("验证成功！你现在可以发送消息了。")
        else:
            await update.message.reply_text("请先通过验证：" + VERIFY_QUESTION)
        return

    # 已验证：确保 topic，转发消息到该 topic（message_thread_id）
    try:
        thread_id = await _ensure_thread_for_user(context, uid, display)
    except Exception as e:
        await update.message.reply_text(f"创建或获取话题失败：{e}")
        return

    forward_text = f"来自用户 {uid} ({display}) 的私聊消息：\n\n{text}"
    # 如果用户发送的不是纯文本，简单处理转发媒体：这里仅处理文本；可扩展处理图片/文件/语音
    try:
        await context.bot.send_message(chat_id=GROUP_ID, message_thread_id=thread_id, text=forward_text)
    except Exception as e:
        await update.message.reply_text(f"转发到群组话题失败：{e}")
        return

    await update.message.reply_text("消息已发送，管理员会在群组的对应话题中回复你。")

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    群组内消息（在论坛 topic 中）：把管理员在 topic 中的回复转发回对应用户。
    注意：
      - 仅处理位于 GROUP_ID 的消息
      - 需要 message_thread_id（代表 topic）
      - 忽略机器人自己发的消息
    """
    msg = update.message
    if not msg:
        return
    if update.effective_chat.id != GROUP_ID:
        return

    # 要求处在某个 topic（message_thread_id 不为 None）
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return

    # 忽略 bot 自己的消息
    if msg.from_user and msg.from_user.is_bot:
        return

    # 找到对应用户
    target_user = thread_to_user.get(int(thread_id))
    if not target_user:
        # 可能是管理员在新 topic 里手动输入标题，尝试从 topic 名称里解析用户 id（可选）
        # 这里简单忽略未映射的 topic
        return

    # 将群组 topic 中的消息转发或重发给用户（这里使用 send_message，可根据需要改为 forward_message）
    sender = msg.from_user.full_name if msg.from_user else "群内用户"
    text = msg.text or ""
    to_user_text = f"群组（话题 {thread_id}）中由 {sender} 的回复：\n\n{text}"
    try:
        await context.bot.send_message(chat_id=target_user, text=to_user_text)
        # 可选：在群组中确认已发送
        # await context.bot.send_message(chat_id=GROUP_ID, message_thread_id=thread_id, text="已将回复转发给用户。")
    except Exception:
        # 发送失败，可能是用户未与 bot 开始对话或被封禁，给管理员提示
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                text="⚠️ 转发给用户失败（用户可能未启动机器人或已阻止机器人）。"
            )
        except Exception:
            pass

# ---------- 启动 ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 私聊：/start 与私聊文本
    app.add_handler(CommandHandler("start", start))
    
    # 【修改点 1】这里原来的 filters.PRIVATE 改为了 filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))

    # 群组内（Topics）消息处理：包括管理员在 topic 中回复
    # 只过滤 GROUP_ID 的消息（在 handler 内再检查），并要求为群组消息
    # 【修改点 2】建议显式写 filters.Chat(chat_id=GROUP_ID)
    app.add_handler(MessageHandler(filters.Chat(chat_id=GROUP_ID) & filters.TEXT & ~filters.COMMAND, handle_group_message))

    print("Bot is starting polling...")
    # 启动轮询
    app.run_polling()

if __name__ == "__main__":
    main()
