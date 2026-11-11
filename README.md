# Starjump

Evening: `scripts/shortlist.py` gathers stories from `config/feeds.txt`, asks Claude for 10 using `config/rubric.md`, and sends them to you on Telegram.

Morning: `scripts/send.py` reads your reply, schedules the email in Kit and adds the day to `docs/index.html`, which GitHub Pages publishes.

Settings live in GitHub under Settings, Secrets and variables, Actions. See the build manual for the full list.
