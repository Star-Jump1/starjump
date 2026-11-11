"""Tomorrow's email lives in data/issue.md. This module reads it, writes it and applies your Telegram edits.

Telegram messages, after the evening shortlist:
  1 4 6 7 9                 numbers alone pick from the shortlist and start the email again
  4: New headline           in the same message as numbers, renames shortlist story 4
  2: New headline           on its own (once a draft exists), renames story 2 of the email
  drop 3                    removes story 3 of the email
  add: Headline https://…   adds a story at the end
  subject: …                sets the subject line (blank uses the first headline)
  preview: …                sets the preview text (blank uses the next two headlines)
  intro: …                  adds a line above the stories
  skip / send               cancels tomorrow's email / undoes a skip
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common import DATA, esc, nice_date, setting, source_for, tg_clear, tg_updates

ISSUE_FILE = DATA / "issue.md"
LONDON = ZoneInfo("Europe/London")

STORY_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s*\[(.+)\]\((\S+)\)\s*(?:\|\s*(.*?))?\s*$")
FIELD_LINE = re.compile(r"^\s*(issue|status|subject|preview|intro)\s*:\s*(.*?)\s*$", re.I)

HELP = """<!--
Edit anything above, then click Commit changes. The morning job sends this file as it stands.
  Change any headline, link or source. Reorder lines. Delete a line to drop a story.
  Add a story as:  6. [Your headline](https://link) | Source
  Leave Subject blank to use the first headline. Leave Preview blank to use the next two.
  Intro is an optional line shown above the stories.
  Set Status to skip to send nothing.
-->"""


def issue_day(now=None):
    """The date of the next send: tomorrow if it is after midday, otherwise today."""
    now = now or datetime.now(LONDON)
    return (now + timedelta(days=1)).date() if now.hour >= 12 else now.date()


def new_issue(items, day):
    return {"issue": nice_date(day), "status": "ready", "subject": "", "preview": "", "intro": "", "items": items}


def parse_issue(text):
    issue = {"issue": "", "status": "ready", "subject": "", "preview": "", "intro": "", "items": []}
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for line in text.splitlines():
        story = STORY_LINE.match(line)
        if story:
            url = story.group(2).strip()
            issue["items"].append(
                {
                    "headline": story.group(1).strip(),
                    "url": url,
                    "source": (story.group(3) or "").strip() or source_for(url),
                }
            )
            continue
        field = FIELD_LINE.match(line)
        if field:
            issue[field.group(1).lower()] = field.group(2)
    issue["status"] = (issue["status"] or "ready").strip().lower()
    return issue


def render_issue(issue):
    lines = [
        f"Issue: {issue['issue']}",
        f"Status: {issue['status']}",
        f"Subject: {issue['subject']}",
        f"Preview: {issue['preview']}",
        f"Intro: {issue['intro']}",
        "",
    ]
    lines += [f"{n}. [{i['headline']}]({i['url']}) | {i['source']}" for n, i in enumerate(issue["items"], 1)]
    return "\n".join(lines) + "\n\n" + HELP + "\n"


def load_issue(day):
    """The saved issue, but only if it is for the given day."""
    if not ISSUE_FILE.exists():
        return None
    issue = parse_issue(ISSUE_FILE.read_text(encoding="utf-8"))
    return issue if issue["issue"].strip() == nice_date(day) else None


def save_issue(issue):
    ISSUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ISSUE_FILE.write_text(render_issue(issue), encoding="utf-8")


def subject_of(issue):
    return issue["subject"].strip() or (issue["items"][0]["headline"] if issue["items"] else "")


def preview_of(issue):
    return issue["preview"].strip() or "; ".join(i["headline"] for i in issue["items"][1:3])[:140]


def parse_picks(text, shortlist):
    """'3 1 7' picks shortlist stories 3, 1 and 7 in that order. '3: New headline' also renames story 3."""
    order, renamed = [], {}
    for line in text.splitlines():
        match = re.match(r"\s*(\d{1,2})\s*[:=]\s*(.+)$", line)
        if match:
            number = int(match.group(1))
            renamed[number] = match.group(2).strip()
            if number not in order:
                order.append(number)
            continue
        for number in map(int, re.findall(r"\d{1,2}", line)):
            if number not in order:
                order.append(number)
    chosen = []
    for number in order:
        if 1 <= number <= len(shortlist):
            item = {k: shortlist[number - 1][k] for k in ("headline", "url", "source")}
            if number in renamed:
                item["headline"] = renamed[number]
            chosen.append(item)
    return chosen


def apply_message(text, issue, shortlist, day):
    """Apply one Telegram message. Returns (issue, lines not understood)."""
    stripped = text.strip()
    word = stripped.lower()
    if word in {"skip", "none", "no"}:
        issue = issue or new_issue([], day)
        issue["status"] = "skip"
        return issue, []
    if word in {"send", "unskip", "go"}:
        if issue:
            issue["status"] = "ready"
        return issue, []

    fields, rest, ignored = {}, [], []
    for line in (l for l in stripped.splitlines() if l.strip()):
        match = re.match(r"\s*(subject|preview|intro)\s*:\s*(.*)$", line, re.I)
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()
        else:
            rest.append(line)

    if any(re.fullmatch(r"[\d\s,]+", line) for line in rest):
        issue = new_issue(parse_picks("\n".join(rest), shortlist), day)
    elif issue is None:
        return None, [f"{line} (pick numbers first)" for line in rest] + [f"{k}: (pick numbers first)" for k in fields]
    else:
        drops = []
        for line in rest:
            drop = re.match(r"\s*drop\s+([\d\s,]+)$", line, re.I)
            add = re.match(r"\s*add\s*:\s*(.+?)\s+(https?://\S+)\s*$", line, re.I)
            rename = re.match(r"\s*(\d{1,2})\s*[:=]\s*(.+)$", line)
            if drop:
                drops += [int(n) for n in re.findall(r"\d+", drop.group(1))]
            elif add:
                url = add.group(2)
                issue["items"].append({"headline": add.group(1).strip(" |"), "url": url, "source": source_for(url)})
            elif rename and 1 <= int(rename.group(1)) <= len(issue["items"]):
                issue["items"][int(rename.group(1)) - 1]["headline"] = rename.group(2).strip()
            else:
                ignored.append(line)
        for number in sorted(set(drops), reverse=True):
            if 1 <= number <= len(issue["items"]):
                issue["items"].pop(number - 1)

    issue.update(fields)
    return issue, ignored


def process_messages(shortlist):
    """Apply every new Telegram message from you to data/issue.md. Returns (issue, changed, ignored)."""
    chat_id = int(setting("TELEGRAM_CHAT_ID", required=True))
    day = issue_day()
    updates = tg_updates()
    texts = [
        u["message"]["text"]
        for u in updates
        if "message" in u
        and u["message"].get("chat", {}).get("id") == chat_id
        and u["message"].get("date", 0) >= shortlist["created"]
        and u["message"].get("text")
    ]
    issue, changed, ignored = load_issue(day), False, []
    for text in texts:
        updated, not_understood = apply_message(text, issue, shortlist["items"], day)
        ignored += not_understood
        if updated is not None:
            issue, changed = updated, True
    if changed:
        save_issue(issue)
    tg_clear(updates)
    return issue, changed, ignored


def preview_message(issue, ignored=()):
    repo = setting("GITHUB_REPOSITORY")
    lines = []
    if issue["status"] == "skip":
        lines.append(f"<b>{esc(issue['issue'])}: skipped.</b> Nothing will send. Reply <code>send</code> to undo.")
    else:
        lines.append(
            f"<b>Email for {esc(issue['issue'])}</b>\n"
            f"Subject: {esc(subject_of(issue))}\n"
            f"Preview: {esc(preview_of(issue))}"
            + (f"\nIntro: {esc(issue['intro'])}" if issue["intro"].strip() else "")
        )
        lines.append(
            "\n".join(
                f"{n}. <a href=\"{esc(i['url'])}\">{esc(i['headline'])}</a> ({esc(i['source'])})"
                for n, i in enumerate(issue["items"], 1)
            )
            or "No stories yet."
        )
    if ignored:
        lines.append("Not understood:\n" + "\n".join(esc(line) for line in ignored))
    lines.append(
        "Edit by reply: <code>2: New headline</code>  <code>drop 3</code>  "
        "<code>add: Headline https://link</code>  <code>subject: …</code>  <code>preview: …</code>  "
        "<code>intro: …</code>  <code>skip</code>\nNumbers alone start again from the shortlist."
        + (f"\n<a href=\"https://github.com/{repo}/edit/main/data/issue.md\">Edit everything on GitHub</a>" if repo else "")
    )
    return "\n\n".join(lines)
