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
# user_id -> bool
user_verified = {}   # <--- 注意这里仍然声明为全局变量

if PERSIST_FILE.exists():
    try:
        data = json.loads(PERSIST_FILE.read_text(encoding="utf-8"))
        user_to_thread = {int(k): int(v) for k, v in data.get("user_to_thread", {}).items()}
        thread_to_user = {int(k): int(v) for k, v in data.get("thread_to_user", {}).items()}
        # 【新增】读取验证状态
        user_verified = {int(k): v for k, v in data.get("user_verified", {}).items()}
    except Exception:
        # 异常处理：如果文件损坏，清空所有数据
        user_to_thread = {}
        thread_to_user = {}
        user_verified = {} # 【新增】清空验证状态

def persist_mapping():
    data = {
        "user_to_thread": {str(k): v for k, v in user_to_thread.items()},
        "thread_to_user": {str(k): v for k, v in thread_to_user.items()},
        # 【新增】保存验证状态 (由于值是 bool，无需转换)
        "user_verified": {str(k): v for k, v in user_verified.items()}, 
    }
    PERSIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- 验证状态（内存） ----------
# ⚠️ 注意：原来的 user_verified = {} 这一行现在要删除或注释掉，
# 因为它已经在上面从文件中加载了。
# 如果不删除，它会覆盖掉从文件加载的数据。
# ------------------------------------

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

# 引入需要的工具
from telegram.helpers import mention_html, escape_html

async def _ensure_thread_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, display: str):
    # 如果内存里有，说明不是新的
    if user_id in user_to_thread:
        return user_to_thread[user_id], False 
    
    # 尝试创建 topic
    try:
        thread_id = await _create_topic_for_user(context.bot, user_id, f"user_{user_id}_{display}")
    except Exception as e:
        # 如果创建失败（比如话题数满了），可能需要清理旧话题或报错
        raise e

    user_to_thread[user_id] = thread_id
    thread_to_user[thread_id] = user_id
    persist_mapping()
    
    # 返回 (id, True) 表示这是新创建的
    return thread_id, True

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
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    text = update.message.text or ""
    # 获取用户对象，用来生成链接
    user = update.effective_user
    
    # 获取显示名，并进行HTML转义防止报错
    user_full_name = escape_html(user.full_name or user.username or str(uid))
    display = _display_name_from_update(update)

    # 1. 验证流程 (保持不变)
    # 注意：这里需要从持久化数据中读取，或者确保 user_verified 已经加载
    # 假设你在 main 里面已经处理好了持久化加载
    if not user_verified.get(uid):
        if text.strip() == VERIFY_ANSWER:
            user_verified[uid] = True
            persist_mapping() # 记得保存验证状态
            await update.message.reply_text("验证成功！你现在可以发送消息了。")
        else:
            await update.message.reply_text("请先通过验证：" + VERIFY_QUESTION)
        return

    # 2. 获取话题 ID 和 新旧状态
    try:
        # 注意：这里接收两个返回值
        thread_id, is_new_topic = await _ensure_thread_for_user(context, uid, display)
    except Exception as e:
        await update.message.reply_text(f"无法建立连接：{e}")
        return

    # 3. 如果是【新话题】，先发送一张“用户资料卡”
    if is_new_topic:
        # 生成可点击的名字链接 <a href="tg://user?id=123">名字</a>
        mention_link = mention_html(uid, user_full_name)
        
        # 构造你想要的资料格式 (参考图二/图四)
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
                parse_mode="HTML" # 必须开启 HTML 模式链接才生效
            )
        except Exception as e:
            # 资料卡发送失败不影响后续消息
            print(f"发送资料卡失败: {e}")

    # 4. 转发用户的实际消息（纯净版）
    # 不加任何前缀，直接发 text
    try:
        await context.bot.send_message(
            chat_id=GROUP_ID, 
            message_thread_id=thread_id, 
            text=text
        )
    except Exception as e:
        await update.message.reply_text(f"发送失败：{e}")
        return

    await update.message.reply_text("已发送。")

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
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

    # --- 修改重点开始 ---
    
    # 获取管理员回复的文本
    text = msg.text or ""
    
    # 如果管理员发的是纯图片/表情包（没有文字），text 会是空的，直接跳过，防止报错
    if not text:
        return

    # 【这里改了】：直接把 text 发给用户，不要加任何前缀
    to_user_text = text 
    
    # --- 修改重点结束 ---

    try:
        await context.bot.send_message(chat_id=target_user, text=to_user_text)
    except Exception:
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
