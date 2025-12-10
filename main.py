import os
import json
import asyncio
import html
from pathlib import Path
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.helpers import mention_html

# ---------- 配置（必填环境变量） ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
VERIFY_QUESTION = os.getenv("VERIFY_QUESTION", "请输入访问密码：")
VERIFY_ANSWER = os.getenv("VERIFY_ANSWER", "123456")

# 持久化文件路径
PERSIST_FILE = Path("/data/topic_mapping.json")

if not BOT_TOKEN:
    raise RuntimeError("请设置 BOT_TOKEN 环境变量")
if GROUP_ID == 0:
    raise RuntimeError("请设置 GROUP_ID 环境变量")

# ---------- 内存数据（从文件恢复） ----------
# user_id -> message_thread_id
user_to_thread = {}
# message_thread_id -> user_id
thread_to_user = {}
# user_id -> bool (是否验证通过)
user_verified = {}
# user_id -> bool (是否被封禁) 【新增】
banned_users = set()

# 启动时加载数据
if PERSIST_FILE.exists():
    try:
        content = PERSIST_FILE.read_text(encoding="utf-8")
        if content.strip():
            data = json.loads(content)
            user_to_thread = {int(k): int(v) for k, v in data.get("user_to_thread", {}).items()}
            thread_to_user = {int(k): int(v) for k, v in data.get("thread_to_user", {}).items()}
            user_verified = {int(k): v for k, v in data.get("user_verified", {}).items()}
            # 加载黑名单，转换为集合
            banned_users = set(data.get("banned_users", []))
    except Exception as e:
        print(f"读取数据文件失败: {e}")
        # 出错时初始化为空，避免程序崩溃
        user_to_thread = {}
        thread_to_user = {}
        user_verified = {}
        banned_users = set()

def persist_mapping():
    """保存数据到文件"""
    data = {
        "user_to_thread": {str(k): v for k, v in user_to_thread.items()},
        "thread_to_user": {str(k): v for k, v in thread_to_user.items()},
        "user_verified": {str(k): v for k, v in user_verified.items()},
        "banned_users": list(banned_users), # 集合转列表才能存JSON
    }
    try:
        if not PERSIST_FILE.parent.exists():
            PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"保存数据失败: {e}")

# ---------- 辅助函数 ----------
async def _create_topic_for_user(bot, user_id: int, title: str) -> int:
    safe_title = title[:40]
    resp = await bot.create_forum_topic(chat_id=GROUP_ID, name=safe_title)
    thread_id = getattr(resp, "message_thread_id", None)
    if thread_id is None:
        thread_id = resp.get("message_thread_id") if isinstance(resp, dict) else None
    if thread_id is None:
        raise RuntimeError("创建 topic 未返回 message_thread_id")
    return int(thread_id)

async def _ensure_thread_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, display: str):
    if user_id in user_to_thread:
        return user_to_thread[user_id], False 
    
    try:
        thread_id = await _create_topic_for_user(context.bot, user_id, f"user_{user_id}_{display}")
    except Exception as e:
        raise e

    user_to_thread[user_id] = thread_id
    thread_to_user[thread_id] = user_id
    persist_mapping()
    return thread_id, True

def _display_name_from_update(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "匿名"
    name = u.full_name or u.username or str(u.id)
    return name.replace("\n", " ")

# ---------- 管理员命令：封禁与解封 ----------

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    用法：
    1. 在群组 Topic 内直接发送 /ban
    2. 发送 /ban 123456789
    """
    # 仅允许在管理群组内操作
    if update.effective_chat.id != GROUP_ID:
        return

    target_uid = None

    # 1. 尝试从参数获取 ID (例如 /ban 123456)
    if context.args and context.args[0].isdigit():
        target_uid = int(context.args[0])
    
    # 2. 如果没参数，尝试从当前 Topic 对应的用户获取
    elif update.effective_message.message_thread_id:
        thread_id = update.effective_message.message_thread_id
        target_uid = thread_to_user.get(thread_id)
    
    if not target_uid:
        await update.message.reply_text("❌ 无法识别目标用户。\n请在用户话题内使用，或指定ID：/ban 123456")
        return

    # 执行封禁
    if target_uid in banned_users:
        await update.message.reply_text(f"用户 {target_uid} 已经在黑名单中了。")
        return

    banned_users.add(target_uid)
    persist_mapping() # 保存
    await update.message.reply_text(f"🚫 用户 {target_uid} 已被封禁。他将无法再发送消息。")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    用法：
    1. 在群组 Topic 内直接发送 /unban
    2. 发送 /unban 123456789
    """
    if update.effective_chat.id != GROUP_ID:
        return

    target_uid = None

    # 1. 尝试从参数获取
    if context.args and context.args[0].isdigit():
        target_uid = int(context.args[0])
    # 2. 尝试从 Topic 获取
    elif update.effective_message.message_thread_id:
        thread_id = update.effective_message.message_thread_id
        target_uid = thread_to_user.get(thread_id)
    
    if not target_uid:
        await update.message.reply_text("❌ 无法识别目标用户。\n请在用户话题内使用，或指定ID：/unban 123456")
        return

    if target_uid not in banned_users:
        await update.message.reply_text(f"用户 {target_uid} 不在黑名单中。")
        return

    banned_users.remove(target_uid)
    persist_mapping()
    await update.message.reply_text(f"✅ 用户 {target_uid} 已解封。")


# ---------- 用户消息处理 ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        return
    
    # 【检查封禁】
    if uid in banned_users:
        # 被封禁用户不给任何回应，或者提示被封禁
        return 

    if user_verified.get(uid):
        await update.message.reply_text("你已经验证过了，可以发送消息。")
        return
    await update.message.reply_text(VERIFY_QUESTION)

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    text = update.message.text or ""
    
    # 【检查封禁】
    if uid in banned_users:
        await update.message.reply_text("🚫 你已被管理员禁止发送消息。")
        return

    user = update.effective_user
    display = _display_name_from_update(update)

    # 验证流程
    if not user_verified.get(uid):
        if text.strip() == VERIFY_ANSWER:
            user_verified[uid] = True
            persist_mapping()
            await update.message.reply_text("验证成功！你现在可以发送消息了。")
        else:
            await update.message.reply_text("请先通过验证：" + VERIFY_QUESTION)
        return

    # 获取/创建话题
    try:
        thread_id, is_new_topic = await _ensure_thread_for_user(context, uid, display)
    except Exception as e:
        await update.message.reply_text(f"系统错误：{e}")
        return

    # 新用户发送资料卡
    if is_new_topic:
        safe_name = html.escape(user.full_name or user.username or str(uid))
        mention_link = mention_html(uid, safe_name)
        info_text = (
            f"<b>新用户接入</b>\n"
            f"ID: <code>{uid}</code>\n"
            f"名字: {mention_link}\n"
            f"#id{uid}" 
        )
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                text=info_text,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            print(f"发送资料卡失败: {e}")

    # 转发消息
    try:
        await context.bot.send_message(chat_id=GROUP_ID, message_thread_id=thread_id, text=text)
    except Exception as e:
        await update.message.reply_text("消息发送失败。")
        return

    await update.message.reply_text("已发送。")

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员回复用户"""
    msg = update.message
    if not msg or update.effective_chat.id != GROUP_ID:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return

    if msg.from_user and msg.from_user.is_bot:
        return

    # 检查是否是命令（防止 /ban 被当做回复发给用户）
    if msg.text and msg.text.startswith("/"):
        return

    target_user = thread_to_user.get(int(thread_id))
    if not target_user:
        return

    text = msg.text or ""
    if not text:
        return

    try:
        await context.bot.send_message(chat_id=target_user, text=text)
    except Exception:
        pass

# ---------- 启动 ----------
def main():
    print("Bot is starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 注册命令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", ban_command))    # 新增
    app.add_handler(CommandHandler("unban", unban_command)) # 新增

    # 消息处理
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, 
        handle_private_message
    ))

    # 群组消息 (注意：这里必须把 COMMAND 过滤掉，否则管理员发 /ban 也会被当成普通回复转发给用户)
    app.add_handler(MessageHandler(
        filters.Chat(chat_id=GROUP_ID) & filters.TEXT & ~filters.COMMAND, 
        handle_group_message
    ))

    print("Polling started.")
    app.run_polling()

if __name__ == "__main__":
    main()
