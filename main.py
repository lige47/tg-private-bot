import os
import json
import asyncio
import html  # 修复导入错误：使用Python自带html库
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
from telegram.helpers import mention_html  # 只保留 mention_html

# ---------- 配置（必填环境变量） ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
# 确保 GROUP_ID 是 int，且通常是 -100 开头
GROUP_ID = int(os.getenv("GROUP_ID", "0"))            
VERIFY_QUESTION = os.getenv("VERIFY_QUESTION", "请输入访问密码：")
VERIFY_ANSWER = os.getenv("VERIFY_ANSWER", "123456")

# 【持久化修改】确保路径指向 Zeabur 挂载的 /data 目录
PERSIST_FILE = Path("/data/topic_mapping.json") 
# 如果你在本地测试没有 /data，可以临时改回下面这行：
# PERSIST_FILE = Path("topic_mapping.json")

if not BOT_TOKEN:
    raise RuntimeError("请设置 BOT_TOKEN 环境变量")
if GROUP_ID == 0:
    raise RuntimeError("请设置 GROUP_ID 环境变量（论坛群的 chat_id）")

# ---------- 内存映射（并尝试从文件恢复） ----------
# user_id -> message_thread_id
user_to_thread = {}
# message_thread_id -> user_id
thread_to_user = {}
# user_id -> bool
user_verified = {}

# 启动时加载数据
if PERSIST_FILE.exists():
    try:
        content = PERSIST_FILE.read_text(encoding="utf-8")
        if content.strip():
            data = json.loads(content)
            user_to_thread = {int(k): int(v) for k, v in data.get("user_to_thread", {}).items()}
            thread_to_user = {int(k): int(v) for k, v in data.get("thread_to_user", {}).items()}
            # 加载验证状态，防止重启后需要重新验证
            user_verified = {int(k): v for k, v in data.get("user_verified", {}).items()}
    except Exception as e:
        print(f"读取数据文件失败: {e}")
        user_to_thread = {}
        thread_to_user = {}
        user_verified = {}

def persist_mapping():
    """保存数据到文件"""
    data = {
        "user_to_thread": {str(k): v for k, v in user_to_thread.items()},
        "thread_to_user": {str(k): v for k, v in thread_to_user.items()},
        "user_verified": {str(k): v for k, v in user_verified.items()},
    }
    try:
        # 确保存储目录存在
        if not PERSIST_FILE.parent.exists():
            PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"保存数据失败: {e}")

# ---------- 帮助函数 ----------
async def _create_topic_for_user(bot, user_id: int, title: str) -> int:
    safe_title = title[:40] # 截断标题防止过长
    resp = await bot.create_forum_topic(chat_id=GROUP_ID, name=safe_title)
    thread_id = getattr(resp, "message_thread_id", None)
    if thread_id is None:
        thread_id = resp.get("message_thread_id") if isinstance(resp, dict) else None
    if thread_id is None:
        raise RuntimeError("创建 topic 未返回 message_thread_id")
    return int(thread_id)

async def _ensure_thread_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, display: str):
    """
    返回 (thread_id, is_new_topic)
    is_new_topic: 如果是刚创建的或者是内存里没有记录的，返回 True
    """
    if user_id in user_to_thread:
        return user_to_thread[user_id], False 
    
    # 尝试创建 topic
    try:
        thread_id = await _create_topic_for_user(context.bot, user_id, f"user_{user_id}_{display}")
    except Exception as e:
        raise e

    # 记录到内存和文件
    user_to_thread[user_id] = thread_id
    thread_to_user[thread_id] = user_id
    persist_mapping() # 保存新建的映射
    
    return thread_id, True

def _display_name_from_update(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "匿名"
    name = u.full_name or u.username or str(u.id)
    return name.replace("\n", " ")

# ---------- 处理器 ----------

# 【你缺失的 start 函数】
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        return
    
    # 检查验证状态
    if user_verified.get(uid):
        await update.message.reply_text("你已经验证过了，可以发送消息。")
        return
    await update.message.reply_text(VERIFY_QUESTION)

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    私聊消息处理：
    1. 验证逻辑
    2. 新用户/新话题 -> 发送资料卡
    3. 后续消息 -> 纯净转发
    """
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    text = update.message.text or ""
    user = update.effective_user
    display = _display_name_from_update(update)

    # --- 1. 验证流程 ---
    if not user_verified.get(uid):
        if text.strip() == VERIFY_ANSWER:
            user_verified[uid] = True
            persist_mapping() # 验证成功立即保存
            await update.message.reply_text("验证成功！你现在可以发送消息了。")
        else:
            await update.message.reply_text("请先通过验证：" + VERIFY_QUESTION)
        return

    # --- 2. 获取话题 ID 和 新旧状态 ---
    try:
        thread_id, is_new_topic = await _ensure_thread_for_user(context, uid, display)
    except Exception as e:
        await update.message.reply_text(f"无法建立连接，请稍后再试或联系管理员。({e})")
        return

    # --- 3. 如果是【新话题】，先发送“用户资料卡” ---
    if is_new_topic:
        # 使用 html.escape 替代旧版的 escape_html
        safe_name = html.escape(user.full_name or user.username or str(uid))
        # 生成可点击的名字链接
        mention_link = mention_html(uid, safe_name)
        
        # 构造资料卡 (HTML格式)
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
                parse_mode=ParseMode.HTML # 必须开启 HTML 模式链接才生效
            )
        except Exception as e:
            print(f"发送资料卡失败: {e}")

    # --- 4. 转发用户的实际消息（纯净版）---
    try:
        await context.bot.send_message(
            chat_id=GROUP_ID, 
            message_thread_id=thread_id, 
            text=text
        )
    except Exception as e:
        await update.message.reply_text(f"消息发送失败：{e}")
        return

    await update.message.reply_text("已发送。")

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    群组消息处理：管理员回复用户
    """
    msg = update.message
    if not msg:
        return
    # 确保是目标群组
    if update.effective_chat.id != GROUP_ID:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return

    if msg.from_user and msg.from_user.is_bot:
        return

    target_user = thread_to_user.get(int(thread_id))
    if not target_user:
        return

    # 获取管理员回复的文本
    text = msg.text or ""
    if not text:
        return

    # 【纯净回复】：直接把 text 发给用户，不加前缀
    try:
        await context.bot.send_message(chat_id=target_user, text=text)
    except Exception:
        # 如果发送失败，给管理员一个反馈
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                text="⚠️ 回复失败（用户可能已封禁机器人）。"
            )
        except Exception:
            pass

# ---------- 启动 ----------
def main():
    print("Bot is starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 添加处理器
    # 1. Start 命令
    app.add_handler(CommandHandler("start", start))
    
    # 2. 私聊消息 (过滤器修正为 filters.ChatType.PRIVATE)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, 
        handle_private_message
    ))

    # 3. 群组消息 (过滤器修正为 filters.Chat(chat_id=...))
    app.add_handler(MessageHandler(
        filters.Chat(chat_id=GROUP_ID) & filters.TEXT & ~filters.COMMAND, 
        handle_group_message
    ))

    print("Polling started.")
    app.run_polling()

if __name__ == "__main__":
    main()
