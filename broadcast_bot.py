import asyncio
import json
import logging
import os
import re
import string
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ===============================
# Настройки и инициализация
# ===============================

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
DEFAULT_TTL_MIN = int(os.getenv("DEFAULT_TTL_MIN", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required!")

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ===============================
# Хранение состояния
# ===============================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"admins": ADMIN_IDS, "chats": [], "broadcasts": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()
admins = set(state.get("admins", []))

# ===============================
# Утилиты
# ===============================

def gen_id():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))

def parse_ttl(text: str):
    m = re.search(r"(\d+)\s*(m|min|мин|minute|minutes|h|ч|hour|hours)?", text, re.I)
    if not m:
        return DEFAULT_TTL_MIN
    num = int(m.group(1))
    unit = m.group(2) or "m"
    if unit.lower().startswith(("h", "ч")):
        ttl = num * 60
    else:
        ttl = num
    return max(1, min(ttl, 180))

def render_message(b: dict, tz_offset: int = 0):
    expire_time = datetime.fromisoformat(b["expire_at"]).astimezone(timezone(timedelta(hours=tz_offset)))
    expire_str = expire_time.strftime("%Y-%m-%d %H:%M")
    status = "свободна"
    if b.get("executor"):
        status = f"взята — {b['executor']}"
    if b.get("expired"):
        status = "истёк срок"
    if b.get("done"):
        status = f"исполнена — {b['executor']}"
    return (f"<b>Заявка #{b['id']}</b>\n"
            f"{b['text']}\n\n"
            f"Актуально до: {expire_str} (≈{b['ttl']} мин)\n"
            f"Статус: {status}")

def build_keyboard(b: dict):
    kb = InlineKeyboardBuilder()
    if not b.get("executor") and not b.get("expired"):
        kb.button(text="✅ Взять", callback_data=f"take_{b['id']}")
    elif b.get("executor") and not b.get("expired") and not b.get("done"):
        kb.button(text="♻️ Снять", callback_data=f"drop_{b['id']}")
        kb.button(text="✔️ Исполнено", callback_data=f"done_{b['id']}")
    kb.adjust(1)
    return kb.as_markup()

async def update_broadcast_messages(b: dict):
    for chat_id, msg_id in b["messages"]:
        try:
            await bot.edit_message_text(
                render_message(b),
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=build_keyboard(b)
            )
        except Exception as e:
            logging.warning(f"Failed to update message {msg_id} in chat {chat_id}: {e}")

async def expire_broadcast(b: dict):
    await asyncio.sleep(b["ttl"] * 60)
    b["expired"] = True
    save_state()
    await update_broadcast_messages(b)

async def broadcast(text: str, ttl_min: int):
    if not state["chats"]:
        return
    bid = gen_id()
    expire_at = (datetime.utcnow() + timedelta(minutes=ttl_min)).isoformat()
    b = {
        "id": bid,
        "text": text,
        "ttl": ttl_min,
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": expire_at,
        "messages": [],
        "executor": None,
        "expired": False,
        "done": False
    }
    errors = 0
    for chat_id in state["chats"]:
        try:
            m = await bot.send_message(chat_id, render_message(b), reply_markup=build_keyboard(b))
            b["messages"].append((chat_id, m.message_id))
        except Exception as e:
            logging.error(f"Broadcast error {chat_id}: {e}")
            errors += 1
    state["broadcasts"].append(b)
    save_state()
    asyncio.create_task(expire_broadcast(b))
    return f"Успешно: {len(b['messages'])}, ошибки: {errors}. Заявка #{bid} (TTL {ttl_min} мин)"

# ===============================
# FSM для /new
# ===============================

class NewRequest(StatesGroup):
    direction = State()
    bank = State()
    ttl = State()

@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    if message.from_user.id not in admins:
        return await message.answer("⛔ Только админ может создавать заявки.")

    kb = InlineKeyboardBuilder()
    kb.button(text="RUB→USDT | Сбер | 20 мин", callback_data="tpl_rub_usdt_sber_20")
    kb.button(text="USDT→RUB | Тинькофф | 1 час", callback_data="tpl_usdt_rub_tink_60")
    kb.button(text="RUB→USDT | Альфа | день", callback_data="tpl_rub_usdt_alfa_day")
    kb.button(text="Собрать вручную ➡️", callback_data="manual_start")
    kb.adjust(1)

    await message.answer("Выберите шаблон или соберите заявку вручную:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("tpl_"))
async def cb_template(call: CallbackQuery):
    _, d1, d2, bank, ttl_raw = call.data.split("_")
    direction = f"{d1.upper()}→{d2.upper()}"
    bank_name = bank.capitalize()
    if ttl_raw == "day":
        ttl_min = 180
        ttl_text = "в течение дня"
    else:
        ttl_min = int(ttl_raw)
        ttl_text = f"{ttl_min} минут" if ttl_min < 60 else f"{ttl_min//60} час"
    text = f"{direction}\nБанк: {bank_name}\nВремя исполнения: {ttl_text}"
    await call.answer("Заявка создана!")
    await broadcast(text, ttl_min)

@router.callback_query(F.data == "manual_start")
async def cb_manual_start(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="RUB→USDT", callback_data="dir_rub_usdt")
    kb.button(text="USDT→RUB", callback_data="dir_usdt_rub")
    kb.adjust(2)
    await call.message.edit_text("Выберите направление:", reply_markup=kb.as_markup())
    await state.set_state(NewRequest.direction)

@router.callback_query(F.data.startswith("dir_"), NewRequest.direction)
async def cb_direction(call: CallbackQuery, state: FSMContext):
    direction = call.data.replace("dir_", "").replace("_", "→").upper()
    await state.update_data(direction=direction)

    kb = InlineKeyboardBuilder()
    for bank in ["Сбер", "Альфа", "Тинькофф", "СБП"]:
        kb.button(text=bank, callback_data=f"bank_{bank.lower()}")
    kb.adjust(2)
    await call.message.edit_text("Выберите банк:", reply_markup=kb.as_markup())
    await state.set_state(NewRequest.bank)

@router.callback_query(F.data.startswith("bank_"), NewRequest.bank)
async def cb_bank(call: CallbackQuery, state: FSMContext):
    bank = call.data.replace("bank_", "").capitalize()
    await state.update_data(bank=bank)

    kb = InlineKeyboardBuilder()
    kb.button(text="20 минут", callback_data="ttl_20")
    kb.button(text="1 час", callback_data="ttl_60")
    kb.button(text="В течение дня", callback_data="ttl_day")
    kb.adjust(1)
    await call.message.edit_text("Выберите время исполнения:", reply_markup=kb.as_markup())
    await state.set_state(NewRequest.ttl)

@router.callback_query(F.data.startswith("ttl_"), NewRequest.ttl)
async def cb_ttl(call: CallbackQuery, state: FSMContext):
    ttl_raw = call.data.replace("ttl_", "")
    if ttl_raw == "day":
        ttl_min = 180
        ttl_text = "в течение дня"
    else:
        ttl_min = int(ttl_raw)
        ttl_text = f"{ttl_min} минут" if ttl_min < 60 else f"{ttl_min//60} час"
    await state.update_data(ttl_min=ttl_min, ttl_text=ttl_text)
    data = await state.get_data()
    text = f"{data['direction']}\nБанк: {data['bank']}\nВремя исполнения: {ttl_text}"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить заявку", callback_data="send_request")
    kb.button(text="❌ Отмена", callback_data="cancel_request")
    kb.adjust(1)
    await call.message.edit_text(f"Подтверждаете заявку?\n\n{text}", reply_markup=kb.as_markup())

@router.callback_query(F.data == "send_request", NewRequest.ttl)
async def cb_send_request(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await call.answer("Заявка отправлена!")
    await broadcast(f"{data['direction']}\nБанк: {data['bank']}\nВремя исполнения: {data['ttl_text']}", data['ttl_min'])
    await state.clear()

@router.callback_query(F.data == "cancel_request", NewRequest.ttl)
async def cb_cancel_request(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Заявка отменена.")

# ===============================
# Команды /start, /help, /register, /unregister, /list, /broadcast
# ===============================

@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    role = "админ" if message.from_user.id in admins else "пользователь"
    await message.answer(f"Привет! Ты {role}.\n\nДоступные команды:\n"
                         "/register — добавить чат\n"
                         "/unregister — убрать чат\n"
                         "/list — список чатов\n"
                         "/broadcast <TTL> <текст>\n"
                         "/new — создать заявку кнопками")

@router.message(Command("register"))
async def cmd_register(message: Message):
    if message.from_user.id not in admins:
        return await message.answer("⛔ Только админ может регистрировать чаты.")
    chat_id = message.chat.id
    if chat_id not in state["chats"]:
        state["chats"].append(chat_id)
        save_state()
        await message.answer(f"Чат {chat_id} зарегистрирован.")
    else:
        await message.answer("Чат уже в списке.")

@router.message(Command("unregister"))
async def cmd_unregister(message: Message):
    if message.from_user.id not in admins:
        return await message.answer("⛔ Только админ может убирать чаты.")
    chat_id = message.chat.id
    if chat_id in state["chats"]:
        state["chats"].remove(chat_id)
        save_state()
        await message.answer(f"Чат {chat_id} удалён.")
    else:
        await message.answer("Чат не найден.")

@router.message(Command("list"))
async def cmd_list(message: Message):
    if not state["chats"]:
        return await message.answer("Список чатов пуст.")
    text = "Подключенные чаты:\n" + "\n".join(str(c) for c in state["chats"])
    await message.answer(text)

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in admins:
        return await message.answer("⛔ Только админ может рассылать заявки.")
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        return await message.answer("Использование: /broadcast <TTL> <текст>")
    ttl = parse_ttl(args[1])
    text = args[2] if len(args) > 2 else ""
    result = await broadcast(text, ttl)
    await message.answer(result)

# ===============================
# Кнопки заявок (взять/снять/исполнено)
# ===============================

@router.callback_query(F.data.startswith("take_"))
async def cb_take(call: CallbackQuery):
    bid = call.data.replace("take_", "")
    b = next((x for x in state["broadcasts"] if x["id"] == bid), None)
    if not b or b.get("executor") or b.get("expired"):
        return await call.answer("Уже занято или истекло.", show_alert=True)
    user = call.from_user
    name = user.full_name or user.username or str(user.id)
    b["executor"] = name
    save_state()
    await update_broadcast_messages(b)
    await call.answer("Вы взяли заявку!")

@router.callback_query(F.data.startswith("drop_"))
async def cb_drop(call: CallbackQuery):
    bid = call.data.replace("drop_", "")
    b = next((x for x in state["broadcasts"] if x["id"] == bid), None)
    if not b or not b.get("executor"):
        return await call.answer("Заявка не взята.", show_alert=True)
    user = call.from_user
    name = user.full_name or user.username or str(user.id)
    if name != b["executor"] and user.id not in admins:
        return await call.answer("Снять может только исполнитель или админ.", show_alert=True)
    b["executor"] = None
    save_state()
    await update_broadcast_messages(b)
    await call.answer("Заявка снова свободна.")

@router.callback_query(F.data.startswith("done_"))
async def cb_done(call: CallbackQuery):
    bid = call.data.replace("done_", "")
    b = next((x for x in state["broadcasts"] if x["id"] == bid), None)
    if not b or not b.get("executor"):
        return await call.answer("Заявка не взята.", show_alert=True)
    user = call.from_user
    name = user.full_name or user.username or str(user.id)
    if name != b["executor"] and user.id not in admins:
        return await call.answer("Отметить исполненной может только исполнитель или админ.", show_alert=True)
    b["done"] = True
    save_state()
    await update_broadcast_messages(b)
    await call.answer("Заявка исполнена!")

# ===============================
# Main
# ===============================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
