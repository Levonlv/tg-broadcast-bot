import os, json, re, uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, constants
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes, MessageHandler, filters
)

# ===============================
# ENV
# ===============================

def getenv_int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val and val.strip() else default
    except Exception:
        return default

STATE_FILE = os.getenv("STATE_FILE", "state.json")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x)
DEFAULT_TTL_MIN = getenv_int("DEFAULT_TTL_MIN", 15)

# ===============================
# State helpers
# ===============================

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"admins": list(ADMIN_IDS), "chats": [], "broadcasts": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = {"admins": list(ADMIN_IDS), "chats": [], "broadcasts": {}}
    data["admins"] = list(set(data.get("admins", [])) | ADMIN_IDS)
    data.setdefault("chats", [])
    data.setdefault("broadcasts", {})
    return data

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def is_admin(uid: int, state: Dict[str, Any]) -> bool:
    return uid in set(state.get("admins", []))

def short_id(bid: str) -> str:
    return bid.split("-")[0]

# ===============================
# Rendering
# ===============================

def build_keyboard(bid: str, state: Dict[str, Any]):
    bc = state["broadcasts"].get(bid)
    if not bc or bc.get("expired", False):
        return None
    if not bc.get("claimed_by"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Взять", callback_data=f"claim:{bid}")]
        ])
    if not bc.get("done"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Снять", callback_data=f"unclaim:{bid}")],
            [InlineKeyboardButton("✔️ Исполнено", callback_data=f"done:{bid}")]
        ])
    return None

def human_name(u) -> str:
    parts = [p for p in [u.first_name, u.last_name] if p]
    base = " ".join(parts) if parts else (u.username or f"id:{u.id}")
    return f"{base} (@{u.username})" if u.username else base

def fmt_deadline(created_at_iso: str, ttl_min: int) -> str:
    created_dt = datetime.fromisoformat(created_at_iso)
    return (created_dt + timedelta(minutes=ttl_min)).strftime("%Y-%m-%d %H:%M")

def render_message(bid: str, state: Dict[str, Any]) -> str:
    bc = state["broadcasts"][bid]
    if bc.get("expired"):
        status = "🔴 Статус: истёк срок"
    elif bc.get("done"):
        status = f"🟢 Статус: исполнена — {bc['claimed_by']['name']}"
    elif bc.get("claimed_by"):
        status = f"🟡 Статус: взята — {bc['claimed_by']['name']}"
    else:
        status = "🟢 Статус: свободна"
    deadline = fmt_deadline(bc["created_at"], bc["ttl_min"])
    return (
        f"📣 <b>Заявка #{short_id(bid)}</b>\n"
        f"{bc['text']}\n\n"
        f"⏳ Актуально до: <b>{deadline}</b> (≈{bc['ttl_min']} мин)\n"
        f"{status}"
    )

def parse_broadcast_args(raw: str):
    raw = raw.strip()
    m = re.match(
        r"^\s*(ttl\s*=\s*|\s*)(?P<num>\d{1,3})\s*(m|min|мин)?\s*(?P<rest>.*)$",
        raw, flags=re.IGNORECASE
    )
    ttl = None
    if m and m.group("num") and m.group("rest"):
        try:
            x = int(m.group("num"))
            if 1 <= x <= 180:
                ttl = x
            raw = m.group("rest").strip()
        except Exception:
            pass
    return (ttl or DEFAULT_TTL_MIN), raw

# ===============================
# Commands
# ===============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    role = "админ" if is_admin(update.effective_user.id, state) else "пользователь"
    await update.message.reply_text(
        "Привет! Я бот для широковещательных заявок партнёрам.\n\n"
        "Команды:\n"
        "/register — зарегистрировать текущий чат\n"
        "/unregister — убрать чат\n"
        "/list — показать все чаты\n"
        "/broadcast <TTL> <текст> — разослать заявку (только админы)\n"
        "/new — создать заявку кнопками (только админы)\n"
        "/help — справка\n\n"
        f"Ваш статус: {role}\n"
        f"TTL по умолчанию: {DEFAULT_TTL_MIN} мин",
        parse_mode=constants.ParseMode.HTML
    )

help_cmd = start

async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_admin(update.effective_user.id, state):
        await update.message.reply_text("Только админы могут регистрировать чаты.")
        return
    cid = update.effective_chat.id
    if cid not in state["chats"]:
        state["chats"].append(cid)
        save_state(state)
        await update.message.reply_text(f"Чат зарегистрирован: {update.effective_chat.title or cid}")
    else:
        await update.message.reply_text("Чат уже в списке.")

async def unregister_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_admin(update.effective_user.id, state):
        await update.message.reply_text("Только админы могут убирать чаты.")
        return
    cid = update.effective_chat.id
    if cid in state["chats"]:
        state["chats"].remove(cid)
        save_state(state)
        await update.message.reply_text(f"Чат удалён: {update.effective_chat.title or cid}")
    else:
        await update.message.reply_text("Этого чата нет в списке.")

async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    lines = [f"• {cid}" for cid in state["chats"]] or ["(пусто)"]
    await update.message.reply_text("Целевые чаты:\n" + "\n".join(lines))

# ===============================
# Broadcast
# ===============================

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_admin(update.effective_user.id, state):
        await update.message.reply_text("Только админы могут рассылать заявки.")
        return
    if not state["chats"]:
        await update.message.reply_text("Нет зарегистрированных чатов. /register")
        return
    raw = re.sub(r"^/broadcast(@\w+)?\s*", "", update.message.text or "", flags=re.IGNORECASE)
    ttl_min, text = parse_broadcast_args(raw)
    if not text:
        await update.message.reply_text("Формат: /broadcast <TTL> <текст>")
        return
    bid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    state["broadcasts"][bid] = {
        "text": text, "created_at": created_at,
        "ttl_min": ttl_min, "messages": [],
        "claimed_by": None, "expired": False, "done": False
    }
    save_state(state)
    ok = fail = 0
    for cid in list(state["chats"]):
        try:
            msg = await context.bot.send_message(
                chat_id=cid, text=render_message(bid, state),
                reply_markup=build_keyboard(bid, state),
                parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True
            )
            state["broadcasts"][bid]["messages"].append(
                {"chat_id": cid, "message_id": msg.message_id}
            )
            ok += 1
        except Exception:
            fail += 1
    save_state(state)
    await schedule_expiration(context, bid, ttl_min)
    await update.message.reply_text(
        f"Рассылка завершена. Успешно: {ok}, ошибки: {fail}. "
        f"Заявка #{short_id(bid)} (TTL {ttl_min} мин)."
    )

async def schedule_expiration(context: ContextTypes.DEFAULT_TYPE, bid: str, ttl_min: int):
    context.job_queue.run_once(expire_job, when=timedelta(minutes=ttl_min), data={"bid": bid})

async def expire_job(ctx: ContextTypes.DEFAULT_TYPE):
    bid = ctx.job.data["bid"]
    state = load_state()
    bc = state["broadcasts"].get(bid)
    if not bc or bc.get("expired"):
        return
    bc["expired"] = True
    save_state(state)
    for msg in bc.get("messages", []):
        try:
            await ctx.bot.edit_message_text(
                chat_id=msg["chat_id"], message_id=msg["message_id"],
                text=render_message(bid, state),
                parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True
            )
        except Exception:
            pass

# ===============================
# Claim / Unclaim / Done
# ===============================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    m = re.match(r"^(claim|unclaim|done):(.+)$", data)
    if not m:
        return
    action, bid = m.group(1), m.group(2)
    bc = state["broadcasts"].get(bid)
    if not bc:
        await q.answer("Заявка не найдена.", show_alert=True)
        return
    if bc.get("expired"):
        await q.answer("Срок заявки истёк.", show_alert=True)
        return
    user = q.from_user
    if action == "claim":
        if bc.get("claimed_by"):
            await q.answer("Уже взяли.")
            return
        bc["claimed_by"] = {"id": user.id, "name": human_name(user)}
    elif action == "unclaim":
        claimer = bc.get("claimed_by")
        if not claimer:
            await q.answer("Уже свободна.")
            return
        if user.id != claimer.get("id") and not is_admin(user.id, state):
            await q.answer("Снять может только исполнитель или админ.", show_alert=True)
            return
        bc["claimed_by"] = None
    elif action == "done":
        claimer = bc.get("claimed_by")
        if not claimer:
            await q.answer("Нельзя исполнить незанятую заявку.", show_alert=True)
            return
        if user.id != claimer.get("id") and not is_admin(user.id, state):
            await q.answer("Исполнить может только исполнитель или админ.", show_alert=True)
            return
        bc["done"] = True
    save_state(state)
    kb = build_keyboard(bid, state)
    for msg in bc.get("messages", []):
        try:
            await context.bot.edit_message_text(
                chat_id=msg["chat_id"], message_id=msg["message_id"],
                text=render_message(bid, state), reply_markup=kb,
                parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True
            )
        except Exception:
            pass

# ===============================
# /new — шаблоны и пошаговый режим
# ===============================

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_admin(update.effective_user.id, state):
        await update.message.reply_text("Только админы могут создавать заявки.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("RUB→USDT | Сбер | 20 мин", callback_data="tpl:RUB→USDT|Сбер|20")],
        [InlineKeyboardButton("USDT→RUB | Тинькофф | 60 мин", callback_data="tpl:USDT→RUB|Тинькофф|60")],
        [InlineKeyboardButton("RUB→USDT | Альфа | день", callback_data="tpl:RUB→USDT|Альфа|180")],
        [InlineKeyboardButton("Собрать вручную ➡️", callback_data="manual:start")]
    ])
    await update.message.reply_text("Выберите шаблон или соберите вручную:", reply_markup=kb)

async def new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    # Быстрый шаблон
    if data.startswith("tpl:"):
        _, payload = data.split(":", 1)
        direction, bank, ttl_str = payload.split("|")
        ttl_min = int(ttl_str)
        text = f"{direction}\nБанк: {bank}\nВремя исполнения: {ttl_min if ttl_min < 180 else 'в течение дня'}"
        fake_update = update
        fake_update.message = q.message  # для совместимости
        await broadcast_simple(fake_update, context, text, ttl_min)
        return
    # Пошаговый (упрощённо: сразу показываем шаги)
    if data == "manual:start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("RUB→USDT", callback_data="manual:dir:RUB→USDT")],
            [InlineKeyboardButton("USDT→RUB", callback_data="manual:dir:USDT→RUB")]
        ])
        await q.message.edit_text("Выберите направление:", reply_markup=kb)
    elif data.startswith("manual:dir:"):
        direction = data.split(":", 2)[2]
        context.user_data["new_direction"] = direction
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Сбер", callback_data="manual:bank:Сбер")],
            [InlineKeyboardButton("Альфа", callback_data="manual:bank:Альфа")],
            [InlineKeyboardButton("Тинькофф", callback_data="manual:bank:Тинькофф")],
            [InlineKeyboardButton("СБП", callback_data="manual:bank:СБП")]
        ])
        await q.message.edit_text("Выберите банк:", reply_markup=kb)
    elif data.startswith("manual:bank:"):
        bank = data.split(":", 2)[2]
        context.user_data["new_bank"] = bank
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("20 минут", callback_data="manual:ttl:20")],
            [InlineKeyboardButton("1 час", callback_data="manual:ttl:60")],
            [InlineKeyboardButton("В течение дня", callback_data="manual:ttl:180")]
        ])
        await q.message.edit_text("Выберите время исполнения:", reply_markup=kb)
    elif data.startswith("manual:ttl:"):
        ttl_min = int(data.split(":", 2)[2])
        direction = context.user_data.get("new_direction")
        bank = context.user_data.get("new_bank")
        text = f"{direction}\nБанк: {bank}\nВремя исполнения: {ttl_min if ttl_min<180 else 'в течение дня'}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Отправить", callback_data=f"manual:send:{ttl_min}")],
            [InlineKeyboardButton("❌ Отмена", callback_data="manual:cancel")]
        ])
        context.user_data["new_text"] = text
        context.user_data["new_ttl"] = ttl_min
        await q.message.edit_text(f"Подтвердите заявку:\n\n{text}", reply_markup=kb)
    elif data.startswith("manual:send:"):
        ttl_min = context.user_data.get("new_ttl")
        text = context.user_data.get("new_text")
        fake_update = update
        fake_update.message = q.message
        await broadcast_simple(fake_update, context, text, ttl_min)
        context.user_data.clear()
    elif data == "manual:cancel":
        context.user_data.clear()
        await q.message.edit_text("❌ Заявка отменена.")

async def broadcast_simple(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, ttl_min: int):
    # Вспомогательная функция: сделать заявку напрямую
    state = load_state()
    bid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    state["broadcasts"][bid] = {
        "text": text, "created_at": created_at,
        "ttl_min": ttl_min, "messages": [],
        "claimed_by": None, "expired": False, "done": False
    }
    save_state(state)
    for cid in list(state["chats"]):
        try:
            msg = await context.bot.send_message(
                chat_id=cid, text=render_message(bid, state),
                reply_markup=build_keyboard(bid, state),
                parse_mode=constants.ParseMode.HTML
            )
            state["broadcasts"][bid]["messages"].append(
                {"chat_id": cid, "message_id": msg.message_id}
            )
        except Exception:
            pass
    save_state(state)
    await schedule_expiration(context, bid, ttl_min)
    await update.message.reply_text(f"Заявка #{short_id(bid)} отправлена.")

# ===============================
# Main
# ===============================

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Неизвестная команда. Наберите /help.")

def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set.")
        return
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("register", register_chat))
    app.add_handler(CommandHandler("unregister", unregister_chat))
    app.add_handler(CommandHandler("list", list_chats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CallbackQueryHandler(on_callback, pattern="^(claim|unclaim|done):"))
    app.add_handler(CallbackQueryHandler(new_callback, pattern="^(tpl:|manual:)"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
