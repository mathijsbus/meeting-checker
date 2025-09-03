import os
import sys
import time
import json
import re
from html import unescape
import requests

# ===================== Config uit env / Secrets =====================

LOGIN_URL       = os.getenv("LOGIN_URL")        # bv. https://site.tld/login
TARGET_URL      = os.getenv("TARGET_URL")       # bv. https://site.tld/afspraak
USERNAME        = os.getenv("SITE_USERNAME")
PASSWORD        = os.getenv("SITE_PASSWORD")
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")   # pas aan indien anders
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")   # pas aan indien anders
TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")
USER_AGENT      = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
JITTER_MAX      = int(os.getenv("JITTER_SECONDS_MAX", "5"))  # kleine willekeurige pauze

STATE_FILE      = "state.json"  # wordt gebruikt voor de-dupe (alleen push bij status-wijziging)

# ===== Fix A: tolerant inlezen van EXTRA_FIELDS_JSON (mag leeg zijn) =====
def _json_env(name: str, default):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Warning: invalid JSON in {name}: {e}; using default.", file=sys.stderr)
        return default

EXTRA_FIELDS = _json_env("EXTRA_FIELDS_JSON", {})  # bv. {"keep_logged_in":"1"} of CSRF-veld

# ===================== Kleine beleefdheids-pauze =====================
if JITTER_MAX > 0:
    import random
    time.sleep(random.randint(0, JITTER_MAX))

# ===================== Helpers =====================

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHATID, "text": text}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()

def looks_like_login_page(html: str) -> bool:
    lowered = html.lower()
    if "wachtwoord" in lowered or "password" in lowered or "inloggen" in lowered or "login" in lowered:
        return True
    # simpele heuristiek: formulier met username/email veld
    if re.search(r'<form[^>]*>', lowered) and re.search(r'name=["\']?(?:username|email)["\']?', lowered):
        return True
    return False

def find_csrf(html: str):
    # probeer verschillende gangbare namen
    patterns = [
        r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
        r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']',
        r'name=["\']__requestverificationtoken["\']\s+value=["\']([^"\']+)["\']',
        r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
    ]
    lowered = html.lower()
    for pat in patterns:
        m = re.search(pat, lowered, re.I)
        if m:
            return m.group(1)
    return None

def safe_get(session: requests.Session, url: str) -> requests.Response:
    r = session.get(url, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r

def is_redirect_to_login(resp: requests.Response) -> bool:
    final_url = resp.url.lower()
    if "login" in final_url:
        return True
    return looks_like_login_page(resp.text)

def do_login(session: requests.Session):
    # 1) GET loginpagina (haal evt. CSRF)
    r = session.get(LOGIN_URL, timeout=25, allow_redirects=True)
    r.raise_for_status()
    html = r.text

    fields = dict(EXTRA_FIELDS)  # kopie
    token = find_csrf(html)
    # Voeg CSRF toe als we er één vonden en er nog geen in EXTRA_FIELDS staat
    if token and not any(k in fields for k in ("csrf_token", "_token", "__requestverificationtoken", "csrfmiddlewaretoken")):
        fields["csrf_token"] = token

    payload = {USERNAME_FIELD: USERNAME, PASSWORD_FIELD: PASSWORD, **fields}

    # 2) POST login
    r = session.post(LOGIN_URL, data=payload, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r

def fetch_target_html() -> str:
    with requests.Session() as s:
        s.headers.update({"User-Agent": USER_AGENT})

        # Probeer direct de target (indien nog ingelogd via server-side sessie)
        try:
            r = safe_get(s, TARGET_URL)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                do_login(s)
                r = safe_get(s, TARGET_URL)
            else:
                raise

        # Als we toch op login uitkwamen of login-HTML zien → login en opnieuw
        if is_redirect_to_login(r):
            do_login(s)
            r = safe_get(s, TARGET_URL)

        return r.text

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"available": None}

def save_state(new_state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=2)

# ===================== Main =====================

def main():
    # check verplichte envs
    required = [
        "LOGIN_URL", "TARGET_URL", "SITE_USERNAME", "SITE_PASSWORD",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
    ]
    missing = [n for n in required if not os.getenv(n)]
    if missing:
        print("Missing required env: " + ", ".join(missing), file=sys.stderr)
        return 2

    html = fetch_target_html()
    text = unescape(html)

    # Als de “geen dagen” tekst NIET voorkomt, dan lijkt er iets beschikbaar
    available = (TEXT_TO_FIND not in text)

    # De-dupe op basis van state.json
    state = load_state()
    prev = state.get("available")

    if available and prev is not True:
        send_telegram("🎉 Er zijn data beschikbaar! Check de site nu.")
        print("Notificatie verstuurd.")

    # Update state en laat de workflow weten dat er iets te committen is
    if (prev is None) or (available != prev):
        save_state({"available": available})
        print("STATE_CHANGED=1")

    print("Status:", "BESCHIKBAAR" if available else "GEEN")
    return 0

if __name__ == "__main__":
    sys.exit(main())
