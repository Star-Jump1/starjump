"""Evening job: gather the last day's stories, ask Claude for the best 10, send them to Telegram."""

import calendar
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests

from common import (
    CONFIG,
    HISTORY_FILE,
    SHORTLIST_FILE,
    bare_url,
    esc,
    is_paywalled,
    load_json,
    nice_date,
    save_json,
    setting,
    source_for,
    tg_clear,
    tg_send,
    tg_updates,
)

MAX_AGE_HOURS = 30   # ignore stories older than this
MAX_STORIES = 300    # cap on how many stories go to Claude, to keep the cost down
SHORTLIST_SIZE = 10
USER_AGENT = "Mozilla/5.0 (compatible; StarjumpShortlist/1.0)"

SYSTEM_PROMPT = """You shortlist stories for Starjump, a daily British good-news email. Follow the editor's rubric below exactly.

{rubric}

You will get a numbered list of stories from the last day. Return JSON only, with no other text, in this shape:
{{"picks": [{{"id": 12, "headline": "Rewritten headline", "why": "Under 15 words on why it passes"}}]}}

Pick {n} stories, best first. If fewer than {n} pass the rubric, return only those that pass."""


def read_feeds():
    lines = (CONFIG / "feeds.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def gather():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    already_sent = {item["url"] for item in load_json(HISTORY_FILE, [])}
    stories, seen_links, seen_titles, failed = [], set(), set(), []

    for feed_url in read_feeds():
        try:
            response = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as error:  # one broken feed should not stop the rest
            failed.append(f"{feed_url} ({error.__class__.__name__})")
            continue

        for entry in feed.entries:
            link = (entry.get("link") or "").strip()
            title = clean(entry.get("title"))
            if not link or not title:
                continue
            key = bare_url(link)
            title_key = re.sub(r"\W+", " ", title.lower()).strip()
            if key in seen_links or title_key in seen_titles or key in already_sent:
                continue
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            when = datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc) if stamp else None
            if when and when < cutoff:
                continue
            seen_links.add(key)
            seen_titles.add(title_key)
            stories.append(
                {
                    "title": title,
                    "url": link,
                    "source": source_for(link),
                    "paywall": is_paywalled(link),
                    "summary": clean(entry.get("summary"))[:280],
                    "when": when.isoformat() if when else "",
                }
            )

    stories.sort(key=lambda s: s["when"], reverse=True)
    return stories[:MAX_STORIES], failed


def ask_claude(stories):
    rubric = (CONFIG / "rubric.md").read_text(encoding="utf-8")
    listing = "\n".join(
        f"[{i}] {s['title']} | {s['source']}{' (paywall)' if s['paywall'] else ''} | {s['summary']}"
        for i, s in enumerate(stories)
    )
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    message = client.messages.create(
        model=setting("CLAUDE_MODEL", "claude-sonnet-5-5"),
        max_tokens=4000,
        system=SYSTEM_PROMPT.format(rubric=rubric, n=SHORTLIST_SIZE),
        messages=[{"role": "user", "content": listing}],
    )
    text = "".join(block.text for block in message.content if block.type == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"Claude did not return JSON. It said: {text[:300]}")
    picks = json.loads(text[start : end + 1]).get("picks", [])

    shortlist = []
    for pick in picks:
        index = pick.get("id")
        if isinstance(index, int) and 0 <= index < len(stories):
            story = stories[index]
            shortlist.append(
                {
                    "headline": (pick.get("headline") or story["title"]).strip(),
                    "original": story["title"],
                    "url": story["url"],
                    "source": story["source"],
                    "paywall": story["paywall"],
                    "why": (pick.get("why") or "").strip(),
                }
            )
    return shortlist[:SHORTLIST_SIZE]


def telegram_text(shortlist, failed):
    # An evening run is for tomorrow's issue; a morning test run is for today's.
    now = datetime.now(ZoneInfo("Europe/London"))
    issue_day = now + timedelta(days=1) if now.hour >= 12 else now
    lines = [
        f"<b>Shortlist of {len(shortlist)}</b> for {nice_date(issue_day)}",
        "Reply with the numbers you want, in order, for example <code>3 1 7 4 9</code>\n"
        "A preview of the email follows within about 15 minutes. You can edit it until about 5 a.m.\n"
        "Reply <code>skip</code> to send nothing.",
    ]
    for number, item in enumerate(shortlist, start=1):
        paywall = " (paywall)" if item["paywall"] else ""
        lines.append(
            f"{number}. <a href=\"{esc(item['url'])}\">{esc(item['headline'])}</a>"
            f" ({esc(item['source'])}{paywall})\n<i>{esc(item['why'])}</i>"
        )
    if failed:
        lines.append("Feeds that failed tonight:\n" + "\n".join(esc(f) for f in failed))
    return "\n\n".join(lines)


def main():
    # Clear old messages so the morning job only sees tonight's reply.
    tg_clear(tg_updates())

    stories, failed = gather()
    print(f"Gathered {len(stories)} stories. {len(failed)} feeds failed.")
    for f in failed:
        print("  failed:", f)

    if not stories:
        tg_send("No stories came in from the feeds tonight, so there is no shortlist.")
        return

    shortlist = ask_claude(stories)
    print(f"Claude picked {len(shortlist)}.")

    save_json(
        SHORTLIST_FILE,
        {"created": int(time.time()), "created_iso": datetime.now(timezone.utc).isoformat(), "items": shortlist},
    )
    tg_send(telegram_text(shortlist, failed))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            tg_send(f"Evening shortlist failed: {esc(str(error))[:500]}")
        except Exception:
            pass  # if Telegram itself is the problem, the GitHub log still shows the error
        raise
