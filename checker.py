import os, sys, time, json, re, traceback
from html import unescape
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# ====== ENV ======
LOGIN_URL       = os.getenv("LOGIN_URL")
TARGET_URL      = os.getenv("TARGET_URL")
USERNAME        = os.getenv("SITE_USERNAME")
PASSWORD        = os.getenv("SITE_PASSWORD")

# requests-fallback (alleen als de site server-side form heeft)
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "email")
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")

TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
CONFIRM_TEXT    = (os.getenv("CONFIRM_TEXT") or "").strip()
EXPECTED_HOST   = (os.getenv("EXPECTED_HOST") or "").strip()
EXPECTED_PATH   = (os.getenv("EXPECTED_PATH") or "").strip()
CSS_SELECTOR    = (os.getenv("CSS_SELECTOR") or "").strip()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")
USER_AGENT      = os.getenv("USER_AGENT","Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEBUG_SNAPSHOT  = os.getenv("DEBUG_SNAPSHOT","0") == "1"
USE_PLAYWRIGHT  = os.getenv("USE_PLAYWRIGHT","1") == "1"  # default aan

# optionele overrides (Variables)
LOGIN_USERNAME_SELECTOR = os.getenv("LOGIN_USERNAME_SELECTOR")  # bv input[name="email"]
LOGIN_PASSWORD_SELECTOR = os.getenv("LOGIN_PASSWORD_SELECTOR")  # bv input[name="password"]
LOGIN_SUBMIT_SELECTOR   = os.getenv("LOGIN_SUBMIT_SELECTOR")    # bv button[type="submit"]

def _json_env(name, default):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default

EXTRA_FIELDS = _json_env("EXTRA_FIELDS_JSON", {})

# ====== helpers ======
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHATID:
        print("Telegram not configured", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHATID, "text": text}, timeout=20).raise_for_status()
    except Exception as e:
        print(f"Telegram error: {e}", file=sys.stderr)

def looks_like_login_page(html: str) -> bool:
    lower = html.lower()
    return any(w in lower for w in ("wachtwoord","password","inloggen","aanmelden","login",'type="password"'))

def url_checks(final_url: str) -> bool:
    ok = True
    host = (urlparse(final_url).hostname or "").lower()
    if EXPECTED_HOST and host != EXPECTED_HOST.lower():
        print(f"URL host mismatch: '{host}' vs '{EXPECTED_HOST}'", file=sys.stderr); ok = False
    path = urlparse(final_url).path or ""
    if ok and EXPECTED_PATH and EXPECTED_PATH not in path:
        print(f"URL path mismatch: '{path}' mist '{EXPECTED_PATH}'", file=sys.stderr); ok = False
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
    if not DEBUG_SNAPSHOT: return
    try:
        with open("last_response.html","w",encoding="utf-8") as f:
            f.write(html or "")
        print("SNAPSHOT_SAVED=1")
        if png_exists:
            print("SNAPSHOT_PNG_PRESENT=1")
    except Exception as e:
        print(f"Snapshot failed: {e}", file=sys.stderr)

# ====== requests fallback (werkt alleen als de site server-side rendered login heeft) ======
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

# ====== Playwright (JS) ======
def fetch_via_playwright():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    def first_visible(page, selector):
        loc = page.locator(selector)
        try: n = loc.count()
        except Exception: return None
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible(): return el
            except Exception: pass
        return None

    def fill_visible(page, selectors, value, label):
        for sel in selectors:
            el = first_visible(page, sel)
            if el:
                try: el.click(timeout=1500)
                except Exception: pass
                try: el.fill("", timeout=1500)
                except Exception: pass
                try: el.type(value, delay=10, timeout=4000)
                except Exception: el.fill(value, timeout=4000)
                print(f"Filled {label}: {sel}")
                return el
        return None

    def submit_enabled(page):
        try:
            btn = first_visible(page, 'button[type="submit"], input[type="submit"]')
            if not btn: return False
            try: return btn.is_enabled()
            except Exception: return True
        except Exception:
            return False

    png_written = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # 1) Ga altijd eerst naar TARGET
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)

        # 2) Login nodig?
        need_login = ("login" in page.url.lower()) or (page.locator('input[type="password"]').count() > 0)
        if need_login:
            print("Login required → opening LOGIN_URL …")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            # Kandidaten (eerst jouw overrides)
            user_cands = [LOGIN_USERNAME_SELECTOR] if LOGIN_USERNAME_SELECTOR else []
            user_cands += [
                ':has(label:has-text("Emailadres")) input',
                ':has(label:has-text("E-mail")) input',
                'input[name="email"]','input[type="email"]','input[id*="email" i]',
                'input[name="username"]','input[id*="user" i]','input[type="text"]'
            ]
            pass_cands = [LOGIN_PASSWORD_SELECTOR] if LOGIN_PASSWORD_SELECTOR else []
            pass_cands += [
                ':has(label:has-text("Wachtwoord")) input',
                'input[name="password"]','input[type="password"]','input[id*="pass" i]'
            ]
            submit_cands = [LOGIN_SUBMIT_SELECTOR] if LOGIN_SUBMIT_SELECTOR else []
            submit_cands += [
                'button[type="submit"]','input[type="submit"]',
                'button:has-text("Inloggen")','text=Inloggen'
            ]

            uel = fill_visible(page, user_cands, USERNAME, "email/username")
            pel = fill_visible(page, pass_cands, PASSWORD, "password")

            if not uel or not pel:
                raise RuntimeError("Kon zichtbare velden niet vinden.")

            # Trigger validatie (blur/tab/enter)
            try:
                pel.blur()
                page.keyboard.press("Tab")
                time.sleep(0.2)
            except Exception: pass

            # alleen klikken als enabled
            clicked = False
            for sel in submit_cands:
                btn = first_visible(page, sel)
                if btn:
                    if submit_enabled(page):
                        try:
                            btn.click(timeout=3000)
                            print(f"Clicked submit: {sel}")
                            clicked = True
                            break
                        except Exception as e:
                            print(f"Click failed on {sel}: {e}")
                    else:
                        print(f"Submit found but DISABLED: {sel}")
            if not clicked:
                # probeer Enter
                try:
                    page.keyboard.press("Enter")
                    print("Pressed Enter to submit.")
                except Exception: pass

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass

            # Screenshot na submit
            if DEBUG_SNAPSHOT:
                try:
                    page.screenshot(path="after_submit.png", full_page=True)
                    print("Screenshot saved: after_submit.png")
                except Exception: pass

            # Altijd terug naar TARGET (exact 1x), geen loop
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)

        # 3) Content ophalen
        sel_text = ""
        if CSS_SELECTOR:
            try:
                page.wait_for_selector(CSS_SELECTOR, timeout=15000)
                el = page.locator(CSS_SELECTOR).first
                if el and el.is_visible():
                    sel_text = el.text_content() or ""
                    print(f"Captured CSS_SELECTOR content (len={len(sel_text)}).")
            except Exception:
                print("CSS_SELECTOR not found within timeout.")

        html = page.content()
        final_url = page.url

        if DEBUG_SNAPSHOT:
            try:
                page.screenshot(path="last_response.png", full_page=True)
                png_written = True
                print("Screenshot saved: last_response.png")
            except Exception as e:
                print(f"Screenshot failed: {e}", file=sys.stderr)

        browser.close()
        return html, final_url, sel_text, png_written

# ====== extract & state ======
def extract_relevant_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)

def load_state():
    try:
        with open("state.json","r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"available": None}

def save_state(st):
    with open("state.json","w",encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

# ====== main ======
def main():
    for req in ("LOGIN_URL","TARGET_URL","SITE_USERNAME","SITE_PASSWORD","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"):
        if not os.getenv(req):
            print(f"Missing env: {req}", file=sys.stderr); return 2

    if USE_PLAYWRIGHT:
        html, final_url, selected_text, png_written = fetch_via_playwright()
    else:
        html, final_url = fetch_via_requests(); selected_text=""; png_written=False

    save_snapshot_files(html, png_written)
    full_text = unescape(html)

    # als we nog op login staan → stoppen (geen loop) met duidelijke melding
    if looks_like_login_page(full_text):
        print("Op loginpagina gebleven; login is niet gelukt (submit disabled of geweigerd).")
        print(f"Final URL: {final_url}")
        return 0

    if not url_checks(final_url):
        print(f"Final URL (mismatch): {final_url}")
        return 0
    if CONFIRM_TEXT and normalize(CONFIRM_TEXT) not in normalize(full_text):
        print(f"CONFIRM_TEXT '{CONFIRM_TEXT}' niet gevonden; geen alert.")
        print(f"Final URL: {final_url}")
        return 0

    relevant = selected_text if (CSS_SELECTOR and selected_text) else extract_relevant_text(full_text)
    available = normalize(TEXT_TO_FIND) not in normalize(relevant)

    state = load_state()
    prev = state.get("available")
    if available and prev is not True:
        send_telegram("🎉 Er lijken dagen beschikbaar! Check de site nu.")
        print("Notificatie verstuurd.")
    if (prev is None) or (available != prev):
        save_state({"available": available})
        print("STATE_CHANGED=1")

    print(f"Status: {'BESCHIKBAAR' if available else 'GEEN'}")
    print(f"Final URL: {final_url}")
    print("Relevant snippet (first 300 chars):", (relevant or "")[:300].replace("\n"," "))
    if DEBUG_SNAPSHOT: print("SNAPSHOT_DEBUG=on")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("UNCAUGHT EXCEPTION:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
