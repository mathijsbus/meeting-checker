import os
import sys
import time
import json
import re
from html import unescape
from urllib.parse import urlparse
import requests

# ===== Config via Secrets / env =====
LOGIN_URL       = os.getenv("LOGIN_URL")        # bv. https://site.tld/login
TARGET_URL      = os.getenv("TARGET_URL")       # bv. https://site.tld/afspraak
USERNAME        = os.getenv("SITE_USERNAME")
PASSWORD        = os.getenv("SITE_PASSWORD")
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")
EXTRA_FIELDS    = json.loads(os.getenv("EXTRA_FIELDS_JSON", "{}"))  # bv {"csrf_token": "..."}
TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")
USER_AGENT      = os.getenv("USER_AGENT", "Mozilla/5.0 (meeting-checker)")
JITTER_MAX      = int(os.getenv("JITTER_SECONDS_MAX", "5"))

STATE_FILE      = "state.json"   # we commit dit bestandje zodat status behouden blijft

# ===== Kleine beleefdheids-pauze =====
if JITTER_MAX > 0:
    import random
    time.sleep(random.randint(0, JITTER_MAX))

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHATID, "text": text}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()

def looks_like_login_page(html: str) -> bool:
    # Heuristiek: zoek form + username/password fields of "inloggen" tekst
    lowered = html.lower()
    if "wachtwoord" in lowered or "password" in lowered:
        return True
    if re.search(r'<form[^>]+>', lowered) and re.search(r'name=["\']?(?:username|email)["\']?', lowered):
        return True
    return False

def find_csrf(html: str):
    # Generieke CSRF extractor; pas desnoods aan op jouw site
    m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1)
    # Andere veelvoorkomende namen:
    for name in ("_token", "__requestverificationtoken", "csrfmiddlewaretoken"):
        m = re.search(fr'name=["\']{name}["\']\s+value=["\']([^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
    return None

def safe_get(session: requests.Session, url: str) -> requests.Response:
    # Volg redirects; geef response terug
    r = session.get(url, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r

def is_redirect_to_login(resp: requests.Response) -> bool:
    # Als eind-URL op login zit, of HTML op login lijkt
    final_url = resp.url
    if "login" in final_url.lower():
        return True
    return looks_like_login_page(resp.text)

def do_login(session: requests.Session):
    # 1) GET loginpagina (haal evt CSRF)
    r = session.get(LOGIN_URL, timeout=25, allow_redirects=True)
    r.raise_for_status()
    html = r.text

    fields = dict(EXTRA_FIELDS)
    token = find_csrf(html)
    if token and ("csrf_token" not in fields and "_token" not in fields):
        # Probeer generieke sleutel
        fields["csrf_token"] = token

    payload = {USERNAME_FIELD: USERNAME, PASSWORD_FIELD: PASSWORD, **fields}

    # 2) POST login
    r = session.post(LOGIN_URL, data=payload, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r

def fetch_target_html() -> str:
    with requests.Session() as s:
        s.headers.update({"User-Agent": USER_AGENT})

        # Probeer direct de target (hergebruik cookie als die nog geldig is)
        try:
            r = safe_get(s, TARGET_URL)
        except requests.HTTPError as e:
            # Als 401/403, login en opnieuw
            if e.response is not None and e.response.status_code in (401, 403):
                do_login(s)
                r = safe_get(s, TARGET_URL)
            else:
                raise

        # Als we toch op login terechtkwamen, login en opnieuw
        if is_redirect_to_login(r):
            do_login(s)
            r = safe_get(s, TARGET_URL)

        return r.text

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"available": None}  # onbekend

def save_state(new_state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=2)

def main():
    # Verplichte envs
    required = [
        "LOGIN_URL","TARGET_URL","SITE_USERNAME","SITE_PASSWORD",
        "TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"
    ]
    missing = [n for n in required if not os.getenv(n)]
    if missing:
        print("Missing required env: " + ", ".join(missing), file=sys.stderr)
        sys.exit(2)

    html = fetch_target_html()
    text = unescape(html)

    # Beschikbaarheid: als de “geen dagen” tekst NIET voorkomt, is er iets beschikbaar
    available = (TEXT_TO_FIND not in text)

    # De-dupe met state.json
    state = load_state()
    prev = state.get("available")

    if available and prev is not True:
        # status veranderde naar beschikbaar → push!
        send_telegram("🎉 Er zijn data beschikbaar! Check de site nu.")
        print("Notificatie verstuurd.")

    if (prev is None) or (available != prev):
        # status veranderd → state updaten (wordt door workflow gecommit)
        save_state({"available": available})
        # signaal aan workflow dat er iets te committen is
        print("STATE_CHANGED=1")

    print("Status:", "BESCHIKBAAR" if available else "GEEN")
    return 0

if __name__ == "__main__":
    sys.exit(main())
