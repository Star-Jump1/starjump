"""Prints the ID numbers of your Kit tags, forms and email templates, so you can copy them into GitHub."""

import requests

from common import setting

HEADERS = {"X-Kit-Api-Key": setting("KIT_API_KEY", required=True), "Accept": "application/json"}

for path, label in [("tags", "Tags"), ("forms", "Forms"), ("email_templates", "Email templates")]:
    print(f"\n{label}")
    response = requests.get(f"https://api.kit.com/v4/{path}", headers=HEADERS, timeout=30)
    if response.status_code != 200:
        print(f"  Kit returned {response.status_code}: {response.text[:300]}")
        continue
    data = response.json()
    rows = data.get(path) or next((v for v in data.values() if isinstance(v, list)), [])
    if not rows:
        print("  none found")
    for row in rows:
        print(f"  {row.get('id')}  {row.get('name')}")
