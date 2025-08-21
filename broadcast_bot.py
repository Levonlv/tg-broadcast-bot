# bot.py
import os
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Set, Dict, Any
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    User,
    constants,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    PicklePersistence,
)
from telegram.error import BadRequest, Forbidden

# ===============================
# 1. КОНФИГУРАЦИЯ И КОНСТАНТЫ
# ===============================

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("Необходимо установить переменную окружения BOT_TOKEN.")
    exit(1)

raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: Set[int] = {int(x) for x in raw_admin_ids.replace(" ", "").split(",") if x}

DEFAULT_TTL_MIN = int(os.getenv("DEFAULT_TTL_MIN", "15"))
STATE_FILE = os.getenv("STATE_FILE", "data/state.pickle")

# Константы для Callback Data
CALLBACK_PREFIX_CLAIM = "claim"
CALLBACK_PREFIX_UNCLAIM = "unclaim"
CALLBACK_PREFIX_DONE = "done"
CALLBACK_PREFIX_TPL = "tpl"
CALLBACK_PREFIX_MANUAL = "manual"

# ===============================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (UTILS)
# ===============================

def is_admin(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    all_admins = set(context.bot_data.get("admins", [])) | ADMIN_IDS
    return user_id in all_admins

def short_id(bid: str) -> str:
    return bid.split("-")[0]

def human_name(user: User) -> str:
    parts = [p for p in [user.first_name, user.last_name] if p]
    base = " ".join(parts) if parts else (user.username or f"id:{user.id}")
    return f"{base} (@{user.username})" if user.username else base

def build_keyboard(bid: str, state: Dict[str, Any]) -> InlineKeyboardMarkup | None:
    bc = state.get(bid)
    if not bc or bc.get("expired", False):
        return None
    if not bc.get("claimed_by"):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Взять", callback_data=f"{CALLBACK_PREFIX_CLAIM}:{bid}")]]
        )
    if not bc.get("done"):
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("♻️ Снять", callback_data=f"{CALLBACK_PREFIX_UNCLAIM}:{bid}")],
                [InlineKeyboardButton("✔️ Исполнено", callback_data=f"{CALLBACK_PREFIX_DONE}:{bid}")],
            ]
        )
    return None

def render_message_text(bid: str, state: Dict[str, Any]) -> str:
    bc = state[bid]
    if bc.get("expired"):
        status = "🔴 Статус: истёк срок"
    elif bc.get("done"):
        claimer_name = bc["claimed_by"]["name"]
        status = f"🟢 Статус: исполнена — {claimer_name}"
    elif bc.get("claimed_by"):
        claimer_name = bc["claimed_by"]["name"]
        status = f"🟡 Статус: взята — {claimer_name}"
    else:
        status = "🟢 Статус: свободна"
    created_dt = datetime.fromisoformat(bc["created_at"])
    deadline = (created_dt + timedelta(minutes=bc["ttl_min"])).strftime("%Y-%m-%d %H:%M")
    return (
        f"📣 <b>Заявка #{short_id(bid)}</b>\n"
        f"{bc['text']}\n\n"
        f"⏳ Актуально до: <b>{deadline}</b> (≈{bc['ttl_min']} мин)\n"
        f"{status}"
    )

async def update_broadcast_messages(context: ContextTypes.DEFAULT_TYPE, bid: str):
    bot_data = context.bot_data
    broadcast = bot_data.get("broadcasts", {}).get(bid)
    if not broadcast:
        return
    text = render_message_text(bid, bot_data["broadcasts"])
    keyboard = build_keyboard(bid, bot_data["broadcasts"])
    for msg_info in list(broadcast.get("messages", [])):
        try:
            await context.bot.edit_message_text(
                chat_id=msg_info["chat_id"],
                message_id=msg_info["message_id"],
                text=text,
                reply_markup=keyboard,
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as e:
            if "message is not modified" not in e.message.lower():
                logger.warning(f"Error updating message in {msg_info['chat_id']}: {e}")
        except Forbidden:
            logger.info(f"Bot blocked in chat {msg_info['chat_id']}. Removing from list.")
            if msg_info["chat_id"] in bot_data.get("chats", set()):
                bot_data["chats"].remove(msg_info["chat_id"])
            broadcast["messages"].remove(msg_info)
        except Exception as e:
            logger.error(f"Unexpected error updating message in {msg_info['chat_id']}: {e}")

# ===============================
# 3. ОБРАБОТЧИКИ КОМАНД (HANDLERS)
# ===============================

# -- Декоратор для проверки прав админа --
def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not is_admin(update.effective_user.id, context):
            await update.message.reply_text("Эта команда доступна только администраторам.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# -- Общие команды --
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    role = "админ" if is_admin(user_id, context) else "пользователь"
    await update.message.reply_text(
        "Привет! Я бот для широковещательных заявок партнёрам.\n\n"
        "<b>Команды:</b>\n"
        "/register — зарегистрировать текущий чат (админ)\n"
        "/unregister — убрать чат (админ)\n"
        "/list — показать все чаты\n"
        "/broadcast <code>&lt;TTL&gt; &lt;текст&gt;</code> — разослать заявку (админ)\n"
        "/new — создать заявку через конструктор (админ)\n"
        "/help — эта справка\n\n"
        f"Ваш статус: <b>{role}</b>\n"
        f"TTL по умолчанию: {DEFAULT_TTL_MIN} мин",
        parse_mode=constants.ParseMode.HTML,
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Неизвестная команда. Используйте /help для списка команд.")

# -- Команды администрирования --
@admin_only
async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    chats = context.bot_data.setdefault("chats", set())
    if cid not in chats:
        chats.add(cid)
        await update.message.reply_text(f"✅ Чат '{update.effective_chat.title}' зарегистрирован.")
    else:
        await update.message.reply_text("ℹ️ Этот чат уже в списке.")

@admin_only
async def unregister_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    chats = context.bot_data.setdefault("chats", set())
    if cid in chats:
        chats.remove(cid)
        await update.message.reply_text(f"❌ Чат '{update.effective_chat.title}' удалён.")
    else:
        await update.message.reply_text("ℹ️ Этого чата нет в списке.")

async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats = context.bot_data.get("chats", [])
    if not chats:
        await update.message.reply_text("Целевые чаты не зарегистрированы.")
        return
    lines = [f"• <code>{cid}</code>" for cid in chats]
    await update.message.reply_text(
        "<b>Целевые чаты:</b>\n" + "\n".join(lines), parse_mode="HTML"
    )

# -- Логика рассылок --
def parse_broadcast_args(raw: str):
    raw = raw.strip()
    m = re.match(
        r"^\s*(ttl\s*=\s*|\s*)(?P<num>\d{1,3})\s*(m|min|мин)?\s*(?P<rest>.*)$",
        raw,
        flags=re.IGNORECASE,
    )
    ttl = None
    if m and m.group("num") and m.group("rest"):
        try:
            x = int(m.group("num"))
            if 1 <= x <= 180:
                ttl = x
            raw = m.group("rest").strip()
        except (ValueError, TypeError):
            pass
    return (ttl or DEFAULT_TTL_MIN), raw

async def create_and_send_broadcast(
    context: ContextTypes.DEFAULT_TYPE, text: str, ttl_min: int, original_message
):
    bot_data = context.bot_data
    chats = bot_data.get("chats", [])
    if not chats:
        await original_message.reply_text("Нет зарегистрированных чатов. Используйте /register.")
        return
    bid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    broadcasts = bot_data.setdefault("broadcasts", {})
    broadcasts[bid] = {
        "text": text,
        "created_at": created_at,
        "ttl_min": ttl_min,
        "messages": [],
        "claimed_by": None,
        "expired": False,
        "done": False,
    }
    text_to_send = render_message_text(bid, broadcasts)
    keyboard = build_keyboard(bid, broadcasts)
    ok = fail = 0
    for cid in list(chats):
        try:
            msg = await context.bot.send_message(
                chat_id=cid,
                text=text_to_send,
                reply_markup=keyboard,
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            broadcasts[bid]["messages"].append(
                {"chat_id": cid, "message_id": msg.message_id}
            )
            ok += 1
        except Exception as e:
            logger.warning(f"Failed to send to chat {cid}: {e}")
            fail += 1
    context.job_queue.run_once(
        expire_job,
        when=timedelta(minutes=ttl_min),
        data={"bid": bid},
        name=f"expire_{bid}",
    )
    await original_message.reply_text(
        f"✅ Рассылка завершена. Успешно: {ok}, ошибки: {fail}.\n"
        f"Заявка <b>#{short_id(bid)}</b> (TTL {ttl_min} мин).",
        parse_mode="HTML",
    )

async def expire_job(context: ContextTypes.DEFAULT_TYPE):
    bid = context.job.data["bid"]
    broadcasts = context.bot_data.get("broadcasts", {})
    bc = broadcasts.get(bid)
    if not bc or bc.get("expired") or bc.get("done"):
        return
    bc["expired"] = True
    logger.info(f"Broadcast {short_id(bid)} expired.")
    await update_broadcast_messages(context, bid)

@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Формат: /broadcast <TTL> <текст>\nНапример: /broadcast 30 Нужна помощь с задачей X"
        )
        return
    raw_args = " ".join(context.args)
    ttl_min, text = parse_broadcast_args(raw_args)
    if not text:
        await update.message.reply_text("Не указан текст заявки.")
        return
    await create_and_send_broadcast(context, text, ttl_min, update.message)

async def claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, bid = query.data.split(":", 1)
    user = query.from_user
    broadcasts = context.bot_data.get("broadcasts", {})
    bc = broadcasts.get(bid)
    if not bc:
        await query.answer("Заявка не найдена.", show_alert=True)
        return
    if bc.get("expired"):
        await query.answer("Срок этой заявки истёк.", show_alert=True)
        return
    claimer = bc.get("claimed_by")
    if action == CALLBACK_PREFIX_CLAIM:
        if claimer:
            await query.answer("Уже взяли.")
            return
        bc["claimed_by"] = {"id": user.id, "name": human_name(user)}
    elif action == CALLBACK_PREFIX_UNCLAIM:
        if not claimer:
            await query.answer("Уже свободна.")
            return
        if user.id != claimer.get("id") and not is_admin(user.id, context):
            await query.answer("Снять может только исполнитель или админ.", show_alert=True)
            return
        bc["claimed_by"] = None
    elif action == CALLBACK_PREFIX_DONE:
        if not claimer:
            await query.answer("Нельзя исполнить незанятую заявку.", show_alert=True)
            return
        if user.id != claimer.get("id") and not is_admin(user.id, context):
            await query.answer(
                "Отметить исполненной может только исполнитель или админ.", show_alert=True
            )
            return
        bc["done"] = True
    await update_broadcast_messages(context, bid)

# -- Диалог для команды /new --
(SELECTING_TEMPLATE, SELECTING_DIRECTION, SELECTING_BANK, SELECTING_TTL, CONFIRMING) = range(5)

@admin_only
async def new_cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("RUB→USDT | Сбер | 20 мин", callback_data=f"{CALLBACK_PREFIX_TPL}:RUB→USDT|Сбер|20")],
        [InlineKeyboardButton("USDT→RUB | Тинькофф | 60 мин", callback_data=f"{CALLBACK_PREFIX_TPL}:USDT→RUB|Тинькофф|60")],
        [InlineKeyboardButton("Собрать вручную ➡️", callback_data=f"{CALLBACK_PREFIX_MANUAL}:start")],
    ]
    await update.message.reply_text(
        "Выберите шаблон или соберите заявку вручную:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECTING_TEMPLATE

async def handle_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    direction, bank, ttl_str = payload.split("|")
    ttl_min = int(ttl_str)
    text = f"{direction}\nБанк: {bank}\nВремя исполнения: {ttl_min} мин"
    await query.message.delete()
    await create_and_send_broadcast(context, text, ttl_min, query.message)
    return ConversationHandler.END

async def manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("RUB→USDT", callback_data="RUB→USDT")],
        [InlineKeyboardButton("USDT→RUB", callback_data="USDT→RUB")],
    ]
    await query.edit_message_text(
        "Шаг 1: Выберите направление:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_DIRECTION

async def select_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["new_broadcast"] = {"direction": query.data}
    keyboard = [
        [
            InlineKeyboardButton("Сбер", callback_data="Сбер"),
            InlineKeyboardButton("Тинькофф", callback_data="Тинькофф"),
        ],
        [
            InlineKeyboardButton("Альфа", callback_data="Альфа"),
            InlineKeyboardButton("СБП", callback_data="СБП"),
        ],
    ]
    await query.edit_message_text(
        "Шаг 2: Выберите банк:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_BANK

async def select_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["new_broadcast"]["bank"] = query.data
    keyboard = [
        [InlineKeyboardButton("20 минут", callback_data="20")],
        [InlineKeyboardButton("1 час", callback_data="60")],
        [InlineKeyboardButton("В течение дня", callback_data="180")],
    ]
    await query.edit_message_text(
        "Шаг 3: Выберите время исполнения:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_TTL

async def select_ttl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ttl_min = int(query.data)
    data = context.user_data["new_broadcast"]
    data["ttl_min"] = ttl_min
    ttl_text = f"{ttl_min} мин" if ttl_min < 180 else "в течение дня"
    text = f"{data['direction']}\nБанк: {data['bank']}\nВремя исполнения: {ttl_text}"
    data["text"] = text
    keyboard = [
        [InlineKeyboardButton("✅ Отправить", callback_data="send")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await query.edit_message_text(
        f"<b>Подтвердите заявку:</b>\n\n{text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return CONFIRMING

async def confirm_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data.pop("new_broadcast")
    await query.message.delete()
    await create_and_send_broadcast(context, data["text"], data["ttl_min"], query.message)
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ Создание заявки отменено.")
    return ConversationHandler.END

# ===============================
# 4. СБОРКА И ЗАПУСК БОТА
# ===============================

def main():
    persistence = PicklePersistence(filepath=STATE_FILE)
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    app.bot_data.setdefault("chats", set())
    app.bot_data.setdefault("broadcasts", {})
    admins = set(app.bot_data.get("admins", [])) | ADMIN_IDS
    app.bot_data["admins"] = admins

    new_broadcast_handler = ConversationHandler(
        entry_points=[CommandHandler("new", new_cmd_start)],
        states={
            SELECTING_TEMPLATE: [
                CallbackQueryHandler(handle_template, pattern=f"^{CALLBACK_PREFIX_TPL}:"),
                CallbackQueryHandler(manual_start, pattern=f"^{CALLBACK_PREFIX_MANUAL}:start$"),
            ],
            SELECTING_DIRECTION: [CallbackQueryHandler(select_direction)],
            SELECTING_BANK: [CallbackQueryHandler(select_bank)],
            SELECTING_TTL: [CallbackQueryHandler(select_ttl)],
            CONFIRMING: [
                CallbackQueryHandler(confirm_send, pattern="^send$"),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )
    app.add_handler(new_broadcast_handler)

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("register", register_chat))
    app.add_handler(CommandHandler("unregister", unregister_chat))
    app.add_handler(CommandHandler("list", list_chats))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    
    claim_pattern = f"^({CALLBACK_PREFIX_CLAIM}|{CALLBACK_PREFIX_UNCLAIM}|{CALLBACK_PREFIX_DONE}):"
    app.add_handler(CallbackQueryHandler(claim_callback, pattern=claim_pattern))
    
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
