import os, sys, time, json, re
from html import unescape
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# ====== ENV / Config ======
LOGIN_URL       = os.getenv("LOGIN_URL")
TARGET_URL      = os.getenv("TARGET_URL")
USERNAME        = os.getenv("SITE_USERNAME")
PASSWORD        = os.getenv("SITE_PASSWORD")
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")

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

# Optioneel voor Playwright-login (als de standaard name= velden niet werken)
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

# kleine beleefdheids-pauze (jitter)
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

# ---------- Playwright (JS-rendered) ----------
def fetch_via_playwright():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    png_written = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        # probeer direct target
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        if "login" in page.url.lower() or page.locator('input[type="password"]').count() > 0:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            if LOGIN_USERNAME_SELECTOR and LOGIN_PASSWORD_SELECTOR:
                page.fill(LOGIN_USERNAME_SELECTOR, USERNAME)
                page.fill(LOGIN_PASSWORD_SELECTOR, PASSWORD)
                if LOGIN_SUBMIT_SELECTOR:
                    page.click(LOGIN_SUBMIT_SELECTOR)
                else:
                    page.keyboard.press("Enter")
            else:
                page.fill(f'input[name="{USERNAME_FIELD}"]', USERNAME)
                page.fill(f'input[name="{PASSWORD_FIELD}"]', PASSWORD)
                page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=60000)
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

        # indien selector meegegeven: wacht er even op
        sel_text = ""
        if CSS_SELECTOR:
            try:
                page.wait_for_selector(CSS_SELECTOR, timeout=15000)
                sel_text = page.text_content(CSS_SELECTOR) or ""
            except PWTimeout:
                sel_text = ""

        html = page.content()
        final_url = page.url

        if DEBUG_SNAPSHOT:
            try:
                page.screenshot(path="last_response.png", full_page=True)
                png_written = True
            except Exception:
                pass

        browser.close()
        return html, final_url, sel_text, png_written

def extract_relevant_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"available": None}

def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

# ===================== main =====================
def main():
    for req in ("LOGIN_URL","TARGET_URL","SITE_USERNAME","SITE_PASSWORD","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"):
        if not os.getenv(req):
            print(f"Missing env: {req}", file=sys.stderr); return 2

    # haal pagina
    selected_text = ""
    png_written = False
    if USE_PLAYWRIGHT:
        html, final_url, selected_text, png_written = fetch_via_playwright()
    else:
        html, final_url = fetch_via_requests()

    # Altijd HTML-snapshot wegschrijven als DEBUG aan staat
    save_snapshot_files(html, png_written)

    full_text = unescape(html)

    # login-achtige content? dan stoppen
    if looks_like_login_page(full_text):
        print("Op loginpagina / niet-ingeladen content; geen alert.")
        print(f"Final URL: {final_url}")
        return 0

    # URL + pagina-confirmaties
    if not url_checks(final_url):
        print(f"Final URL (mismatch): {final_url}")
        return 0
    if CONFIRM_TEXT and normalize(CONFIRM_TEXT) not in normalize(full_text):
        print(f"CONFIRM_TEXT '{CONFIRM_TEXT}' niet gevonden; geen alert.")
        print(f"Final URL: {final_url}")
        return 0

    # bepaal “relevant text”
    if CSS_SELECTOR and selected_text:
        relevant = selected_text
    else:
        relevant = extract_relevant_text(full_text)

    # availability met normalisatie (spaties/case negeren)
    available = normalize(TEXT_TO_FIND) not in normalize(relevant)

    # de-dupe
    state = load_state()
    prev = state.get("available")
    if available and prev is not True:
        send_telegram("🎉 Er lijken data beschikbaar! Check de site nu.")
        print("Notificatie verstuurd.")
    if (prev is None) or (available != prev):
        save_state({"available": available})
        print("STATE_CHANGED=1")

    print(f"Status: {'BESCHIKBAAR' if available else 'GEEN'}")
    print(f"Final URL: {final_url}")
    print("Relevant snippet (first 300 chars):", (relevant or "")[:300].replace("\n"," "))
    if DEBUG_SNAPSHOT:
        print("SNAPSHOT_DEBUG=on")
    return 0

if __name__ == "__main__":
    sys.exit(main())
