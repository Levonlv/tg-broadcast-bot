import os, json, re, uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

STATE_FILE = os.getenv("STATE_FILE", "state.json")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x)
DEFAULT_TTL_MIN = int(os.getenv("DEFAULT_TTL_MIN", "15"))

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"admins": list(ADMIN_IDS), "chats": [], "broadcasts": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try: data = json.load(f)
        except Exception: data = {"admins": list(ADMIN_IDS), "chats": [], "broadcasts": {}}
    data["admins"] = list(set(data.get("admins", [])) | ADMIN_IDS)
    data.setdefault("chats", []); data.setdefault("broadcasts", {})
    return data

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def is_admin(uid: int, state: Dict[str, Any]) -> bool: return uid in set(state.get("admins", []))
def short_id(bid: str) -> str: return bid.split("-")[0]

def build_keyboard(bid: str, state: Dict[str, Any]):
    bc = state["broadcasts"].get(bid)
    if not bc or bc.get("expired", False): return None
    if not bc.get("claimed_by"):
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Взять", callback_data=f"claim:{bid}")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Снять", callback_data=f"unclaim:{bid}")]])

def human_name(u) -> str:
    parts = [p for p in [u.first_name, u.last_name] if p]
    base = " ".join(parts) if parts else (u.username or f"id:{u.id}")
    return f"{base} (@{u.username})" if u.username else base

def fmt_deadline(created_at_iso: str, ttl_min: int) -> str:
    created_dt = datetime.fromisoformat(created_at_iso)
    return (created_dt + timedelta(minutes=ttl_min)).strftime("%Y-%m-%d %H:%M")

def render_message(bid: str, state: Dict[str, Any]) -> str:
    bc = state["broadcasts"][bid]
    status = (
        "🔴 Статус: истёк срок"
        if bc.get("expired")
        else ("🟡 Статус: взята — " + bc["claimed_by"]["name"] if bc.get("claimed_by") else "🟢 Статус: свободна")
    )
    deadline = fmt_deadline(bc["created_at"], bc["ttl_min"])
    body = escape(bc["text"])  # важно: экранируем пользовательский текст, т.к. parse_mode=HTML
    return (
        f"📣 <b>Заявка #{short_id(bid)}</b>\n"
        f"{body}\n\n"
        f"⏳ Актуально до: <b>{deadline}</b> (≈{bc['ttl_min']} мин)\n"
        f"{status}"
    )


def parse_broadcast_args(raw: str):
    raw = raw.strip()
    m = re.match(r"^\s*(ttl\s*=\s*|\s*)(?P<num>\d{1,3})\s*(m|min|мин)?\s*(?P<rest>.*)$", raw, flags=re.IGNORECASE)
    ttl = None
    if m and m.group("num") and m.group("rest"):
        try:
            x = int(m.group("num"))
            if 1 <= x <= 180: ttl = x
            raw = m.group("rest").strip()
        except: pass
    return (ttl or DEFAULT_TTL_MIN), raw

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    role = "админ" if is_admin(update.effective_user.id, state) else "пользователь"
    await update.message.reply_text(
        "Привет! Я бот для широковещательных заявок партнёрам.\n\n"
        "Команды:\n"
        "/register — зарегистрировать текущий чат как целевой\n"
        "/unregister — убрать текущий чат\n"
        "/list — показать все чаты\n"
        "<code>/broadcast &lt;TTL мин&gt; &lt;текст&gt;</code> — разослать заявку (только админы)\n"
        "/help — справка\n\n"
        f"Ваш статус: {role}",
        parse_mode=constants.ParseMode.HTML
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await start(update, context)

async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_admin(update.effective_user.id, state): await update.message.reply_text("Только админы могут регистрировать чаты."); return
    cid = update.effective_chat.id
    if cid not in state["chats"]:
        state["chats"].append(cid); save_state(state)
        await update.message.reply_text(f"Чат зарегистрирован: {update.effective_chat.title or cid}")
    else: await update.message.reply_text("Чат уже в списке.")

async def unregister_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_admin(update.effective_user.id, state): await update.message.reply_text("Только админы могут убирать чаты."); return
    cid = update.effective_chat.id
    if cid in state["chats"]:
        state["chats"].remove(cid); save_state(state)
        await update.message.reply_text(f"Чат удалён: {update.effective_chat.title or cid}")
    else: await update.message.reply_text("Этого чата нет в списке.")

async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    lines = [f"• {cid}" for cid in state["chats"]] or ["(пусто)"]
    await update.message.reply_text("Целевые чаты:\n" + "\n".join(lines))

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_admin(update.effective_user.id, state): await update.message.reply_text("Только админы могут рассылать заявки."); return
    if not state["chats"]: await update.message.reply_text("Нет зарегистрированных чатов. Добавьте бота в группы и отправьте /register."); return
    raw = re.sub(r"^/broadcast(@\w+)?\s*", "", update.message.text or "", flags=re.IGNORECASE)
    ttl_min, text = parse_broadcast_args(raw)
    if not text: await update.message.reply_text("Формат: /broadcast <TTL мин> <текст>\nНапр.: /broadcast 12m Продаём дирхамы, Сбер, 150к."); return
    bid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    state["broadcasts"][bid] = {"text": text, "created_at": created_at, "ttl_min": ttl_min, "messages": [], "claimed_by": None, "expired": False}
    save_state(state)
    ok = fail = 0
    for cid in list(state["chats"]):
        try:
            msg = await context.bot.send_message(chat_id=cid, text=render_message(bid, state),
                                                 reply_markup=build_keyboard(bid, state),
                                                 parse_mode=constants.ParseMode.HTML,
                                                 disable_web_page_preview=True)
            state["broadcasts"][bid]["messages"].append({"chat_id": cid, "message_id": msg.message_id}); ok += 1
        except Exception: fail += 1
    save_state(state)
    await schedule_expiration(context, bid, ttl_min)
    await update.message.reply_text(f"Рассылка завершена. Успешно: {ok}, ошибки: {fail}. Заявка #{short_id(bid)} (TTL {ttl_min} мин).")

async def schedule_expiration(context: ContextTypes.DEFAULT_TYPE, bid: str, ttl_min: int):
    context.job_queue.run_once(expire_job, when=timedelta(minutes=ttl_min), data={"bid": bid}, name=f"expire:{bid}")

async def expire_job(ctx: ContextTypes.DEFAULT_TYPE):
    bid = ctx.job.data["bid"]
    state = load_state(); bc = state["broadcasts"].get(bid)
    if not bc or bc.get("expired"): return
    bc["expired"] = True; save_state(state)
    for msg in bc.get("messages", []):
        try:
            await ctx.bot.edit_message_text(chat_id=msg["chat_id"], message_id=msg["message_id"],
                                            text=render_message(bid, state),
                                            parse_mode=constants.ParseMode.HTML,
                                            disable_web_page_preview=True)
        except Exception: pass

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state(); q = update.callback_query; await q.answer()
    m = re.match(r"^(claim|unclaim):(.+)$", q.data or "")
    if not m: return
    action, bid = m.group(1), m.group(2)
    bc = state["broadcasts"].get(bid)
    if not bc: await q.answer("Заявка не найдена.", show_alert=True); return
    if bc.get("expired"): await q.answer("Срок заявки истёк.", show_alert=True); return
    user = q.from_user
    if action == "claim":
        if bc.get("claimed_by"): await q.answer("Уже взяли."); return
        bc["claimed_by"] = {"id": user.id, "name": human_name(user), "username": user.username,
                            "ts": datetime.now().isoformat(timespec="seconds")}
    else:
        claimer = bc.get("claimed_by")
        if not claimer: await q.answer("Уже свободна."); return
        if user.id != claimer.get("id") and not is_admin(user.id, state):
            await q.answer("Снять может только исполнитель или админ.", show_alert=True); return
        bc["claimed_by"] = None
    save_state(state)
    kb = build_keyboard(bid, state)
    for msg in bc.get("messages", []):
        try:
            await context.bot.edit_message_text(chat_id=msg["chat_id"], message_id=msg["message_id"],
                                                text=render_message(bid, state), reply_markup=kb,
                                                parse_mode=constants.ParseMode.HTML,
                                                disable_web_page_preview=True)
        except Exception: pass

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Неизвестная команда. Наберите /help.")

def main():
    if not BOT_TOKEN: print("ERROR: BOT_TOKEN is not set."); return
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start","help"], start))
    app.add_handler(CommandHandler("register", register_chat))
    app.add_handler(CommandHandler("unregister", unregister_chat))
    app.add_handler(CommandHandler("list", list_chats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
