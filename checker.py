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

# alleen voor requests-fallback
USERNAME_FIELD  = os.getenv("USERNAME_FIELD", "username")
PASSWORD_FIELD  = os.getenv("PASSWORD_FIELD", "password")

TEXT_TO_FIND    = os.getenv("TEXT_TO_FIND", "Geen dagen gevonden.")
CONFIRM_TEXT    = (os.getenv("CONFIRM_TEXT") or "").strip()
EXPECTED_HOST   = (os.getenv("EXPECTED_HOST") or "").strip()
EXPECTED_PATH   = (os.getenv("EXPECTED_PATH") or "").strip()
CSS_SELECTOR    = (os.getenv("CSS_SELECTOR") or "").strip()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID")
USER_AGENT      = os.getenv("USER_AGENT","Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

JITTER_MAX      = int(os.getenv("JITTER_SECONDS_MAX", "5"))
STATE_FILE      = "state.json"
DEBUG_SNAPSHOT  = os.getenv("DEBUG_SNAPSHOT", "0") == "1"
USE_PLAYWRIGHT  = os.getenv("USE_PLAYWRIGHT", "0") == "1"

# optionele overrides via Variables
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

if JITTER_MAX > 0:
    import random
    time.sleep(random.randint(0, JITTER_MAX))

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHATID, "text": text}, timeout=20)
    r.raise_for_status()

def looks_like_login_page(html: str) -> bool:
    lower = html.lower()
    return any(w in lower for w in ("wachtwoord","password","inloggen","aanmelden","login",'type="password"'))

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
    if not DEBUG_SNAPSHOT: return
    try:
        with open("last_response.html","w",encoding="utf-8") as f:
            f.write(html or "")
        print("SNAPSHOT_SAVED=1")
        if png_exists: print("SNAPSHOT_PNG_PRESENT=1")
    except Exception as e:
        print(f"Snapshot failed: {e}", file=sys.stderr)

# -------- requests fallback --------
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

# -------- Playwright helpers --------
def handle_consents(page):
    candidates = [
        'button:has-text("Akkoord")','button:has-text("Accepteren")','button:has-text("Alles accepteren")',
        '#onetrust-accept-btn-handler','button#onetrust-accept-btn-handler',
        'button[aria-label*="accept" i]','text=Akkoord','text=Accepteren'
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1200)
                print(f"Consent clicked: {sel}")
                time.sleep(0.2)
        except Exception:
            pass

def first_visible(page, selector):
    loc = page.locator(selector)
    try: n = loc.count()
    except Exception: return None
    for i in range(n):
        item = loc.nth(i)
        try:
            if item.is_visible(): return item
        except Exception: continue
    return None

def fill_visible(page, selectors, value, label):
    for sel in selectors:
        el = first_visible(page, sel)
        if el:
            try: el.click(timeout=2000)
            except Exception: pass
            try: el.fill("", timeout=2000)
            except Exception: pass
            try: el.type(value, delay=20, timeout=4000)
            except Exception: el.fill(value, timeout=4000)
            try:
                iv = el.input_value()
                print(f"Filled visible {label}: {sel} (len={len(iv) if iv else 0})")
            except Exception:
                print(f"Filled visible {label}: {sel}")
            return sel, el
    return None, None

def click_visible(page, selectors, label):
    for sel in selectors:
        el = first_visible(page, sel)
        if el:
            try:
                if hasattr(el, "is_enabled") and not el.is_enabled():
                    print(f"{label} element found but DISABLED: {sel}")
                el.click(timeout=3000)
                print(f"Clicked visible {label}: {sel}")
                return sel, el
            except Exception as e:
                print(f"Click failed on {label} {sel}: {e}")
                continue
    return None, None

def collect_login_errors(page):
    texts = []
    cands = [
        '[role="alert"]','.MuiAlert-root','.alert','.error','.invalid-feedback',
        '.help-block','.MuiFormHelperText-root','span[role="alert"]',
        'text=/ongeldig|incorrect|fout|verkeerd|combina/i'
    ]
    for sel in cands:
        try:
            loc = page.locator(sel)
            if loc.count():
                for i in range(min(3, loc.count())):
                    t = (loc.nth(i).inner_text() or "").strip()
                    if t: texts.append(f"{sel}: {t}")
        except Exception:
            pass
    return texts

def submit_even_if_disabled(page, password_el):
    """Probeer Enter/blur en eventual JS form.submit() als knop disabled blijft."""
    try:
        # blur/validatie
        password_el.blur()
        page.keyboard.press("Tab")
        time.sleep(0.2)
        page.keyboard.press("Enter")
        print("Pressed Enter on password/after blur.")
        time.sleep(0.5)
    except Exception:
        pass

    # check submit status en form submit via JS
    try:
        submit = first_visible(page, 'button[type="submit"], input[type="submit"]')
        if submit and hasattr(submit, "is_enabled") and not submit.is_enabled():
            print("Submit still disabled → trying JS form.submit()")
            page.evaluate("""
                () => {
                  const btn = document.querySelector('button[type="submit"], input[type="submit"]');
                  const form = btn ? btn.closest('form') : document.querySelector('form');
                  if (form) form.submit();
                }
            """)
            time.sleep(0.6)
    except Exception as e:
        print(f"form.submit() attempt failed: {e}")

def captcha_present(page) -> bool:
    try:
        if page.frame_locator('iframe[src*="recaptcha"]').count() > 0:
            return True
        if page.locator('div.g-recaptcha').count() > 0:
            return True
    except Exception:
        pass
    return False

# -------- Playwright flow --------
def fetch_via_playwright():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    png_written = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        login_responses = []
        def on_response(resp):
            try:
                if "login" in resp.url.lower() and resp.request.method.upper() in ("POST","PUT"):
                    body = ""
                    try: body = resp.text()[:800]
                    except Exception: pass
                    login_responses.append((resp.status, resp.url, body))
            except Exception: pass
        page.on("response", on_response)

        # 1) Altijd eerst target
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        handle_consents(page)

        def on_login_page() -> bool:
            return ("login" in page.url.lower()) or (page.locator('input[type="password"]').count() > 0)

        # 2) Indien nodig: één loginpoging
        if on_login_page():
            print("Login required → open LOGIN_URL once, fill & submit …")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            handle_consents(page)

            # Captcha detectie?
            if captcha_present(page):
                print("CAPTCHA_DETECTED=1 — kan niet automatisch inloggen.")
                html = page.content()
                if DEBUG_SNAPSHOT:
                    try:
                        page.screenshot(path="after_submit.png", full_page=True)
                        print("Screenshot saved: after_submit.png")
                    except Exception: pass
                return html, page.url, "", False

            user_candidates = []
            if LOGIN_USERNAME_SELECTOR: user_candidates.append(LOGIN_USERNAME_SELECTOR)
            user_candidates += [
                ':has(label:has-text("Emailadres")) input',
                ':has(label:has-text("E-mail")) input',
                'input[name="email"]','input[type="email"]','input[id*="email" i]',
                'input[name="username"]','input[id*="user" i]','input[type="text"]'
            ]
            pass_candidates = []
            if LOGIN_PASSWORD_SELECTOR: pass_candidates.append(LOGIN_PASSWORD_SELECTOR)
            pass_candidates += [
                ':has(label:has-text("Wachtwoord")) input',
                'input[name="password"]','input[type="password"]','input[id*="pass" i]'
            ]
            submit_candidates = []
            if LOGIN_SUBMIT_SELECTOR: submit_candidates.append(LOGIN_SUBMIT_SELECTOR)
            submit_candidates += [
                'button[type="submit"]','input[type="submit"]',
                'button:has-text("Inloggen")','button:has-text("Aanmelden")',
                'text=Inloggen','text=Aanmelden'
            ]

            sel_user, _ = fill_visible(page, user_candidates, USERNAME, "username")
            sel_pass, pass_el = fill_visible(page, pass_candidates, PASSWORD, "password")
            if not sel_user or not sel_pass:
                raise RuntimeError("Could not find visible username/password fields.")

            sub_sel, sub_el = click_visible(page, submit_candidates, "submit")
            if not sub_sel or (sub_el and hasattr(sub_el, "is_enabled") and not sub_el.is_enabled()):
                print("Submit click not possible or disabled — trying Enter/JS.")
                if pass_el:
                    submit_even_if_disabled(page, pass_el)
                else:
                    page.keyboard.press("Enter"); print("Pressed Enter (no pass_el).")

            try: page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout: pass

            if DEBUG_SNAPSHOT:
                try:
                    page.screenshot(path="after_submit.png", full_page=True)
                    print("Screenshot saved: after_submit.png")
                except Exception: pass

            # 3) Terug naar target (exact één keer)
            handle_consents(page)
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            handle_consents(page)

        # 4) Content ophalen
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

        # cookie-namen loggen
        try:
            names = [c.get("name","") for c in context.cookies()]
            if names: print("COOKIE_NAMES_SET:", ", ".join(names[:20]))
        except Exception:
            pass

        if DEBUG_SNAPSHOT:
            try:
                page.screenshot(path="last_response.png", full_page=True)
                png_written = True
                print("Screenshot saved: last_response.png")
            except Exception as e:
                print(f"Screenshot failed: {e}", file=sys.stderr)

        # Als we nog op login zitten → log errors en responses
        if "login" in (final_url or "").lower():
            errs = collect_login_errors(page)
            if errs:
                print("LOGIN_ERRORS_DETECTED:")
                for e in errs: print(f"- {e}")
            if login_responses:
                print("LOGIN_HTTP_RESPONSES:")
                for st,u,b in login_responses[-3:]:
                    print(f"- {st} {u}\n  BODY_SNIPPET: {(b or '').strip()[:300]}")
            print("Login failed once; not retrying to avoid loops.")

        browser.close()
        return html, final_url, sel_text, png_written

# -------- extraction & state --------
def extract_relevant_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)

def load_state():
    try:
        with open(STATE_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"available": None}

def save_state(st):
    with open(STATE_FILE,"w",encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

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

    if looks_like_login_page(full_text):
        print("Op loginpagina / niet-ingeladen content; geen alert.")
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
        send_telegram("🎉 Er lijken data beschikbaar! Check de site nu.")
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
