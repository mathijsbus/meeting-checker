import os
import sys
import time
import json
import re
from html import unescape
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# ===================== Config uit env / Secrets =====================

LOGIN_URL       = os.getenv("LOGIN_URL")        # bv. https://site.tld/login
TARGET_URL      = os.getenv("TARGET_URL")       # bv. https://site.tld/afspraak
USERNAME        = os.getenv("SITE_USERNAME")
PASSWORD        = os.getenv("SITE_PASSWORD")
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")     # pas aan indien anders
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")     # pas aan indien anders

TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
CONFIRM_TEXT    = (os.getenv("CONFIRM_TEXT") or "").strip()   # optioneel: vaste tekst die altijd op de slots-pagina staat

# *** NIEUW: richt preciezer ***
EXPECTED_HOST   = (os.getenv("EXPECTED_HOST") or "").strip()  # bv. 'mijnsite.nl'  (optioneel maar sterk aangeraden)
EXPECTED_PATH   = (os.getenv("EXPECTED_PATH") or "").strip()  # bv. '/afspraak'    (optioneel substring match)
CSS_SELECTOR    = (os.getenv("CSS_SELECTOR") or "").strip()   # bv. '#slots-message' of '.calendar-status' (aanrader)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")

USER_AGENT      = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
JITTER_MAX      = int(os.getenv("JITTER_SECONDS_MAX", "5"))  # kleine willekeurige pauze
STATE_FILE      = "state.json"  # de-dupe (alleen push bij status-wijziging)

# Debug/snapshot (artifact)
DEBUG_SNAPSHOT  = os.getenv("DEBUG_SNAPSHOT", "0") == "1"     # als 1: sla last_response.html op voor inspectie

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

def url_checks(final_url: str) -> bool:
    """Controleer dat we op het juiste domein/pad zitten (als EXPECTED_* gezet is)."""
    ok = True
    if EXPECTED_HOST:
        host = urlparse(final_url).hostname or ""
        if host.lower() != EXPECTED_HOST.lower():
            print(f"URL check failed: host '{host}' != EXPECTED_HOST '{EXPECTED_HOST}'", file=sys.stderr)
            ok = False
    if ok and EXPECTED_PATH:
        path = urlparse(final_url).path or ""
        if EXPECTED_PATH not in path:
            print(f"URL check failed: path '{path}' mist EXPECTED_PATH '{EXPECTED_PATH}'", file=sys.stderr)
            ok = False
    return ok

def extract_relevant_text(html: str) -> str:
    """Pak ofwel de hele tekst, of (liever) de tekst uit een specifiek element."""
    if CSS_SELECTOR:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(CSS_SELECTOR)
        if not node:
            print(f"CSS selector '{CSS_SELECTOR}' niet gevonden; val terug op hele document.", file=sys.stderr)
            return soup.get_text(separator=" ", strip=True)
        return node.get_text(separator=" ", strip=True)
    # fallback: hele documenttekst
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)

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

    # Snapshot voor inspectie (optioneel)
    if DEBUG_SNAPSHOT:
        try:
            with open("last_response.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("SNAPSHOT_SAVED=1")
        except Exception as e:
            print(f"Snapshot failed: {e}", file=sys.stderr)

    text_full = unescape(html)

    # 0) Niet op loginpagina blijven hangen
    if looks_like_login_page(text_full):
        print("Op loginpagina terechtgekomen; geen alert. (Controleer USERNAME_FIELD/PASSWORD_FIELD/EXTRA_FIELDS_JSON.)")
        print(f"Final URL: {final_url}")
        return 0

    # 1) URL-check (indien ingesteld)
    if not url_checks(final_url):
        print(f"Final URL (mismatch): {final_url}")
        return 0

    # 2) Pagina-check: CONFIRM_TEXT (indien ingesteld)
    lowered = text_full.lower()
    if CONFIRM_TEXT and (CONFIRM_TEXT.lower() not in lowered):
        print(f"CONFIRM_TEXT '{CONFIRM_TEXT}' niet gevonden; geen alert.")
        print(f"Final URL: {final_url}")
        return 0

    # 3) Element-check: kijk gericht in CSS_SELECTOR of het hele document
    relevant_text = extract_relevant_text(text_full)

    # Beschikbaarheid: als de “geen dagen” tekst NIET voorkomt in het relevante stuk, dan lijkt het beschikbaar
    available = (TEXT_TO_FIND not in relevant_text)

    # De-dupe op basis van state.json
    state = load_state()
    prev = state.get("available")

    if available and prev is not True:
        send_telegram("🎉 Er lijken data beschikbaar! Check de site nu.")
        print("Notificatie verstuurd.")

    if (prev is None) or (available != prev):
        save_state({"available": available})
        print("STATE_CHANGED=1")

    # Logging ter controle
    print(f"Status: {'BESCHIKBAAR' if available else 'GEEN'}")
    print(f"Final URL: {final_url}")
    if login_attempted:
        print("Login attempted: yes")
    else:
        print("Login attempted: no")

    # Extra logging: een klein fragment rondom TEXT_TO_FIND of eerste chars van relevant
    snippet = relevant_text[:300].replace("\n", " ")
    print(f"Relevant snippet (first 300 chars): {snippet}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
