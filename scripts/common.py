"""Shared helpers for the newsletter scripts."""

import html
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
CONFIG = ROOT / "config"

SHORTLIST_FILE = DATA / "shortlist.json"
HISTORY_FILE = DATA / "history.json"

# Friendly names for common sources. Anything not listed shows its web address.
SOURCE_NAMES = {
    "bbc.co.uk": "BBC",
    "bbc.com": "BBC",
    "theguardian.com": "Guardian",
    "news.sky.com": "Sky News",
    "cityam.com": "City AM",
    "capx.co": "CapX",
    "gov.uk": "GOV.UK",
    "independent.co.uk": "Independent",
    "telegraph.co.uk": "Telegraph",
    "spectator.co.uk": "Spectator",
    "inews.co.uk": "i",
    "positive.news": "Positive News",
    "thetimes.com": "Times",
    "thetimes.co.uk": "Times",
    "ft.com": "FT",
}

# Sources that usually sit behind a paywall. Claude is told to prefer free ones.
PAYWALLED = {"telegraph.co.uk", "thetimes.com", "thetimes.co.uk", "ft.com", "spectator.co.uk"}


def setting(name, default=None, required=False):
    """Read a GitHub secret or variable passed in as an environment variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            sys.exit(
                f"Missing setting: {name}. Add it in GitHub under "
                "Settings, Secrets and variables, Actions."
            )
        return default
    return value


def domain_of(url):
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def source_for(url):
    host = domain_of(url)
    for known, name in SOURCE_NAMES.items():
        if host == known or host.endswith("." + known):
            return name
    return host


def is_paywalled(url):
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in PAYWALLED)


def bare_url(url):
    """The address without tracking codes, used to spot repeats."""
    return url.split("?")[0].split("#")[0].rstrip("/")


def esc(text):
    return html.escape(text or "", quote=True)


def nice_date(d):
    """6 August 2026"""
    return f"{d.day} {d.strftime('%B %Y')}"


def nice_time(dt):
    """8 a.m. or 8:30 a.m."""
    hour = dt.hour % 12 or 12
    suffix = "a.m." if dt.hour < 12 else "p.m."
    if dt.minute:
        return f"{hour}:{dt.minute:02d} {suffix}"
    return f"{hour} {suffix}"


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------- Telegram ----------

def tg_call(method, payload=None):
    token = setting("TELEGRAM_BOT_TOKEN", required=True)
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", json=payload or {}, timeout=30
    )
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description', data)}")
    return data["result"]


def tg_send(text):
    """Send a message to you. Long messages are split, as Telegram caps them at 4,096 characters."""
    chat_id = setting("TELEGRAM_CHAT_ID", required=True)
    chunks, current = [], ""
    for part in text.split("\n\n"):
        if current and len(current) + len(part) + 2 > 3900:
            chunks.append(current)
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part
    if current:
        chunks.append(current)
    for chunk in chunks:
        tg_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )


def tg_updates():
    """Messages sent to the bot that have not yet been cleared. Telegram keeps them for 24 hours."""
    return tg_call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})


def tg_clear(updates):
    """Mark messages as read so they are not picked up again."""
    if updates:
        tg_call("getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0})
