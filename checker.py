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
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")     # pas aan indien anders
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")     # pas aan indien anders
TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
# Optioneel: tekst die ALTIJD op de ECHTE afsprakenpagina staat (kopje, label e.d.)
CONFIRM_TEXT    = os.getenv("CONFIRM_TEXT", "").strip()
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")
USER_AGENT      = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
JITTER_MAX      = int(os.getenv("JITTER_SECONDS_MAX", "5"))  # kleine willekeurige pauze
STATE_FILE      = "state.json"  # de-dupe (alleen push bij status-wijziging)

# ===== Tolerant inlezen van EXTRA_FIELDS_JSON (mag leeg zijn) =====
def _json_env(name: str, default):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Warning: invalid JSON in {name}: {e}; using default.", file=sys.stderr)
        return default

EXTRA_FIELDS = _json_env("EXTRA_FIELDS_JSON", {})  # bv. {"keep_logged_in":"1"} of site-specifieke CSRF-naam

# ===================== Kleine beleefdheids-pauze =====================
if JITTER_MAX > 0:
    import random
    time.sleep(random.randint(0, JITTER_MAX))

# ===================== Heuristieken & helpers =====================

LOGIN_HINTS = (
    "wachtwoord", "password", "inloggen", "aanmelden", "login", 'type="password"'
)

MONTHS_NL = "jan|feb|mrt|apr|mei|jun|jul|aug|sep|okt|nov|dec"
MONTHS_EN = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"

DATE_PATTERNS = [
    rf"\b\d{{1,2}}\s*(?:{MONTHS_NL}|{MONTHS_EN})\b",  # 5 sep / 5 okt / 5 oct
    r"\b\d{4}-\d{2}-\d{2}\b",                        # 2025-09-03
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",                  # 3/9/2025
]
TIME_PATTERN = r"\b\d{1,2}:\d{2}\b"                  # 09:30

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHATID, "text": text}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()

def looks_like_login_page(html: str) -> bool:
    lowered = html.lower()
    if any(hint in lowered for hint in LOGIN_HINTS):
        return True
    # simpele heuristiek: formulier met username/email veld
    if re.search(r'<form[^>]*>', lowered) and re.search(r'name=["\']?(?:username|email)["\']?', lowered):
        return True
    return False

def find_csrf(html: str):
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
    if token and not any(k in fields for k in ("csrf_token", "_token", "__requestverificationtoken", "csrfmiddlewaretoken")):
        fields["csrf_token"] = token

    payload = {USERNAME_FIELD: USERNAME, PASSWORD_FIELD: PASSWORD, **fields}

    # 2) POST login
    r = session.post(LOGIN_URL, data=payload, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r

def fetch_target(session: requests.Session):
    """Probeer target te halen; doe login als nodig. Return (html, final_url, login_attempted)."""
    login_attempted = False
    try:
        r = safe_get(session, TARGET_URL)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            do_login(session)
            login_attempted = True
            r = safe_get(session, TARGET_URL)
        else:
            raise

    if is_redirect_to_login(r):
        do_login(session)
        login_attempted = True
        r = safe_get(session, TARGET_URL)

    return r.text, r.url, login_attempted

def hints_of_real_slots_page(html: str) -> bool:
    """Alleen gebruiken *voor* we pushen: voorkom valse positieven.
       True als de content plausibel bij een 'slots/agenda'-pagina hoort."""
    lowered = html.lower()
    # Als gebruiker CONFIRM_TEXT zet, moet die aanwezig zijn
    if CONFIRM_TEXT:
        if CONFIRM_TEXT.lower() not in lowered:
            return False
    # Anders: heuristiek – zoek naar datum/tijd of woorden die vaak voorkomen
    date_like = any(re.search(pat, lowered) for pat in DATE_PATTERNS) or re.search(TIME_PATTERN, lowered)
    context_words = any(w in lowered for w in ("afspraak", "agenda", "kalender", "beschikbaar", "dagen", "slots", "datum"))
    return bool(date_like or context_words)

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

    with requests.Session() as s:
        s.headers.update({"User-Agent": USER_AGENT})
        html, final_url, login_attempted = fetch_target(s)

    text = unescape(html)

    # 1) Als we nog steeds op login-achtige content zitten → NIET alerten
    if looks_like_login_page(text):
        print("Op loginpagina terechtgekomen; geen alert. (Controleer USERNAME_FIELD/PASSWORD_FIELD/EXTRA_FIELDS_JSON.)")
        print(f"Final URL: {final_url}")
        return 0

    # 2) Bepaal availability: als de “geen dagen”-zin NIET voorkomt, is het mogelijk beschikbaar
    candidate_available = (TEXT_TO_FIND not in text)

    # 3) Alleen pushen als we sterke aanwijzing hebben dat dit écht de afsprakenpagina is
    will_alert = candidate_available and hints_of_real_slots_page(text)

    # De-dupe op basis van state.json
    state = load_state()
    prev = state.get("available")
    available = bool(will_alert)  # 'beschikbaar' definiëren we nu als 'we zouden alerten'

    if will_alert and prev is not True:
        send_telegram("🎉 Er lijken data beschikbaar! Check de site nu.")
        print("Notificatie verstuurd.")

    if (prev is None) or (available != prev):
        save_state({"available": available})
        print("STATE_CHANGED=1")

    print(f"Status: {'BESCHIKBAAR' if available else 'GEEN'}")
    print(f"Final URL: {final_url}")
    if login_attempted:
        print("Login attempted: yes")
    else:
        print("Login attempted: no")
    return 0

if __name__ == "__main__":
    sys.exit(main())
