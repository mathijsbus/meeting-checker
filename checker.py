import os, sys, time, json, re, traceback
from html import unescape
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# ====== ENV / Config ======
LOGIN_URL       = os.getenv("LOGIN_URL")
TARGET_URL      = os.getenv("TARGET_URL")
USERNAME        = os.getenv("SITE_USERNAME")
PASSWORD        = os.getenv("SITE_PASSWORD")
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")  # alleen fallback voor requests-flow
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")  # idem

TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
CONFIRM_TEXT    = (os.getenv("CONFIRM_TEXT") or "").strip()

EXPECTED_HOST   = (os.getenv("EXPECTED_HOST") or "").strip()
EXPECTED_PATH   = (os.getenv("EXPECTED_PATH") or "").strip()
CSS_SELECTOR    = (os.getenv("CSS_SELECTOR") or "").strip()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")
USER_AGENT      = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

JITTER_MAX      = int(os.getenv("JITTER_SECONDS_MAX", "5"))
STATE_FILE      = "state.json"
DEBUG_SNAPSHOT  = os.getenv("DEBUG_SNAPSHOT", "0") == "1"
USE_PLAYWRIGHT  = os.getenv("USE_PLAYWRIGHT", "0") == "1"

# Optionele, expliciete selectors (kun je in Variables zetten als je ze weet)
LOGIN_USERNAME_SELECTOR = os.getenv("LOGIN_USERNAME_SELECTOR")  # bv: input[name="email"]
LOGIN_PASSWORD_SELECTOR = os.getenv("LOGIN_PASSWORD_SELECTOR")  # bv: input[type="password"]
LOGIN_SUBMIT_SELECTOR   = os.getenv("LOGIN_SUBMIT_SELECTOR")    # bv: button[type="submit"]

def _json_env(name, default):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default

EXTRA_FIELDS = _json_env("EXTRA_FIELDS_JSON", {})  # voor requests-flow indien nodig

# Jitter
if JITTER_MAX > 0:
    import random
    time.sleep(random.randint(0, JITTER_MAX))

# ====== helpers ======
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHATID, "text": text}, timeout=20)
    r.raise_for_status()

def looks_like_login_page(html: str) -> bool:
    lower = html.lower()
    return any(w in lower for w in ("wachtwoord", "password", "inloggen", "aanmelden", "login", 'type="password"'))

def url_checks(final_url: str) -> bool:
    ok = True
    if EXPECTED_HOST:
        host = (urlparse(final_url).hostname or "").lower()
        if host != EXPECTED_HOST.lower():
            print(f"URL check failed host: '{host}' != '{EXPECTED_HOST}'", file=sys.stderr); ok = False
    if ok and EXPECTED_PATH:
        path = urlparse(final_url).path or ""
        if EXPECTED_PATH not in path:
            print(f"URL check failed path: '{path}' mist '{EXPECTED_PATH}'", file=sys.stderr); ok = False
    return ok

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()

def find_csrf(html: str):
    pats = [
        r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
        r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']',
        r'name=["\']__requestverificationtoken["\']\s+value=["\']([^"\']+)["\']',
        r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
    ]
    lower = html.lower()
    for p in pats:
        m = re.search(p, lower, re.I)
        if m: return m.group(1)
    return None

def save_snapshot_files(html: str, png_exists: bool):
    """Sla altijd last_response.html op als DEBUG_SNAPSHOT=1."""
    if not DEBUG_SNAPSHOT:
        return
    try:
        with open("last_response.html", "w", encoding="utf-8") as f:
            f.write(html or "")
        print("SNAPSHOT_SAVED=1")
        if png_exists:
            print("SNAPSHOT_PNG_PRESENT=1")
    except Exception as e:
        print(f"Snapshot failed: {e}", file=sys.stderr)

# ---------- requests (non-JS) fallback ----------
def fetch_via_requests():
    def safe_get(sess, url):
        r = sess.get(url, timeout=25, allow_redirects=True); r.raise_for_status(); return r
    with requests.Session() as s:
        s.headers.update({"User-Agent": USER_AGENT})
        try:
            r = safe_get(s, TARGET_URL)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401,403):
                r = s.get(LOGIN_URL, timeout=25); r.raise_for_status()
                token = find_csrf(r.text)
                fields = dict(EXTRA_FIELDS)
                if token and not any(k in fields for k in ("csrf_token","_token","__requestverificationtoken","csrfmiddlewaretoken")):
                    fields["csrf_token"] = token
                payload = {USERNAME_FIELD: USERNAME, PASSWORD_FIELD: PASSWORD, **fields}
                r = s.post(LOGIN_URL, data=payload, timeout=25); r.raise_for_status()
                r = safe_get(s, TARGET_URL)
            else:
                raise
        return r.text, r.url

# ---------- Playwright helpers ----------
def try_fill(page, selectors, value, timeout=3000):
    """Probeer meerdere selectors één voor één te vullen; return de gebruikte selector of None."""
    for sel in selectors:
        try:
            page.fill(sel, value, timeout=timeout)
            print(f"Filled selector: {sel}")
            return sel
        except Exception:
            continue
    return None

def try_click(page, selectors, timeout=3000):
    for sel in selectors:
        try:
            page.click(sel, timeout=timeout)
            print(f"Clicked selector: {sel}")
            return sel
        except Exception:
            continue
    return None

# ---------- Playwright (JS-rendered) ----------
def fetch_via_playwright():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    png_written = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # 1) Probeer meteen naar target
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

        # 2) Indien login nodig, ga naar loginpagina en log in met robuuste selectors
        needs_login = ("login" in page.url.lower()) or (page.locator('input[type="password"]').count() > 0)
        if needs_login:
            print("Detected login, navigating to LOGIN_URL …")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            # Kandidaten (eerst jouw expliciete, dan veelvoorkomende varianten)
            user_candidates = [LOGIN_USERNAME_SELECTOR] if LOGIN_USERNAME_SELECTOR else []
            user_candidates += [
