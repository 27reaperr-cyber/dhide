"""
dHide — Telegram bot for lawful people-lookups.

Enhanced Telegram probe: fetches t.me page to extract name, bio, profile photo.
Mock registration data added for demonstration.
"""

import asyncio
import os
import re
from enum import Enum
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from bs4 import BeautifulSoup

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
        async with session.get(url, params=params, timeout=12) as resp:
            data = await resp.json()
    except Exception as exc:
        return f"Numverify error: {exc}"

    if not data.get("valid"):
        return "Number not valid or not found."

    carrier = data.get("carrier") or "n/a"
    country = f'{data.get("country_name", "n/a")} ({data.get("country_code", "")})'
    line = data.get("line_type") or "n/a"
    location = data.get("location") or "n/a"
    local = data.get("local_format") or phone
    international = data.get("international_format") or phone
    prefix = data.get("country_prefix") or "n/a"

    return (
        "✓ Valid number\n"
        f"Country: {country} (prefix +{prefix})\n"
        f"Location: {location}\n"
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
        "extratags": 1,
        "namedetails": 1,
        "limit": 3,
    }
    try:
        async with session.get(url, params=params, headers=headers, timeout=12) as resp:
            data = await resp.json()
    except Exception as exc:
        return f"Nominatim error: {exc}"

    if not data:
        return "No address matches found."

    lines: List[str] = []
    for idx, item in enumerate(data, 1):
        name = item.get("display_name", "—")
        lat = item.get("lat", "?")
        lon = item.get("lon", "?")
        bbox = item.get("boundingbox")
        bbox_str = f"[{bbox[0]}, {bbox[1]}; {bbox[2]}, {bbox[3]}]" if bbox else "n/a"
        address_details = item.get("address", {})
        comps = []
        for key in ["road", "house_number", "city", "town", "village", "postcode", "country"]:
            if val := address_details.get(key):
                comps.append(f"{key.replace('_', ' ').title()}: {val}")
        comp_str = "\n      ".join(comps) if comps else "—"

        lines.append(
            f"📍 Result #{idx}\n"
            f"Name: {name}\n"
            f"Coordinates: {lat}, {lon}\n"
            f"Bounding box: {bbox_str}\n"
            f"Address details:\n      {comp_str}"
        )

    return "Top matches:\n\n" + "\n\n".join(lines)


# --- Social lookup (multi‑platform) ---------------------------------------


class Platform:
    def __init__(self, name: str, url_pattern: str, check_url: Optional[str] = None):
        self.name = name
        self.url_pattern = url_pattern  # with {} placeholder for username
        self.check_url = check_url or url_pattern  # URL to probe (may differ from public profile)

    async def check(self, session: aiohttp.ClientSession, username: str) -> Tuple[bool, str, Optional[Dict]]:
        """Return (exists, profile_url, extra_info)"""
        url = self.check_url.format(username)
        profile_url = self.url_pattern.format(username)
        try:
            async with session.head(url, allow_redirects=True, timeout=8) as resp:
                exists = resp.status < 400
                return exists, profile_url, None
        except Exception:
            return False, profile_url, None


class TelegramPlatform(Platform):
    def __init__(self):
        super().__init__("Telegram", "https://t.me/{}")

    async def check(self, session: aiohttp.ClientSession, username: str) -> Tuple[bool, str, Optional[Dict]]:
        url = self.check_url.format(username)
        profile_url = self.url_pattern.format(username)
        try:
            async with session.get(url, timeout=8) as resp:
                if resp.status != 200:
                    return False, profile_url, None
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')

                # Extract name (from <meta property="og:title"> or <title>)
                name_tag = soup.find("meta", property="og:title")
                name = name_tag.get("content") if name_tag else soup.title.string if soup.title else username

                # Extract bio (from <meta property="og:description">)
                bio_tag = soup.find("meta", property="og:description")
                bio = bio_tag.get("content") if bio_tag else None

                # Extract profile photo (from <meta property="og:image">)
                photo_tag = soup.find("meta", property="og:image")
                photo_url = photo_tag.get("content") if photo_tag else None

                extra = {
                    "name": name,
                    "bio": bio,
                    "photo_url": photo_url,
                }
                return True, profile_url, extra
        except Exception:
            return False, profile_url, None


class InstagramPlatform(Platform):
    def __init__(self):
        super().__init__("Instagram", "https://instagram.com/{}", "https://instagram.com/{}/?__a=1")

    async def check(self, session: aiohttp.ClientSession, username: str) -> Tuple[bool, str, Optional[Dict]]:
        url = self.check_url.format(username)
        profile_url = self.url_pattern.format(username)
        try:
            async with session.get(url, timeout=8) as resp:
                if resp.status != 200:
                    return False, profile_url, None
                data = await resp.json()
                user = data.get("graphql", {}).get("user", {})
                if not user:
                    return False, profile_url, None
                extra = {
                    "full_name": user.get("full_name"),
                    "biography": user.get("biography"),
                    "followers": user.get("edge_followed_by", {}).get("count"),
                    "following": user.get("edge_follow", {}).get("count"),
                    "private": user.get("is_private"),
                    "verified": user.get("is_verified"),
                }
                return True, profile_url, extra
        except Exception:
            return False, profile_url, None


# Define all platforms we want to check
PLATFORMS: List[Platform] = [
    TelegramPlatform(),
    InstagramPlatform(),
    Platform("Twitter", "https://twitter.com/{}"),
    Platform("VK", "https://vk.com/{}"),
    Platform("Facebook", "https://facebook.com/{}"),
]


def extract_username_from_query(query: str) -> str:
    """Try to extract a username from various social URLs or plain text."""
    q = query.strip().lower()
    patterns = [
        r"(?:t\.me/|@)([a-z0-9_]{5,})",
        r"(?:instagram\.com/)([a-z0-9_.]+)",
        r"(?:twitter\.com/)([a-z0-9_]+)",
        r"(?:vk\.com/)([a-z0-9_.]+)",
        r"(?:facebook\.com/)([a-z0-9_.]+)",
    ]
    for pat in patterns:
        match = re.search(pat, q, re.IGNORECASE)
        if match:
            return match.group(1)
    if re.match(r"^[a-zA-Z0-9_]{3,}$", q):
        return q
    return ""


async def lookup_social(session: aiohttp.ClientSession, query: str) -> str:
    username = extract_username_from_query(query)
    if not username:
        return "Could not extract a valid username from your input. Please send a username (e.g., @durov) or a profile link."

    lines = [f"🔎 Searching for <b>{username}</b> across platforms:\n"]
    for platform in PLATFORMS:
        exists, profile_url, extra = await platform.check(session, username)
        if exists:
            line = f"✅ <b>{platform.name}</b>: <a href='{profile_url}'>profile</a>"
            if extra:
                if platform.name == "Telegram":
                    details = []
                    if extra.get("name"):
                        details.append(f"Name: {extra['name']}")
                    if extra.get("bio"):
                        bio = extra['bio'][:200] + ("…" if len(extra['bio']) > 200 else "")
                        details.append(f"Bio: {bio}")
                    if extra.get("photo_url"):
                        details.append(f"🖼️ <a href='{extra['photo_url']}'>Profile photo</a>")
                    # Mock registration data with disclaimer
                    details.append("\n⚠️ <i>Registration data below is for demonstration only and not publicly available.</i>")
                    details.append("📞 Registration phone: +7 (***) ***-**-** (example)")
                    details.append("📅 Registration date: 2023-05-15 (example)")
                    if details:
                        line += "\n      " + "\n      ".join(details)
                elif platform.name == "Instagram" and extra:
                    details = []
                    if extra.get("full_name"):
                        details.append(f"Name: {extra['full_name']}")
                    if extra.get("biography"):
                        bio = extra['biography'][:100] + ("…" if len(extra['biography']) > 100 else "")
                        details.append(f"Bio: {bio}")
                    if extra.get("followers") is not None:
                        details.append(f"👥 {extra['followers']} followers, {extra['following']} following")
                    if extra.get("private"):
                        details.append("🔒 Private account")
                    if extra.get("verified"):
                        details.append("✅ Verified")
                    if details:
                        line += "\n      " + "\n      ".join(details)
            lines.append(line)
        else:
            lines.append(f"❌ {platform.name}: not found")

    return "\n".join(lines)


# --- Helper to detect lookup kind ------------------------------------------


def detect_kind(query: str) -> LookupKind:
    digits = "".join(ch for ch in query if ch.isdigit())
    if len(digits) >= 10:
        return LookupKind.PHONE

    social_patterns = [
        r"t\.me/", r"@\w", r"instagram\.com/", r"twitter\.com/", r"vk\.com/", r"facebook\.com/",
    ]
    if any(re.search(p, query.lower()) for p in social_patterns):
        return LookupKind.SOCIAL

    return LookupKind.ADDRESS


# --- Dispatcher -----------------------------------------------------------

dp = Dispatcher()


def kind_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text="📞 Телефон", callback_data=LookupKind.PHONE.value),
        InlineKeyboardButton(text="🏠 Адрес", callback_data=LookupKind.ADDRESS.value),
        InlineKeyboardButton(text="🌐 Соцсети (Telegram, Instagram, …)", callback_data=LookupKind.SOCIAL.value),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "dHide — поиск сведений для юридических целей.\n"
        "Выберите тип запроса или отправьте /find &lt;данные&gt;.",
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


@dp.callback_query(F.data.in_({k.value for k in LookupKind}))
async def on_kind_choice(callback: CallbackQuery) -> None:
    kind = LookupKind(callback.data)
    await callback.message.answer(
        f"Отправьте {format_prompt(kind)}.\n"
        "Можно также использовать команду /find &lt;данные&gt;."
    )
    await callback.answer()


def format_prompt(kind: LookupKind) -> str:
    mapping = {
        LookupKind.PHONE: "номер телефона (+7XXXXXXXXXX)",
        LookupKind.ADDRESS: "полный адрес",
        LookupKind.SOCIAL: "username или ссылку на профиль (Telegram, Instagram, Twitter, VK, Facebook)",
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
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())