"""Morning job: applies any late Telegram edits, then sends data/issue.md as it stands.

MODE setting (a GitHub variable):
  draft  - saves the email as a draft in Kit. Nothing sends. The archive page is not touched.
  tester - schedules the email to subscribers tagged with TESTER_TAG_ID only. Updates the archive.
  live   - schedules the email to every subscriber. Updates the archive.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from common import (
    DOCS,
    HISTORY_FILE,
    SHORTLIST_FILE,
    bare_url,
    esc,
    load_json,
    nice_date,
    nice_time,
    save_json,
    setting,
    tg_send,
)
from issue import (
    auto_pick_count,
    load_issue,
    new_issue,
    preview_of,
    process_messages,
    record_taste,
    save_issue,
    shortlist_day,
    subject_of,
)

LONDON = ZoneInfo("Europe/London")
MARKER = "<!-- NEW DAYS GO BELOW THIS LINE -->"
KIT_URL = "https://api.kit.com/v4/broadcasts"
HISTORY_DAYS = 30


def send_time():
    """Today at SEND_TIME, London time. If that has passed (a manual test run), ten minutes from now."""
    now = datetime.now(LONDON)
    hour, minute = map(int, setting("SEND_TIME", "08:00").split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now + timedelta(minutes=5):
        target = (now + timedelta(minutes=10)).replace(second=0, microsecond=0)
    return target


def email_html(issue):
    html = ""
    if issue["intro"].strip():
        html += f'<p style="margin:0 0 24px 0;font-size:18px;line-height:1.4;">{esc(issue["intro"])}</p>'
    html += "".join(
        '<p style="margin:0 0 18px 0;font-size:18px;line-height:1.4;">'
        f'<a href="{esc(i["url"])}">{esc(i["headline"])}</a> '
        f'<span style="color:#595959;font-size:14px;">{esc(i["source"])}</span></p>'
        for i in issue["items"]
    )
    site = setting("SITE_URL")
    if site:
        html += (
            '<p style="margin:28px 0 0 0;color:#595959;font-size:14px;">'
            f'Every past issue: <a href="{esc(site)}">{esc(site)}</a></p>'
        )
    return html


def create_broadcast(issue, when, mode):
    body = {
        "subject": subject_of(issue),
        "preview_text": preview_of(issue),
        "content": email_html(issue),
        "description": f"Issue for {nice_date(when)}",
        "public": False,
        "published_at": when.isoformat(),
        "send_at": None if mode == "draft" else when.isoformat(),
    }
    if mode == "tester":
        tag_id = int(setting("TESTER_TAG_ID", required=True))
        body["subscriber_filter"] = [{"all": [{"type": "tag", "ids": [tag_id]}], "any": None, "none": None}]
    template_id = setting("KIT_TEMPLATE_ID")
    if template_id:
        body["email_template_id"] = int(template_id)

    response = requests.post(
        KIT_URL,
        headers={
            "X-Kit-Api-Key": setting("KIT_API_KEY", required=True),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Kit refused the email ({response.status_code}): {response.text[:400]}")


def update_site(items, day):
    page = DOCS / "index.html"
    text = page.read_text(encoding="utf-8")
    if MARKER not in text:
        raise RuntimeError("The marker line is missing from docs/index.html, so the archive was not updated.")
    day_id = day.isoformat()
    # A re-run on the same day replaces that day's block rather than adding a second one.
    text = re.sub(rf'\n?<section class="day" id="{day_id}">.*?</section>', "", text, flags=re.S)
    text = re.sub(r'\n?<p class="empty">.*?</p>', "", text, flags=re.S)
    links = "".join(
        f'    <li><a href="{esc(i["url"])}">{esc(i["headline"])}</a> <span class="src">{esc(i["source"])}</span></li>\n'
        for i in items
    )
    block = (
        f'\n<section class="day" id="{day_id}">\n'
        f'  <h2><a href="#{day_id}">{nice_date(day)}</a></h2>\n'
        f"  <ul>\n{links}  </ul>\n"
        "</section>"
    )
    page.write_text(text.replace(MARKER, MARKER + block, 1), encoding="utf-8")


def update_history(items, day):
    history = {h["url"]: h for h in load_json(HISTORY_FILE, [])}
    for i in items:
        history[bare_url(i["url"])] = {"url": bare_url(i["url"]), "date": day.isoformat()}
    cutoff = (day - timedelta(days=HISTORY_DAYS)).isoformat()
    save_json(HISTORY_FILE, [h for h in history.values() if h["date"] >= cutoff])


def main():
    mode = setting("MODE", "draft").lower()
    if mode not in {"draft", "tester", "live"}:
        raise RuntimeError(f"MODE must be draft, tester or live, not '{mode}'.")

    when = send_time()
    # Today's email, or tomorrow's if this is a test run in the afternoon.
    days = [when.date(), when.date() + timedelta(days=1)]
    shortlist = load_json(SHORTLIST_FILE, None)
    current = shortlist if shortlist and shortlist_day(shortlist) in days else None

    # Pick up any edits or notes sent on Telegram after the last evening draft run.
    if current:
        process_messages(current)

    issue = load_issue(days[0]) or load_issue(days[1])
    auto = False
    if issue is None:
        count = auto_pick_count()
        if not current or not current["items"] or count == 0:
            tg_send("No email was prepared for today, so nothing was sent.")
            return
        top = [{k: i[k] for k in ("headline", "url", "source")} for i in current["items"][:count]]
        issue, auto = new_issue(top, when.date()), True

    if issue["status"] == "skip":
        tg_send("Today was skipped. Nothing was sent.")
        return
    if issue["status"] == "sent":
        tg_send("This email has already been sent, so it was not sent again.")
        return
    if not issue["items"]:
        tg_send("Today's email has no stories, so nothing was sent.")
        return

    create_broadcast(issue, when, mode)
    if mode != "draft":
        update_site(issue["items"], when.date())
        update_history(issue["items"], when.date())
        issue["status"] = "sent"
        save_issue(issue)
        if current and not auto:
            record_taste(current, issue, when.date())

    status = {
        "draft": "saved as a draft in Kit. Nothing will send.",
        "tester": f"scheduled for {nice_time(when)} to tester-tagged subscribers only.",
        "live": f"scheduled for {nice_time(when)} to all subscribers.",
    }[mode]
    lead = "No picks came in, so I chose the top ones from the shortlist.\n" if auto else ""
    tg_send(f"{lead}Done: {len(issue['items'])} stories {status}\nSubject: {esc(subject_of(issue))}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            tg_send(f"Morning job failed: {esc(str(error))[:500]}")
        except Exception:
            pass  # if Telegram itself is the problem, the GitHub log still shows the error
        raise
