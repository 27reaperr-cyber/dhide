"""
dHide — Telegram bot for lawful people-lookups.

Public APIs chosen:
- Phone: Numverify (free tier, requires NUMVERIFY_KEY).
- Address: OpenStreetMap Nominatim (no key, requires User-Agent).
- Social: Telegram probe via t.me (no key; checks public profile existence).

Environment variables:
- BOT_TOKEN       : Telegram Bot API token.
- NUMVERIFY_KEY   : API key for https://numverify.com/ (free tier works).
- NOMINATIM_UA    : Optional User-Agent for Nominatim; default "dHide/1.0".

Usage:
  pip install -r requirements.txt
  set BOT_TOKEN=...
  set NUMVERIFY_KEY=...
  python bot.py
"""

import asyncio
import os
import re
from enum import Enum
from typing import List

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.client.default import DefaultBotProperties

# --- Config ---------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN env var with your Telegram Bot API token.")

NUMVERIFY_KEY = os.getenv("NUMVERIFY_KEY")
NOMINATIM_UA = os.getenv("NOMINATIM_UA", "dHide/1.0")

# --- Domain ---------------------------------------------------------------


class LookupKind(str, Enum):
    PHONE = "phone"
    ADDRESS = "address"
    SOCIAL = "social"


# --- External lookups -----------------------------------------------------


async def lookup_phone(session: aiohttp.ClientSession, phone: str) -> str:
    if not NUMVERIFY_KEY:
        return "Phone lookup requires NUMVERIFY_KEY (https://numverify.com/)."

    url = "http://apilayer.net/api/validate"
    params = {
        "access_key": NUMVERIFY_KEY,
        "number": phone,
        "format": 1,
    }
    try:
        resp = await session.get(url, params=params, timeout=12)
        data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        return f"Numverify error: {exc}"

    if not data.get("valid"):
        return "Number not valid or not found."

    carrier = data.get("carrier") or "n/a"
    country = f'{data.get("country_name", "n/a")} ({data.get("country_code", "")})'
    line = data.get("line_type") or "n/a"
    local = data.get("local_format") or phone
    international = data.get("international_format") or phone

    return (
        "✓ Valid number\n"
        f"Country: {country}\n"
        f"Carrier: {carrier}\n"
        f"Line type: {line}\n"
        f"Local: {local}\n"
        f"International: {international}"
    )


async def lookup_address(session: aiohttp.ClientSession, address: str) -> str:
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": NOMINATIM_UA}
    params = {
        "q": address,
        "format": "json",
        "addressdetails": 1,
        "limit": 3,
    }
    try:
        resp = await session.get(url, params=params, headers=headers, timeout=12)
        data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        return f"Nominatim error: {exc}"

    if not data:
        return "No address matches found."

    lines: List[str] = []
    for item in data:
        name = item.get("display_name", "—")
        lat = item.get("lat", "?")
        lon = item.get("lon", "?")
        lines.append(f"{name}\nGeo: {lat}, {lon}")

    return "Top matches:\n\n" + "\n\n".join(lines)


def _looks_tg(query: str) -> bool:
    q = query.lower()
    return q.startswith("@") or "t.me" in q or "telegram" in q


async def lookup_social(session: aiohttp.ClientSession, handle: str) -> str:
    probe = await _telegram_probe(session, handle)
    return probe or "No social data."


async def _telegram_probe(session: aiohttp.ClientSession, query: str) -> str:
    handle = _extract_tg_handle(query)
    if not handle:
        return ""
    url = f"https://t.me/{handle}"
    try:
        resp = await session.head(url, allow_redirects=True, timeout=8)
        ok = resp.status < 400
    except Exception as exc:  # noqa: BLE001
        return f"Telegram probe error: {exc}"
    status = "exists (public profile/page found)" if ok else "not found"
    return f"Telegram @{handle}: {status}"


def _extract_tg_handle(raw: str) -> str:
    m = re.search(r"(?:t\\.me/|@)([A-Za-z0-9_]{5,})", raw)
    if m:
        return m.group(1)
    return raw[1:] if raw.startswith("@") else ""


# --- Dispatcher -----------------------------------------------------------

dp = Dispatcher()


def kind_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text="📞 Телефон", callback_data=LookupKind.PHONE.value),
        InlineKeyboardButton(text="🏠 Адрес", callback_data=LookupKind.ADDRESS.value),
        InlineKeyboardButton(text="🌐 Соцсети", callback_data=LookupKind.SOCIAL.value),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "dHide — поиск сведений для юридических целей.\n"
        "Выберите тип запроса или отправьте /find <данные>.",
        reply_markup=kind_keyboard(),
    )


@dp.message(Command("find"))
async def cmd_find(message: Message) -> None:
    if not message.text:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /find <телефон|адрес|соцсеть>")
        return

    query = parts[1].strip()
    kind = detect_kind(query)
    await message.reply("Ищу данные, подождите…")
    result = await perform_lookup(kind, query)
    await message.answer(result, parse_mode=ParseMode.HTML)


def detect_kind(query: str) -> LookupKind:
    digits = "".join(ch for ch in query if ch.isdigit())
    if len(digits) >= 10:
        return LookupKind.PHONE
    if _looks_tg(query):
        return LookupKind.SOCIAL
    return LookupKind.ADDRESS


@dp.callback_query(F.data.in_({k.value for k in LookupKind}))
async def on_kind_choice(callback: CallbackQuery) -> None:
    kind = LookupKind(callback.data)
    await callback.message.answer(
        f"Отправьте {format_prompt(kind)}.\n"
        "Можно также использовать команду /find <данные>."
    )
    await callback.answer()


def format_prompt(kind: LookupKind) -> str:
    mapping = {
        LookupKind.PHONE: "номер телефона (+7XXXXXXXXXX)",
        LookupKind.ADDRESS: "полный адрес",
        LookupKind.SOCIAL: "ссылку или @username",
    }
    return mapping[kind]


@dp.message()
async def on_free_text(message: Message) -> None:
    query = message.text or ""
    kind = detect_kind(query)
    await message.reply("Ищу данные, подождите…")
    result = await perform_lookup(kind, query)
    await message.answer(result, parse_mode=ParseMode.HTML)


async def perform_lookup(kind: LookupKind, query: str) -> str:
    async with aiohttp.ClientSession() as session:
        if kind is LookupKind.PHONE:
            return await lookup_phone(session, query)
        if kind is LookupKind.ADDRESS:
            return await lookup_address(session, query)
        return await lookup_social(session, query)


async def main() -> None:
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Ensure no leftover webhook/poller conflicts before starting long polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
