from __future__ import annotations

import os
import json
import asyncio
import re
import time
import requests
from pathlib import Path

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.dispatcher.handler import CancelHandler

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from worker import spam_worker
from config import BOT_TOKEN, API_ID, API_HASH, DIGIBOT_BASE_URL, DIGIBOT_INTERNAL_API_KEY
from premium_emoji import PremiumEmoji


print("=== BOT.PY STARTED ===", flush=True)
print("CWD:", os.getcwd(), flush=True)
print("FILES:", os.listdir("."), flush=True)

# ======================
# PERSISTENT STORAGE
# ======================
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_ROOT = DATA_DIR / "users"
USERS_ROOT.mkdir(parents=True, exist_ok=True)

OVERRIDE_FILE = DATA_DIR / "access_override.json"

# ======================
# INIT
# ======================
bot = Bot(BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

workers = {}
login_clients = {}

PHONE_RE = re.compile(r"^\+\d{10,15}$")

# ======================
# ADMINS
# ======================
ADMINS = {7447763153}  # <-- ЗАМЕНИ НА СВОЙ TG_ID

# ======================
# PREMIUM EMOJI
# ======================
PREMIUM_STICKER_SETS = [
    "sefhvm_by_EmojiTitleBot",
]

premium: PremiumEmoji | None = None


def _p() -> PremiumEmoji:
    global premium
    if premium is None:
        premium = PremiumEmoji(emoji_map={})
    return premium


# ======================
# OVERRIDES
# ======================
def _load_overrides() -> dict:
    if not OVERRIDE_FILE.exists():
        return {}
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_overrides(data: dict) -> None:
    with open(OVERRIDE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def grant_access(uid: int, minutes: int | None = None) -> None:
    data = _load_overrides()
    if minutes is None:
        data[str(uid)] = {"expires": 0}
    else:
        data[str(uid)] = {"expires": int(time.time()) + int(minutes) * 60}
    _save_overrides(data)


def revoke_access(uid: int) -> None:
    data = _load_overrides()
    data.pop(str(uid), None)
    _save_overrides(data)


def has_override(uid: int) -> bool:
    data = _load_overrides()
    row = data.get(str(uid))
    if not row:
        return False

    exp = int(row.get("expires", 0))
    if exp == 0:
        return True

    return time.time() < exp


# ======================
# HELPERS
# ======================
def user_dir(uid: int) -> Path:
    path = USERS_ROOT / f"user_{uid}"
    (path / "sessions").mkdir(parents=True, exist_ok=True)
    return path


def get_user_data(user_id: int):
    user_file = user_dir(user_id) / "user_data.json"
    if user_file.exists():
        with open(user_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_user_data(user_id: int, data: dict):
    udir = user_dir(user_id)
    with open(udir / "user_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_settings(uid: int):
    file = user_dir(uid) / "settings.json"
    if not file.exists():
        return None
    with open(file, "r", encoding="utf-8") as f:
        return json.load(f)


def get_user_text(uid: int):
    file = user_dir(uid) / "message.json"
    if not file.exists():
        return None

    with open(file, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") == "forward":
        return "✨ Пересланное сообщение\nPremium-стикеры сохранятся"

    return data.get("text", "")


def get_sessions(uid: int):
    sess_dir = user_dir(uid) / "sessions"
    if not sess_dir.exists():
        return []
    return [f for f in os.listdir(sess_dir) if f.endswith(".session")]


def get_accounts_info(uid: int):
    file = user_dir(uid) / "accounts.json"
    if not file.exists():
        return []
    with open(file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_accounts_info(uid: int, accounts: list):
    file = user_dir(uid) / "accounts.json"
    with open(file, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)


# ======================
# FREE TRIAL
# ======================
TRIAL_SECONDS = 24 * 60 * 60


def get_tariff(uid: int):
    tf = user_dir(uid) / "tariff.json"

    if not tf.exists():
        data = {
            "name": "FREE",
            "expires": int(time.time()) + TRIAL_SECONDS,
            "max_accounts": 999999999
        }
        with open(tf, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        user_data = get_user_data(uid)
        if user_data:
            user_data["trial_start_time"] = time.time()
            save_user_data(uid, user_data)

        return data

    with open(tf, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "max_accounts" not in data:
        data["max_accounts"] = 999999999
        with open(tf, "w", encoding="utf-8") as fw:
            json.dump(data, fw, indent=2, ensure_ascii=False)

    return data


def trial_is_active(uid: int) -> bool:
    t = get_tariff(uid)
    try:
        return bool(t.get("expires")) and time.time() < float(t["expires"])
    except Exception:
        return False


# ======================
# DIGIBOT STATUS
# ======================
_DIGI_CACHE = {}


async def digibot_check_active(uid: int) -> tuple[bool, str]:
    now = time.time()
    cached = _DIGI_CACHE.get(uid)
    if cached and cached[0] > now:
        return cached[1], cached[2]

    if not DIGIBOT_BASE_URL or not DIGIBOT_INTERNAL_API_KEY:
        return False, "unknown"

    url = f"{DIGIBOT_BASE_URL}/internal/status"
    headers = {
        "X-API-Key": DIGIBOT_INTERNAL_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {"tg_id": int(uid)}

    def _req():
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=6)
            return r.status_code, r.json()
        except Exception:
            return 0, None

    code, data = await asyncio.to_thread(_req)

    if code != 200 or not isinstance(data, dict) or not data.get("ok"):
        return False, "unknown"

    status = str(data.get("status") or "unknown")
    is_active = bool(data.get("is_active"))

    _DIGI_CACHE[uid] = (now + 30, is_active, status)
    return is_active, status


async def access_allowed(uid: int) -> bool:
    if uid in ADMINS:
        return True

    if has_override(uid):
        return True

    if trial_is_active(uid):
        return True

    is_active, _ = await digibot_check_active(uid)
    return bool(is_active)


# ======================
# MIDDLEWARE
# ======================
class DigiBotAccessMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        uid = message.from_user.id

        if message.text and message.text.startswith("/start"):
            return

        ok = await access_allowed(uid)
        if ok:
            return

        text = (
            "⛔ <b>Доступ запрещён</b>\n\n"
            "🎁 <b>Ваш бесплатный доступ закончился.</b>\n\n"
            "<b>Чтобы пользоваться ботом дальше — активируйте статус «Активный» в</b> @DigaroBot.\n\n"
            "👉 <b>Открой DigaroBot → Кабинет → получи статус «Активный».</b>"
        )
        await _p().answer_html(message, text, reply_markup=menu(uid))
        raise CancelHandler()

    async def on_pre_process_callback_query(self, call: types.CallbackQuery, data: dict):
        uid = call.from_user.id

        ok = await access_allowed(uid)
        if ok:
            return

        await call.answer("⛔ Доступ запрещён. Активируй «Активный» в DigaroBot.", show_alert=True)
        try:
            text = (
                "⛔ <b>Доступ запрещён</b>\n\n"
                "<b>Бесплатный доступ закончился.</b>\n\n"
                "<b>Чтобы продолжить — активируйте статус «Активный» в</b> @DigaroBot"
            )
            await _p().answer_html(call.message, text, reply_markup=menu(uid))
        except Exception:
            pass

        raise CancelHandler()


dp.middleware.setup(DigiBotAccessMiddleware())


# ======================
# UI
# ======================
def menu(uid: int | None = None):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔓 Подключить", "📝 Текст")
    kb.row("⚙️ Настройки", "👤 Личный кабинет")
    kb.row("📘 Для Новичка")
    kb.add("▶️ Начать работу")
    kb.add("⛔ Остановить")

    if uid is not None and uid in ADMINS:
        kb.add("🛡 Админ")

    return kb


def back_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⬅️ Назад")
    return kb


# ======================
# TELETHON CLIENT
# ======================
def create_custom_telegram_client(session_file: str):
    return TelegramClient(
        session_file,
        API_ID,
        API_HASH,
        device_model="Samsung Galaxy S21",
        system_version="Android 13",
        app_version="9.6.3",
        lang_code="ru",
        system_lang_code="ru"
    )


async def reset_login(uid: int):
    client = login_clients.get(uid)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
        login_clients.pop(uid, None)


# ======================
# STATES
# ======================
class TextState(StatesGroup):
    waiting = State()


class PhoneState(StatesGroup):
    phone = State()
    code = State()
    password = State()


class SettingsFSM(StatesGroup):
    delay_groups = State()
    groups_count = State()
    delay_cycle = State()


# ======================
# START
# ======================
@dp.message_handler(commands=["start"], state="*")
async def start(msg: types.Message, state):
    await state.finish()

    user = msg.from_user

    if not get_user_data(user.id):
        user_data = {
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "join_time": time.time(),
            "work_started": False,
            "accounts_connected_count": 0
        }
        save_user_data(user.id, user_data)

    get_tariff(user.id)

    text = (
        "👋 <b>Добро пожаловать в BlastBot</b>\n\n"
        "🚀 <b>Telegram-сервис для автоматической рассылки сообщений</b>\n"
        "<b>в чаты с нескольких аккаунтов.</b>\n\n"
        "🎁 <b>Бесплатный тест — 24 часа</b>\n"
        "<b>После 24 часов доступ будет только со статусом «Активный» в</b> @DigaroBot\n\n"
        "⬇️ Выберите действие ниже"
    )
    await _p().answer_html(msg, text, reply_markup=menu(user.id))


# ======================
# BACK
# ======================
@dp.message_handler(lambda m: m.text == "⬅️ Назад", state="*")
async def back(msg: types.Message, state):
    await reset_login(msg.from_user.id)
    await state.finish()
    await msg.answer("↩️ Возврат в меню", reply_markup=menu(msg.from_user.id))


# ======================
# GUIDE
# ======================
@dp.message_handler(lambda m: m.text == "📘 Для Новичка", state="*")
async def usage(msg: types.Message, state):
    await state.finish()

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(
            text="📖 Открыть инструкцию",
            url="https://digitalilservices.github.io/BlastBot/"
        )
    )

    text = (
        "📘 <b>Инструкция по использованию</b>\n\n"
        "Нажмите кнопку ниже, чтобы открыть полное руководство:"
    )
    await _p().answer_html(msg, text, reply_markup=kb)


# ======================
# ADMIN PANEL
# ======================
@dp.message_handler(lambda m: m.text == "🛡 Админ", state="*")
async def admin_panel(msg: types.Message, state):
    await state.finish()
    if msg.from_user.id not in ADMINS:
        return

    text = (
        "🛡 <b>Админ панель</b>\n\n"
        "✅ Выдать доступ:\n"
        "<code>/grant USER_ID</code> — навсегда\n"
        "<code>/grant USER_ID MINUTES</code> — на время\n\n"
        "⛔ Забрать доступ:\n"
        "<code>/revoke USER_ID</code>\n\n"
        "📋 Список:\n"
        "<code>/grants</code>\n\n"
        "ℹ️ Ручной доступ даёт вход в бота даже без статуса Active в DigaroBot."
    )
    await _p().answer_html(msg, text, reply_markup=menu(msg.from_user.id))


@dp.message_handler(lambda m: m.text and m.text.startswith("/grant"), state="*")
async def cmd_grant(msg: types.Message, state):
    await state.finish()
    if msg.from_user.id not in ADMINS:
        return

    parts = msg.text.split()
    if len(parts) < 2:
        await msg.answer("Формат: /grant USER_ID [MINUTES]", reply_markup=menu(msg.from_user.id))
        return

    try:
        uid = int(parts[1])
        minutes = int(parts[2]) if len(parts) >= 3 else None
    except Exception:
        await msg.answer("❌ Ошибка. Формат: /grant USER_ID [MINUTES]", reply_markup=menu(msg.from_user.id))
        return

    grant_access(uid, minutes=minutes)

    if minutes is None:
        await msg.answer(f"✅ Доступ выдан навсегда для {uid}", reply_markup=menu(msg.from_user.id))
    else:
        await msg.answer(f"✅ Доступ выдан на {minutes} минут для {uid}", reply_markup=menu(msg.from_user.id))

    try:
        await bot.send_message(uid, "✅ Вам выдан доступ к боту администратором.")
    except Exception:
        pass


@dp.message_handler(lambda m: m.text and m.text.startswith("/revoke"), state="*")
async def cmd_revoke(msg: types.Message, state):
    await state.finish()
    if msg.from_user.id not in ADMINS:
        return

    parts = msg.text.split()
    if len(parts) != 2:
        await msg.answer("Формат: /revoke USER_ID", reply_markup=menu(msg.from_user.id))
        return

    try:
        uid = int(parts[1])
    except Exception:
        await msg.answer("❌ USER_ID должен быть числом", reply_markup=menu(msg.from_user.id))
        return

    revoke_access(uid)
    await msg.answer(f"⛔ Доступ снят для {uid}", reply_markup=menu(msg.from_user.id))

    try:
        await bot.send_message(uid, "⛔ Ваш доступ к боту был отключён администратором.")
    except Exception:
        pass


@dp.message_handler(lambda m: m.text and m.text.startswith("/grants"), state="*")
async def cmd_grants(msg: types.Message, state):
    await state.finish()
    if msg.from_user.id not in ADMINS:
        return

    data = _load_overrides()
    if not data:
        await msg.answer("Пусто. Никому не выдан ручной доступ.", reply_markup=menu(msg.from_user.id))
        return

    now = int(time.time())
    lines = []
    for uid_str, row in data.items():
        exp = int(row.get("expires", 0))
        if exp == 0:
            lines.append(f"• {uid_str} — навсегда")
        else:
            left = exp - now
            if left <= 0:
                lines.append(f"• {uid_str} — истёк")
            else:
                lines.append(f"• {uid_str} — осталось ~{left // 60} мин")

    await msg.answer("📋 Ручной доступ:\n" + "\n".join(lines), reply_markup=menu(msg.from_user.id))


# ======================
# ACCOUNTS
# ======================
@dp.message_handler(lambda m: m.text == "🔓 Подключить", state="*")
async def add_account(msg: types.Message, state):
    tariff = get_tariff(msg.from_user.id)
    accounts = get_sessions(msg.from_user.id)

    if len(accounts) >= int(tariff.get("max_accounts", 999999999)):
        text = (
            "❌ Лимит аккаунтов достигнут\n\n"
            f"🔢 Максимум: {int(tariff.get('max_accounts', 999999999))} аккаунтов\n"
        )
        await _p().answer_html(msg, text, reply_markup=menu(msg.from_user.id))
        return

    await reset_login(msg.from_user.id)
    await state.finish()
    await msg.answer("📱 Введи номер телефона в формате +380XXXXXXXXX", reply_markup=back_kb())
    await PhoneState.phone.set()


@dp.message_handler(state=PhoneState.phone)
async def get_phone(msg: types.Message, state):
    text = (msg.text or "").strip()

    if not PHONE_RE.match(text):
        await msg.answer("❌ Неверный формат номера\nПример: +380XXXXXXXXX", reply_markup=back_kb())
        return

    phone = text
    path = user_dir(msg.from_user.id)
    session_file = str(path / "sessions" / phone)

    client = None
    try:
        await reset_login(msg.from_user.id)

        client = create_custom_telegram_client(session_file)
        await client.connect()
        await client.send_code_request(phone)

        login_clients[msg.from_user.id] = client
        await state.update_data(phone=phone)

        await msg.answer("🔐 Введи код из Telegram", reply_markup=back_kb())
        await PhoneState.code.set()

    except Exception as e:
        try:
            if client:
                await client.disconnect()
        except Exception:
            pass

        await msg.answer(f"❌ Не удалось отправить код: {e}", reply_markup=menu(msg.from_user.id))
        await state.finish()


@dp.message_handler(state=PhoneState.code)
async def get_code(msg: types.Message, state):
    uid = msg.from_user.id
    code = (msg.text or "").strip()

    if not code.isdigit():
        await msg.answer("❌ Код должен быть числом", reply_markup=back_kb())
        return

    data = await state.get_data()
    client = login_clients.get(uid)

    if not client:
        await msg.answer("❌ Сессия входа потеряна. Подключи аккаунт заново.", reply_markup=menu(uid))
        await state.finish()
        return

    try:
        await client.sign_in(phone=data["phone"], code=code)
        me = await client.get_me()

        accounts = get_accounts_info(uid)
        exists = any(acc.get("phone") == data["phone"] for acc in accounts)

        if not exists:
            accounts.append({
                "phone": data["phone"],
                "username": me.username or "no_username"
            })
            save_accounts_info(uid, accounts)

        user_data = get_user_data(uid)
        if user_data:
            user_data["accounts_connected_count"] = len(accounts)
            save_user_data(uid, user_data)

        await msg.answer("✅ Аккаунт успешно добавлен", reply_markup=menu(uid))

    except SessionPasswordNeededError:
        await msg.answer("🔑 На аккаунте включена 2FA. Введи пароль.", reply_markup=back_kb())
        await PhoneState.password.set()
        return

    except Exception as e:
        await msg.answer(f"❌ Ошибка входа: {e}", reply_markup=menu(uid))

    await reset_login(uid)
    await state.finish()


@dp.message_handler(state=PhoneState.password)
async def get_password(msg: types.Message, state):
    uid = msg.from_user.id
    client = login_clients.get(uid)

    if not client:
        await msg.answer("❌ Сессия входа потеряна. Подключи аккаунт заново.", reply_markup=menu(uid))
        await state.finish()
        return

    try:
        await client.sign_in(password=(msg.text or "").strip())
        data = await state.get_data()
        me = await client.get_me()

        accounts = get_accounts_info(uid)
        exists = any(acc.get("phone") == data["phone"] for acc in accounts)

        if not exists:
            accounts.append({
                "phone": data["phone"],
                "username": me.username or "no_username"
            })
            save_accounts_info(uid, accounts)

        user_data = get_user_data(uid)
        if user_data:
            user_data["accounts_connected_count"] = len(accounts)
            save_user_data(uid, user_data)

        await msg.answer("✅ Аккаунт добавлен (2FA)", reply_markup=menu(uid))

    except Exception as e:
        await msg.answer(f"❌ Ошибка 2FA: {e}", reply_markup=menu(uid))

    await reset_login(uid)
    await state.finish()


# ======================
# MESSAGE TEXT
# ======================
@dp.message_handler(lambda m: m.text == "📝 Текст", state="*")
async def text_message(msg: types.Message, state):
    await state.finish()
    await msg.answer("✍️ Отправь текст рассылки", reply_markup=back_kb())
    await TextState.waiting.set()


@dp.message_handler(state=TextState.waiting, content_types=types.ContentTypes.ANY)
async def save_text(msg: types.Message, state):
    path = user_dir(msg.from_user.id)

    if msg.forward_from_chat:
        if msg.forward_from_chat.type != "channel":
            await msg.answer("❌ Перешли сообщение ИМЕННО ИЗ КАНАЛА", reply_markup=menu(msg.from_user.id))
            await state.finish()
            return

        if not msg.forward_from_message_id:
            await msg.answer("❌ Не удалось получить ID пересланного сообщения", reply_markup=menu(msg.from_user.id))
            await state.finish()
            return

        data = {
            "type": "forward",
            "from_chat_id": msg.forward_from_chat.id,
            "message_id": msg.forward_from_message_id
        }
    else:
        text_value = (msg.text or msg.caption or "").strip()
        if not text_value:
            await msg.answer("❌ Пустой текст нельзя сохранить", reply_markup=menu(msg.from_user.id))
            await state.finish()
            return

        data = {
            "type": "copy",
            "text": text_value
        }

    with open(path / "message.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    await msg.answer("✅ Сообщение сохранено", reply_markup=menu(msg.from_user.id))
    await state.finish()


# ======================
# SETTINGS
# ======================
@dp.message_handler(lambda m: m.text == "⚙️ Настройки", state="*")
async def settings_start(msg: types.Message, state):
    await state.finish()
    await msg.answer("⏱ Введите задержку между отправкой в группах (сек):", reply_markup=back_kb())
    await SettingsFSM.delay_groups.set()


@dp.message_handler(state=SettingsFSM.delay_groups)
async def set_delay_groups(msg: types.Message, state):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.answer("❌ Нужно число", reply_markup=back_kb())
        return

    await state.update_data(delay_between_groups=int(text))
    await msg.answer("👥 Сколько групп брать с одного аккаунта?", reply_markup=back_kb())
    await SettingsFSM.groups_count.set()


@dp.message_handler(state=SettingsFSM.groups_count)
async def set_groups(msg: types.Message, state):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.answer("❌ Нужно число", reply_markup=back_kb())
        return

    await state.update_data(groups_per_account=int(text))
    await msg.answer("⏳ Задержка после всех аккаунтов (минуты):", reply_markup=back_kb())
    await SettingsFSM.delay_cycle.set()


@dp.message_handler(state=SettingsFSM.delay_cycle)
async def set_cycle(msg: types.Message, state):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.answer("❌ Нужно число", reply_markup=back_kb())
        return

    data = await state.get_data()
    path = user_dir(msg.from_user.id)

    settings = {
        "delay_between_groups": int(data["delay_between_groups"]),
        "groups_per_account": int(data["groups_per_account"]),
        "delay_between_cycles": int(text) * 60
    }

    with open(path / "settings.json", "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    await msg.answer("✅ Настройки сохранены", reply_markup=menu(msg.from_user.id))
    await state.finish()


# ======================
# CABINET
# ======================
@dp.message_handler(lambda m: m.text == "👤 Личный кабинет", state="*")
async def cabinet(msg: types.Message, state):
    await state.finish()

    uid = msg.from_user.id
    accounts = get_accounts_info(uid)
    tariff = get_tariff(uid)
    text_msg = get_user_text(uid)
    settings = get_settings(uid)

    is_active, status = await digibot_check_active(uid)
    status_norm = (status or "").strip().lower()
    has_paid_access = bool(is_active) or status_norm in ("active", "leader", "активный", "лидер")
    has_manual = has_override(uid)

    text = "👤 <b>Личный кабинет</b>\n\n"
    text += f"🔢 Аккаунтов подключено: <b>{len(accounts)}</b>\n"

    if not accounts:
        text += "❌ Аккаунты не подключены\n"
    else:
        text += "📱 Подключённые аккаунты:\n"
        for i, acc in enumerate(accounts, 1):
            phone = acc.get("phone", "-")
            username = acc.get("username", "-")
            text += f"• {i}. <b>{phone}</b> — @{username}\n"

    text += "\n"

    if has_paid_access:
        text += "✅ <b>Статус Активный</b>\n\n"
    elif has_manual:
        text += "✅ <b>Доступ выдан админом</b>\n\n"
    else:
        text += "🎁 <b>Бесплатный доступ:</b>\n"
        if tariff.get("expires"):
            seconds_left = float(tariff["expires"]) - time.time()
            if seconds_left > 0:
                hours_left = int(seconds_left / 3600)
                minutes_left = int((seconds_left % 3600) / 60)
                text += f"⏳ Осталось: <b>{hours_left} ч. {minutes_left} мин.</b>\n"
            else:
                text += "⏳ <b>Срок истёк</b>\n"
                text += "✅ Дальше доступ только со статусом <b>Активный</b> в @DigaroBot\n"
        text += "\n"

    text += "📄 <b>Текст рассылки:</b>\n"
    if text_msg:
        preview = text_msg[:300]
        text += f"{preview}\n"
        if len(text_msg) > 300:
            text += "…\n"
    else:
        text += "❌ Текст не задан\n"
    text += "\n"

    text += "⚙️ <b>Настройки:</b>\n"
    if settings:
        text += (
            f"• ⏱ Задержка между группами: <b>{settings['delay_between_groups']} сек</b>\n"
            f"• 👥 Групп с аккаунта: <b>{settings['groups_per_account']}</b>\n"
            f"• 🔁 Пауза между циклами: <b>{settings['delay_between_cycles'] // 60} мин</b>\n"
        )
    else:
        text += "❌ Настройки не заданы\n"

    text += (
        "\n❌ Удаление аккаунта:\n"
        "<code>del 1</code> - удалить нужный аккаунт 1,2,3...\n"
        "<code>del all</code> - удалить все аккаунты полностью"
    )

    await _p().answer_html(msg, text, reply_markup=menu(uid))


@dp.message_handler(lambda m: m.text and m.text.startswith("del"), state="*")
async def delete_account(msg: types.Message, state):
    await state.finish()

    uid = msg.from_user.id
    parts = msg.text.split()

    path = user_dir(uid)
    sessions_dir = path / "sessions"
    accounts_file = path / "accounts.json"

    if not accounts_file.exists():
        await msg.answer("❌ Аккаунтов нет", reply_markup=menu(uid))
        return

    with open(accounts_file, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    if len(parts) < 2:
        await msg.answer("❌ Укажи номер аккаунта\nПример: del 1", reply_markup=menu(uid))
        return

    arg = parts[1].lower()

    if arg == "all":
        for file in os.listdir(sessions_dir):
            if file.endswith(".session"):
                try:
                    os.remove(sessions_dir / file)
                except Exception:
                    pass

        with open(accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2, ensure_ascii=False)

        user_data = get_user_data(uid)
        if user_data:
            user_data["accounts_connected_count"] = 0
            save_user_data(uid, user_data)

        await msg.answer("✅ Все аккаунты удалены", reply_markup=menu(uid))
        return

    if not arg.isdigit():
        await msg.answer("❌ Неверный номер", reply_markup=menu(uid))
        return

    index = int(arg) - 1

    if index < 0 or index >= len(accounts):
        await msg.answer("❌ Такого аккаунта нет", reply_markup=menu(uid))
        return

    phone = accounts[index]["phone"]

    try:
        os.remove(sessions_dir / f"{phone}.session")
    except Exception:
        pass

    accounts.pop(index)

    with open(accounts_file, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)

    user_data = get_user_data(uid)
    if user_data:
        user_data["accounts_connected_count"] = len(accounts)
        save_user_data(uid, user_data)

    await msg.answer("✅ Аккаунт удалён", reply_markup=menu(uid))


# ======================
# START / STOP
# ======================
@dp.message_handler(lambda m: m.text == "▶️ Начать работу", state="*")
async def start_work(msg: types.Message, state):
    await state.finish()
    uid = msg.from_user.id
    path = user_dir(uid)

    if uid in workers and not workers[uid]["stop"]:
        await msg.answer("⚠️ Рассылка уже запущена", reply_markup=menu(uid))
        return

    accounts = get_accounts_info(uid)
    if not accounts:
        await msg.answer("❌ Нет подключённых аккаунтов", reply_markup=menu(uid))
        return

    if not (path / "message.json").exists():
        await msg.answer("❌ Нет текста", reply_markup=menu(uid))
        return

    if not (path / "settings.json").exists():
        await msg.answer("❌ Нет настроек", reply_markup=menu(uid))
        return

    try:
        with open(path / "message.json", "r", encoding="utf-8") as f:
            message_data = json.load(f)

        if message_data.get("type") == "copy" and not (message_data.get("text") or "").strip():
            await msg.answer("❌ Текст сообщения пустой", reply_markup=menu(uid))
            return
    except Exception:
        await msg.answer("❌ Ошибка чтения message.json", reply_markup=menu(uid))
        return

    user_data = get_user_data(uid)
    if user_data:
        user_data["work_started"] = True
        if not user_data.get("trial_start_time"):
            user_data["trial_start_time"] = time.time()
        save_user_data(uid, user_data)

    if uid in workers:
        workers.pop(uid, None)

    stop_flag = {"stop": False, "logs": []}
    workers[uid] = stop_flag

    status = await msg.answer("🚀 Рассылка запущена\n📤 Отправлено: 0")

    async def progress(sent, errors, info=None):
        try:
            if isinstance(info, dict):
                phone = info.get("phone")
                if phone and phone not in [l.get("phone") for l in workers[uid]["logs"]]:
                    workers[uid]["logs"].append(info)

            logs_text = ""
            if workers[uid]["logs"]:
                lines = []
                for i, log in enumerate(workers[uid]["logs"], 1):
                    reason = log.get("reason", "error")

                    emoji = {
                        "spam_block": "🚫 СПАМ-БЛОК",
                        "freeze": "❄️ ЗАМОРОЖЕН",
                        "dead": "❌ МЁРТВЫЙ",
                        "error": "⚠️ ОШИБКА",
                        "not_authorized": "🔐 НЕ АВТОРИЗОВАН",
                        "no_write_permission": "🚫 НЕТ ПРАВ",
                    }.get(reason, "❓ ПРОБЛЕМА")

                    lines.append(f"{i}. {emoji} — <b>{log.get('phone', '-')}</b>")

                logs_text = (
                    "\n\n🧾 <b>Проблемные аккаунты:</b>\n"
                    + "\n".join(lines) +
                    "\n\n<i>👉 Зайдите в личный кабинет и удалите проблемный аккаунт</i>"
                )

            text_ = (
                "🚀 <b>Рассылка запущена</b>\n"
                f"📤 Отправлено: <b>{sent}</b>\n"
                f"❌ Ошибки: <b>{errors}</b>"
                f"{logs_text}"
            )

            await _p().edit_html(status, text_)
        except Exception:
            pass

    task = asyncio.create_task(spam_worker(str(path), stop_flag, progress))
    workers[uid]["task"] = task


@dp.message_handler(lambda m: m.text == "⛔ Остановить", state="*")
async def stop(msg: types.Message, state):
    await state.finish()
    uid = msg.from_user.id

    if uid in workers:
        workers[uid]["stop"] = True
        await msg.answer("⛔ Рассылка остановлена", reply_markup=menu(uid))
    else:
        await msg.answer("ℹ️ Рассылка сейчас не запущена", reply_markup=menu(uid))


# ======================
# STARTUP
# ======================
async def on_startup(_dp: Dispatcher):
    global premium
    try:
        if PREMIUM_STICKER_SETS:
            premium = await PremiumEmoji.from_sticker_sets(bot, PREMIUM_STICKER_SETS)
            print(f"PremiumEmoji loaded: {len(premium.emoji_map)} emoji", flush=True)
        else:
            premium = PremiumEmoji(emoji_map={})
            print("PremiumEmoji: no sticker sets configured", flush=True)
    except Exception as e:
        premium = PremiumEmoji(emoji_map={})
        print(f"PremiumEmoji load failed: {e}", flush=True)


# ======================
# RUN
# ======================
if __name__ == "__main__":
    print("=== START POLLING ===", flush=True)

    try:
        executor.start_polling(
            dp,
            skip_updates=True,
            on_startup=on_startup
        )
    except Exception as e:
        import traceback
        print("FATAL ERROR:", e, flush=True)
        traceback.print_exc()
        time.sleep(60)
