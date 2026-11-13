"""Evening job: gather stories from the feeds and the web, ask Claude for the best 10, send them to Telegram.

Stage 1: a cheap model (Haiku) skims every headline from config/feeds.txt and keeps the possibles.
Stage 2: Claude searches the web for stories the feeds missed, following config/search.md.
Stage 3: Sonnet picks the final 10 from both, using config/rubric.md.
"""

import calendar
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
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

MAX_AGE_HOURS = 30    # ignore feed stories older than this
SHORTLIST_SIZE = 10
SKIM_ABOVE = 120      # with fewer stories than this, skip the first pass
CHUNK_SIZE = 250      # stories per first-pass request
KEEP_PER_CHUNK = 15   # most the first pass may keep from each chunk
USER_AGENT = "Mozilla/5.0 (compatible; StarjumpShortlist/1.0)"

FILTER_PROMPT = """You do a fast first pass for Starjump, a daily good-news email about progress. Read the editor's rubric, then the numbered headlines. Keep only stories that could plausibly pass. Be generous with possibles, but drop obvious misses.

{rubric}

Return JSON only, no other text: {{"keep": [12, 40, 41]}} with at most {k} ids."""

SEARCH_PROMPT = """You find stories for Starjump, a daily good-news email about progress. Today is {today}.

The editor's rubric:
{rubric}

The editor's search brief:
{brief}

Search the web for stories published in the last 48 hours that pass the rubric, especially ones big news sites missed. Use varied, specific searches. Include a story only if you saw its publication date and it is within the last 48 hours. Give each article's own address, never a search page or aggregator.

When you have finished searching, reply with JSON only, no other text:
{{"stories": [{{"title": "Headline", "url": "https://...", "summary": "One sentence", "date": "YYYY-MM-DD"}}]}}
Up to 20 stories."""

FINAL_PROMPT = """You shortlist stories for Starjump, a daily good-news email about progress. Follow the editor's rubric below exactly.

{rubric}

You will get a numbered list of candidate stories. Return JSON only, with no other text, in this shape:
{{"picks": [{{"id": 12, "headline": "Rewritten headline", "why": "Under 15 words on why it passes"}}]}}

Pick {n} stories, best first. If fewer than {n} pass the rubric, return only those that pass."""


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def json_from(text, key):
    """The JSON object in Claude's reply that contains the given key."""
    start = text.rfind('{"' + key + '"')
    if start == -1:
        start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"Claude did not return JSON. It said: {text[:300]}")
    return json.loads(text[start : end + 1])


def reply_text(message):
    return "".join(block.text for block in message.content if block.type == "text")


def real_link(link):
    """Bing News feeds wrap each link in a redirect; return the article's own address."""
    parsed = urlparse(link)
    if parsed.netloc.endswith("bing.com"):
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target.startswith("http"):
            return target
    return link


def read_feeds():
    lines = (CONFIG / "feeds.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def fetch(feed_url):
    try:
        response = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=20)
        response.raise_for_status()
        return feed_url, feedparser.parse(response.content), None
    except Exception as error:  # one broken feed should not stop the rest
        return feed_url, None, error.__class__.__name__


def gather(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    seen_titles, stories, failed, working = set(), [], [], 0

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(fetch, read_feeds()))

    for feed_url, feed, error in results:
        if error:
            failed.append(f"{feed_url} ({error})")
            continue
        working += 1
        for entry in feed.entries:
            link = real_link((entry.get("link") or "").strip())
            title = clean(entry.get("title"))
            if not link or not title:
                continue
            key = bare_url(link)
            title_key = re.sub(r"\W+", " ", title.lower()).strip()
            if key in seen or title_key in seen_titles:
                continue
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            when = datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc) if stamp else None
            if when and when < cutoff:
                continue
            seen.add(key)
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
    return stories, failed, working


def first_pass(client, stories, rubric, notes):
    """Haiku skims every headline in batches and keeps the possibles."""
    if len(stories) <= SKIM_ABOVE:
        return stories
    model = setting("FILTER_MODEL", "claude-haiku-5-5")
    kept = []
    for start in range(0, len(stories), CHUNK_SIZE):
        chunk = stories[start : start + CHUNK_SIZE]
        listing = "\n".join(f"[{i}] {s['title']} | {s['source']} | {s['summary'][:150]}" for i, s in enumerate(chunk))
        try:
            message = client.messages.create(
                model=model,
                max_tokens=600,
                system=FILTER_PROMPT.format(rubric=rubric, k=KEEP_PER_CHUNK),
                messages=[{"role": "user", "content": listing}],
            )
            ids = json_from(reply_text(message), "keep").get("keep", [])
            kept += [chunk[i] for i in ids[:KEEP_PER_CHUNK] if isinstance(i, int) and 0 <= i < len(chunk)]
        except Exception as error:
            notes.append(f"First pass skipped one batch ({error.__class__.__name__})")
    return kept


def web_search(client, rubric, seen, notes):
    """Claude searches the web for stories the feeds missed. Switched off if config/search.md has no brief."""
    path = CONFIG / "search.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    count = re.search(r"^Searches:\s*(\d+)", text, re.M)
    searches = int(count.group(1)) if count else 10
    brief = text.split("\n---", 1)[1].strip() if "\n---" in text else ""
    if not brief or searches == 0:
        return []

    tool = {"type": setting("WEB_SEARCH_TOOL", "web_search_20250305"), "name": "web_search", "max_uses": searches}
    prompt = SEARCH_PROMPT.format(
        today=nice_date(datetime.now(ZoneInfo("Europe/London"))),
        rubric=rubric,
        brief=brief,
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        for _ in range(5):  # a long search can pause; send it back to carry on
            message = client.messages.create(
                model=setting("CLAUDE_MODEL", "claude-sonnet-5-5"),
                max_tokens=8000,
                tools=[tool],
                messages=messages,
            )
            if message.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": message.content}]
        found = json_from(reply_text(message), "stories").get("stories", [])
    except Exception as error:
        notes.append(f"Web search failed: {str(error)[:200]}")
        return []

    stories = []
    for item in found:
        url = (item.get("url") or "").strip()
        if not url.startswith("http") or bare_url(url) in seen:
            continue
        seen.add(bare_url(url))
        stories.append(
            {
                "title": clean(item.get("title")),
                "url": url,
                "source": source_for(url),
                "paywall": is_paywalled(url),
                "summary": clean(item.get("summary"))[:280],
                "when": item.get("date", ""),
                "web": True,
            }
        )
    return stories


def final_pick(client, candidates, rubric):
    listing = "\n".join(
        f"[{i}] {s['title']} | {s['source']}{' (paywall)' if s['paywall'] else ''} | {s['summary']}"
        for i, s in enumerate(candidates)
    )
    message = client.messages.create(
        model=setting("CLAUDE_MODEL", "claude-sonnet-5-5"),
        max_tokens=4000,
        system=FINAL_PROMPT.format(rubric=rubric, n=SHORTLIST_SIZE),
        messages=[{"role": "user", "content": listing}],
    )
    picks = json_from(reply_text(message), "picks").get("picks", [])
    shortlist = []
    for pick in picks:
        index = pick.get("id")
        if isinstance(index, int) and 0 <= index < len(candidates):
            story = candidates[index]
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


def telegram_text(shortlist, stats, failed, notes):
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
    lines.append(stats)
    if notes:
        lines.append("\n".join(esc(n) for n in notes))
    if failed:
        shown = "\n".join(esc(f) for f in failed[:10])
        more = f"\n...and {len(failed) - 10} more" if len(failed) > 10 else ""
        lines.append(f"Feeds that failed tonight:\n{shown}{more}")
    return "\n\n".join(lines)


def main():
    # Clear old messages so the evening draft job only sees tonight's reply.
    tg_clear(tg_updates())

    rubric = (CONFIG / "rubric.md").read_text(encoding="utf-8")
    seen = {item["url"] for item in load_json(HISTORY_FILE, [])}
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    notes = []

    stories, failed, working = gather(seen)
    possibles = first_pass(client, stories, rubric, notes)
    found = web_search(client, rubric, seen, notes)
    candidates = possibles + found
    stats = (
        f"Read {len(stories)} stories from {working} feeds; {len(possibles)} made the first cut. "
        f"Web search added {len(found)}."
    )
    print(stats)
    for line in notes + failed:
        print("  ", line)

    if not candidates:
        tg_send("Nothing came through tonight, so there is no shortlist.\n\n" + stats)
        return

    shortlist = final_pick(client, candidates, rubric)
    save_json(
        SHORTLIST_FILE,
        {"created": int(time.time()), "created_iso": datetime.now(timezone.utc).isoformat(), "items": shortlist},
    )
    tg_send(telegram_text(shortlist, stats, failed, notes))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            tg_send(f"Evening shortlist failed: {esc(str(error))[:500]}")
        except Exception:
            pass  # if Telegram itself is the problem, the GitHub log still shows the error
        raise
