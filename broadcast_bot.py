#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

# ===============================
# CONFIG
# ===============================

def getenv_int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val and val.strip() else default
    except Exception:
        return default

STATE_FILE = os.getenv("STATE_FILE", "state.json")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DEFAULT_TTL_MIN = getenv_int("DEFAULT_TTL_MIN", 15)
DEFAULT_TZ = os.getenv("TZ", "Asia/Dubai")  # Для форматирования дедлайнов

# ===============================
# MODELS
# ===============================

@dataclass
class MessageRef:
    chat_id: int
    message_id: int

@dataclass
class Claimer:
    id: int
    name: str

@dataclass
class Broadcast:
    id: str
    text: str
    created_ts: int  # unix epoch (UTC)
    ttl_min: int
    messages: List[MessageRef] = field(default_factory=list)
    claimed_by: Optional[Claimer] = None
    expired: bool = False
    done: bool = False

    def deadline_ts(self) -> int:
        return self.created_ts + self.ttl_min * 60

# ===============================
# STORAGE (async + in-proc lock + atomic writes)
# ===============================

class StateStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._state: Dict[str, Any] = {
            "admins": list(ADMIN_IDS),
            "chats": [],
            "broadcasts": {},  # bid -> Broadcast as dict
            "version": 1,
        }

    async def load(self):
        async with self._lock:
            if not os.path.exists(self.path):
                return
            try:
                data = await asyncio.to_thread(self._read_json, self.path)
                # базовая валидация/миграция
                data.setdefault("admins", [])
                data.setdefault("chats", [])
                data.setdefault("broadcasts", {})
                data["admins"] = list(set(data["admins"]) | ADMIN_IDS)
                self._state = data
            except Exception as e:
                logging.exception("Failed to load state: %s", e)

    def _read_json(self, p: str) -> Dict[str, Any]:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    async def save(self):
        async with self._lock:
            tmp_path = f"{self.path}.tmp"
            try:
                await asyncio.to_thread(self._write_json_atomic, tmp_path, self.path, self._state)
            except Exception as e:
                logging.exception("Failed to save state: %s", e)

    def _write_json_atomic(self, tmp: str, final: str, data: Dict[str, Any]):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)

    # ---------- accessors ----------
    async def list_chats(self) -> List[int]:
        async with self._lock:
            return list(self._state.get("chats", []))

    async def add_chat(self, chat_id: int) -> bool:
        async with self._lock:
            chats = self._state.setdefault("chats", [])
            if chat_id not in chats:
                chats.append(chat_id)
                return True
            return False

    async def remove_chat(self, chat_id: int) -> bool:
        async with self._lock:
            chats = self._state.setdefault("chats", [])
            if chat_id in chats:
                chats.remove(chat_id)
                return True
            return False

    async def is_admin(self, uid: int) -> bool:
        async with self._lock:
            return uid in set(self._state.get("admins", []))

    async def upsert_broadcast(self, bc: Broadcast):
        async with self._lock:
            self._state["broadcasts"][bc.id] = asdict(bc)

    async def get_broadcast(self, bid: str) -> Optional[Broadcast]:
        async with self._lock:
            raw = self._state["broadcasts"].get(bid)
            return self._from_dict(raw) if raw else None

    async def update_broadcast(self, bid: str, updater):
        async with self._lock:
            raw = self._state["broadcasts"].get(bid)
            if not raw:
                return None
            bc = self._from_dict(raw)
            updater(bc)
            self._state["broadcasts"][bid] = asdict(bc)
            return bc

    async def iter_broadcasts(self) -> List[Broadcast]:
        async with self._lock:
            return [self._from_dict(v) for v in self._state.get("broadcasts", {}).values()]

    def _from_dict(self, raw: Dict[str, Any]) -> Broadcast:
        # back-compat on messages/claimed_by shape
        msgs = [MessageRef(**m) for m in raw.get("messages", [])]
        claimer = raw.get("claimed_by")
        cl = Claimer(**claimer) if claimer else None
        return Broadcast(
            id=raw["id"],
            text=raw["text"],
            created_ts=raw["created_ts"],
            ttl_min=raw["ttl_min"],
            messages=msgs,
            claimed_by=cl,
            expired=raw.get("expired", False),
            done=raw.get("done", False),
        )

STATE = StateStore(STATE_FILE)

# ===============================
# UTILITIES
# ===============================

def now_ts() -> int:
    return int(time.time())

def short_id(bid: str) -> str:
    return bid.split("-")[0]

def human_name(u) -> str:
    parts = [p for p in [u.first_name, u.last_name] if p]
    base = " ".join(parts) if parts else (u.username or f"id:{u.id}")
    return f"{base} (@{u.username})" if u.username else base

def parse_ttl_and_text(raw: str, default_min: int) -> tuple[int, str]:
    """
    Поддерживает:
      "30 ...", "ttl=30 ...", "30m ...", "1h ...", "2ч ...", "90 мин ..."
    Возвращает (ttl_min, text)
    """
    s = (raw or "").strip()
    # Уберём саму команду, если пришло целиком message.text
    s = re.sub(r"^/broadcast(@\w+)?\s*", "", s, flags=re.IGNORECASE).strip()

    # pattern: [ttl part][rest]
    m = re.match(
        r"^(?:ttl\s*=\s*)?(?P<num>\d{1,3})\s*(?P<unit>m|min|мин|h|hr|ч|час)?\s*(?P<rest>.*)$",
        s, flags=re.IGNORECASE
    )
    ttl = default_min
    text = s
    if m:
        num = int(m.group("num"))
        unit = (m.group("unit") or "m").lower()
        rest = (m.group("rest") or "").strip()
        if unit in ("h", "hr", "ч", "час"):
            ttl = min(max(num * 60, 1), 720)   # до 12 часов
        else:
            ttl = min(max(num, 1), 180)       # до 3 часов по умолчанию
        if rest:
            text = rest

    return ttl, text

def format_deadline_local(deadline_ts: int) -> str:
    # показываем локально (Asia/Dubai по умолчанию)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DEFAULT_TZ)
    except Exception:
        tz = timezone(timedelta(hours=4))  # fallback Gulf
    return datetime.fromtimestamp(deadline_ts, tz).strftime("%Y-%m-%d %H:%M")

def build_keyboard(bc: Broadcast) -> Optional[InlineKeyboardMarkup]:
    if bc.expired:
        return None
    if not bc.claimed_by:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Взять", callback_data=f"claim:{bc.id}")]
        ])
    if not bc.done:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Снять", callback_data=f"unclaim:{bc.id}")],
            [InlineKeyboardButton("✔️ Исполнено", callback_data=f"done:{bc.id}")]
        ])
    return None

def render_message(bc: Broadcast) -> str:
    if bc.expired:
        status = "🔴 Статус: истёк срок"
    elif bc.done:
        status = f"🟢 Статус: исполнена — {bc.claimed_by.name if bc.claimed_by else '—'}"
    elif bc.claimed_by:
        status = f"🟡 Статус: взята — {bc.claimed_by.name}"
    else:
        status = "🟢 Статус: свободна"
    deadline = format_deadline_local(bc.deadline_ts())
    return (
        f"📣 <b>Заявка #{short_id(bc.id)}</b>\n"
        f"{bc.text}\n\n"
        f"⏳ Актуально до: <b>{deadline}</b> (≈{bc.ttl_min} мин)\n"
        f"{status}"
    )

async def safe_send_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                         reply_markup: Optional[InlineKeyboardMarkup] = None):
    tries = 0
    while True:
        try:
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except (TimedOut, NetworkError):
            if tries < 3:
                tries += 1
                await asyncio.sleep(1.5 * tries)
                continue
            raise

async def safe_edit_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int,
                         text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    try:
        return await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        # Например "message is not modified" — игнорируем
        if "message is not modified" in str(e).lower():
            return None
        raise

# ===============================
# HANDLERS: Commands
# ===============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = "админ" if await STATE.is_admin(update.effective_user.id) else "пользователь"
    await update.message.reply_text(
        "Привет! Я бот для широковещательных заявок партнёрам.\n\n"
        "Команды:\n"
        "/register — зарегистрировать текущий чат\n"
        "/unregister — убрать чат\n"
        "/list — показать все чаты\n"
        "/broadcast <TTL> <текст> — разослать заявку (только админы)\n"
        "/new — создать заявку кнопками (только админы)\n"
        "/status — состояние бота\n"
        "/help — справка\n\n"
        f"Ваш статус: {role}\n"
        f"TTL по умолчанию: {DEFAULT_TTL_MIN} мин",
        parse_mode=ParseMode.HTML,
    )

help_cmd = start

async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await STATE.is_admin(update.effective_user.id):
        await update.message.reply_text("Только админы могут регистрировать чаты.")
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        await update.message.reply_text("Регистрировать имеет смысл только группы/каналы.")
        return
    cid = update.effective_chat.id
    if await STATE.add_chat(cid):
        await STATE.save()
        await update.message.reply_text(f"Чат зарегистрирован: {update.effective_chat.title or cid}")
    else:
        await update.message.reply_text("Чат уже в списке.")

async def unregister_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await STATE.is_admin(update.effective_user.id):
        await update.message.reply_text("Только админы могут убирать чаты.")
        return
    cid = update.effective_chat.id
    if await STATE.remove_chat(cid):
        await STATE.save()
        await update.message.reply_text(f"Чат удалён: {update.effective_chat.title or cid}")
    else:
        await update.message.reply_text("Этого чата нет в списке.")

async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats = await STATE.list_chats()
    lines = [f"• {cid}" for cid in chats] or ["(пусто)"]
    await update.message.reply_text("Целевые чаты:\n" + "\n".join(lines))

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bcs = await STATE.iter_broadcasts()
    total = len(bcs)
    open_cnt = sum(1 for b in bcs if not b.expired and not b.done)
    claimed = sum(1 for b in bcs if b.claimed_by and not b.expired and not b.done)
    done = sum(1 for b in bcs if b.done)
    expired = sum(1 for b in bcs if b.expired)
    chats = len(await STATE.list_chats())
    await update.message.reply_text(
        f"Статус:\n"
        f"Чатов: {chats}\n"
        f"Заявок всего: {total}\n"
        f"Открытых: {open_cnt}, взятых: {claimed}\n"
        f"Исполнено: {done}, истекло: {expired}"
    )

# ===============================
# BROADCAST
# ===============================

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await STATE.is_admin(update.effective_user.id):
        await update.message.reply_text("Только админы могут рассылать заявки.")
        return

    chats = await STATE.list_chats()
    if not chats:
        await update.message.reply_text("Нет зарегистрированных чатов. /register")
        return

    ttl_min, text = parse_ttl_and_text(update.message.text or "", DEFAULT_TTL_MIN)
    if not text:
        await update.message.reply_text("Формат: /broadcast <TTL> <текст>")
        return

    bc = Broadcast(
        id=str(uuid.uuid4()),
        text=text,
        created_ts=now_ts(),
        ttl_min=ttl_min,
    )
    await STATE.upsert_broadcast(bc)
    await STATE.save()

    ok = fail = removed = 0
    for cid in list(chats):
        try:
            msg = await safe_send_text(
                context,
                chat_id=cid,
                text=render_message(bc),
                reply_markup=build_keyboard(bc),
            )
            bc.messages.append(MessageRef(chat_id=cid, message_id=msg.message_id))
            ok += 1
        except Forbidden:
            # Бот удалён/нет прав — выпилим чат из регистра
            removed += 1
            await STATE.remove_chat(cid)
        except Exception as e:
            logging.warning("Send to %s failed: %s", cid, e)
            fail += 1

    # сохранить сообщения + возможные изменения
    await STATE.upsert_broadcast(bc)
    await STATE.save()

    # планируем истечение
    await schedule_expiration(context, bc)

    await update.message.reply_text(
        f"Рассылка завершена. Успешно: {ok}, ошибки: {fail}, удалено чатов: {removed}. "
        f"Заявка #{short_id(bc.id)} (TTL {bc.ttl_min} мин)."
    )

async def schedule_expiration(context: ContextTypes.DEFAULT_TYPE, bc: Broadcast):
    delay_sec = max(bc.deadline_ts() - now_ts(), 0)
    context.job_queue.run_once(
        expire_job,
        when=delay_sec,
        data={"bid": bc.id},
        name=f"expire:{bc.id}",
    )

async def expire_job(ctx: ContextTypes.DEFAULT_TYPE):
    bid = ctx.job.data["bid"]
    bc = await STATE.update_broadcast(bid, lambda x: setattr(x, "expired", True))
    if not bc:
        return
    await STATE.save()

    # обновляем все сообщения
    for msg in bc.messages:
        try:
            await safe_edit_text(ctx, msg.chat_id, msg.message_id, render_message(bc), None)
        except Forbidden:
            # удалённый чат — игнор
            pass
        except Exception as e:
            logging.debug("Edit on expire failed for %s: %s", msg.chat_id, e)

# ===============================
# CLAIM / UNCLAIM / DONE
# ===============================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    m = re.match(r"^(claim|unclaim|done):(.+)$", data)
    if not m:
        return
    action, bid = m.group(1), m.group(2)
    bc = await STATE.get_broadcast(bid)
    if not bc:
        await q.answer("Заявка не найдена.", show_alert=True)
        return
    if bc.expired:
        await q.answer("Срок заявки истёк.", show_alert=True)
        return

    user = q.from_user

    if action == "claim":
        if bc.claimed_by:
            await q.answer("Уже взяли.")
            return
        bc.claimed_by = Claimer(id=user.id, name=human_name(user))

    elif action == "unclaim":
        if not bc.claimed_by:
            await q.answer("Уже свободна.")
            return
        if user.id != bc.claimed_by.id and not await STATE.is_admin(user.id):
            await q.answer("Снять может только исполнитель или админ.", show_alert=True)
            return
        bc.claimed_by = None

    elif action == "done":
        if not bc.claimed_by:
            await q.answer("Нельзя исполнить незанятую заявку.", show_alert=True)
            return
        if user.id != bc.claimed_by.id and not await STATE.is_admin(user.id):
            await q.answer("Исполнить может только исполнитель или админ.", show_alert=True)
            return
        bc.done = True

    await STATE.upsert_broadcast(bc)
    await STATE.save()

    kb = build_keyboard(bc)
    for msg in bc.messages:
        try:
            await safe_edit_text(context, msg.chat_id, msg.message_id, render_message(bc), kb)
        except Exception:
            pass

# ===============================
# /new — шаблоны и пошаговый режим
# ===============================

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await STATE.is_admin(update.effective_user.id):
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
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    # Быстрый шаблон
    if data.startswith("tpl:"):
        _, payload = data.split(":", 1)
        direction, bank, ttl_str = payload.split("|")
        ttl_min = int(ttl_str)
        text = f"{direction}\nБанк: {bank}\nВремя исполнения: {ttl_min if ttl_min < 180 else 'в течение дня'}"
        # Создаём и шлём
        await broadcast_simple(q.message, context, text, ttl_min)
        return

    # Пошаговый
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
        text = f"{direction}\nБанк: {bank}\nВремя исполнения: {ttl_min if ttl_min < 180 else 'в течение дня'}"
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
        await broadcast_simple(q.message, context, text, ttl_min)
        context.user_data.clear()
    elif data == "manual:cancel":
        context.user_data.clear()
        await q.message.edit_text("❌ Заявка отменена.")

async def broadcast_simple(message, context: ContextTypes.DEFAULT_TYPE, text: str, ttl_min: int):
    if not text:
        await message.reply_text("Пустой текст заявки.")
        return
    chats = await STATE.list_chats()
    if not chats:
        await message.reply_text("Нет зарегистрированных чатов. /register")
        return

    bc = Broadcast(
        id=str(uuid.uuid4()),
        text=text,
        created_ts=now_ts(),
        ttl_min=ttl_min,
    )
    await STATE.upsert_broadcast(bc)
    await STATE.save()

    ok = fail = removed = 0
    for cid in list(chats):
        try:
            msg = await safe_send_text(context, cid, render_message(bc), build_keyboard(bc))
            bc.messages.append(MessageRef(chat_id=cid, message_id=msg.message_id))
            ok += 1
        except Forbidden:
            removed += 1
            await STATE.remove_chat(cid)
        except Exception as e:
            logging.warning("Send to %s failed: %s", cid, e)
            fail += 1

    await STATE.upsert_broadcast(bc)
    await STATE.save()
    await schedule_expiration(context, bc)

    await message.reply_text(f"Заявка #{short_id(bc.id)} отправлена. "
                             f"Успешно: {ok}, ошибки: {fail}, удалено чатов: {removed}.")

# ===============================
# OTHER HANDLERS
# ===============================

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Неизвестная команда. Наберите /help.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Exception while handling an update: %s", context.error)

# ===============================
# STARTUP: rehydrate jobs
# ===============================

async def post_init(app: Application):
    # Восстановить state и дозапланировать истечения
    await STATE.load()
    bcs = await STATE.iter_broadcasts()
    for bc in bcs:
        if bc.done or bc.expired:
            continue
        if bc.deadline_ts() <= now_ts():
            # уже истекло — пометить и обновить сообщения
            await STATE.update_broadcast(bc.id, lambda x: setattr(x, "expired", True))
            await STATE.save()
            for msg in bc.messages:
                try:
                    await app.bot.edit_message_text(
                        chat_id=msg.chat_id,
                        message_id=msg.message_id,
                        text=render_message(bc),
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
        else:
            # запланировать по оставшемуся времени
            delay = max(bc.deadline_ts() - now_ts(), 0)
            app.job_queue.run_once(expire_job, when=delay, data={"bid": bc.id}, name=f"expire:{bc.id}")

# ===============================
# MAIN
# ===============================

def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    # Логи в stdout (systemd/docker-friendly)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    defaults = Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())  # защита от rate-limit
        .concurrent_updates(True)
        .defaults(defaults)
        .post_init(post_init)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("register", register_chat))
    app.add_handler(CommandHandler("unregister", unregister_chat))
    app.add_handler(CommandHandler("list", list_chats))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # Конструктор заявок
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(claim|unclaim|done):"))
    app.add_handler(CallbackQueryHandler(new_callback, pattern=r"^(tpl:|manual:)"))

    # Unknown
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # Глобальная обработка ошибок
    app.add_error_handler(error_handler)

    logging.info("Bot is starting...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=None,  # пусть PTB обработает SIGINT/SIGTERM по умолчанию
    )

if __name__ == "__main__":
    main()
