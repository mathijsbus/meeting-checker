import os
import sys
import time
import json
import re
from html import unescape
import requests

# ===== Config uit omgevingsvariabelen (komen uit GitHub Secrets) =====
LOGIN_URL       = os.getenv("LOGIN_URL")        # bv. https://site.tld/login
TARGET_URL      = os.getenv("TARGET_URL")       # bv. https://site.tld/afspraak
USERNAME        = os.getenv("SITE_USERNAME")
PASSWORD        = os.getenv("SITE_PASSWORD")
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")  # pas aan naar echte form-naam
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")  # pas aan naar echte form-naam
EXTRA_FIELDS    = json.loads(os.getenv("EXTRA_FIELDS_JSON", "{}"))  # bv {"csrf_token": "ABC"}
TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")
USER_AGENT      = os.getenv("USER_AGENT", "Mozilla/5.0 (meeting-checker)")
JITTER_MAX      = int(os.getenv("JITTER_SECONDS_MAX", "5"))  # kleine beleefdheids-pauze

if JITTER_MAX > 0:
    import random
    time.sleep(random.randint(0, JITTER_MAX))

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHATID, "text": text}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()

def get_csrf(html):
    # Probeer generiek een CSRF-token te vinden (pas aan indien jouw site anders heet)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None

def login_and_get(session: requests.Session) -> str:
    # 1) loginpagina ophalen (mogelijk CSRF)
    r = session.get(LOGIN_URL, timeout=20)
    r.raise_for_status()
    html = r.text

    # CSRF automatisch invullen als niet al in EXTRA_FIELDS meegegeven
    fields = dict(EXTRA_FIELDS)
    if "csrf_token" not in fields:
        token = get_csrf(html)
        if token:
            fields["csrf_token"] = token

    # 2) inloggen (POST met form fields)
    payload = {USERNAME_FIELD: USERNAME, PASSWORD_FIELD: PASSWORD, **fields}
    r = session.post(LOGIN_URL, data=payload, timeout=20, allow_redirects=True)
    r.raise_for_status()

    # 3) doelpagina ophalen
    r = session.get(TARGET_URL, timeout=20)
    r.raise_for_status()
    return r.text

def main():
    # Check op verplichte variabelen
    required = [
        "LOGIN_URL","TARGET_URL","SITE_USERNAME","SITE_PASSWORD",
        "TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print("Missing required env: " + ", ".join(missing), file=sys.stderr)
        sys.exit(2)

    with requests.Session() as s:
        s.headers.update({"User-Agent": USER_AGENT})
        try:
            html = login_and_get(s)
        except requests.HTTPError as e:
            print(f"HTTPError: {e}", file=sys.stderr)
            sys.exit(3)

    text = unescape(html)
    available = TEXT_TO_FIND not in text  # als de tekst NIET gevonden is, dan is er iets beschikbaar

    if available:
        msg = "🎉 Er zijn data beschikbaar! Check de site nu."
        try:
            send_telegram(msg)
            print("Notificatie verstuurd.")
        except Exception as e:
            print(f"Telegram failed: {e}", file=sys.stderr)
            sys.exit(4)
    else:
        print("Nog niets beschikbaar.")

if __name__ == "__main__":
    main()
