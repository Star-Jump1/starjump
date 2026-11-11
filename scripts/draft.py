"""Evening job, every 15 minutes: turns your Telegram reply into tomorrow's email and sends you a preview."""

import time

from common import SHORTLIST_FILE, esc, load_json, tg_send
from issue import preview_message, process_messages

SHORTLIST_MAX_AGE = 14 * 3600  # seconds


def main():
    shortlist = load_json(SHORTLIST_FILE, None)
    if not shortlist or time.time() - shortlist["created"] > SHORTLIST_MAX_AGE:
        print("No shortlist from this evening yet. Nothing to do.")
        return
    issue, changed, ignored = process_messages(shortlist)
    if changed:
        tg_send(preview_message(issue, ignored))
        print("Draft updated and preview sent.")
    elif ignored:
        tg_send("Not understood:\n" + "\n".join(esc(line) for line in ignored))
    else:
        print("No new messages.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            tg_send(f"Draft job failed: {esc(str(error))[:500]}")
        except Exception:
            pass
        raise
