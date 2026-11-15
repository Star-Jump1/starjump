"""Evening job, every 15 minutes: turns your Telegram reply into tomorrow's email and sends you a preview."""

from common import SHORTLIST_FILE, esc, load_json, tg_send
from issue import issue_day, preview_message, process_messages, shortlist_day


def main():
    shortlist = load_json(SHORTLIST_FILE, None)
    if not shortlist or shortlist_day(shortlist) != issue_day():
        print("No shortlist for the next email yet. Nothing to do.")
        return
    issue, changed, ignored, noted = process_messages(shortlist)
    if noted:
        tg_send("Noted for future shortlists:\n" + "\n".join(esc(n) for n in noted))
    if changed:
        tg_send(preview_message(issue, ignored))
        print("Draft updated and preview sent.")
    elif ignored:
        tg_send("Not understood:\n" + "\n".join(esc(line) for line in ignored))
    elif not noted:
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
