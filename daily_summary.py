import os, sys, io, re, zipfile, requests
import datetime as dt
from zoneinfo import ZoneInfo

# ===== env =====
TOKEN         = os.getenv("GITHUB_TOKEN")
REPO          = os.getenv("GITHUB_REPOSITORY")                # "owner/repo"
# Comma-separated list; bv: "check_daemon.yml" of "check.yml,check_daemon.yml"
WORKFLOW_FILES = [s.strip() for s in os.getenv("WORKFLOW_FILES", "check_daemon.yml").split(",") if s.strip()]

TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT       = os.getenv("TELEGRAM_CHAT_ID")

LOCAL_TZ      = os.getenv("LOCAL_TZ", "Europe/Amsterdam")
LOCAL_HOUR    = int(os.getenv("LOCAL_HOUR", "18"))            # 18:00 lokale tijd

# Marker waarmee we checks herkennen in de daemon-logs:
CHECK_REGEX   = os.getenv("CHECK_REGEX", r"^::group::check ").encode()

HDRS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

def telegram(msg: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TG_CHAT, "text": msg}, timeout=30)
    r.raise_for_status()

def list_runs_since(owner, repo, workflow_file, since_utc):
    runs = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
        r = requests.get(url, headers=HDRS, params={"per_page": 100, "page": page}, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data.get("workflow_runs", [])
        if not items:
            break
        # voeg alle runs toe die binnen het venster vallen
        stop = False
        for it in items:
            created = dt.datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))
            if created >= since_utc:
                runs.append(it)
            else:
                stop = True
        if stop:
            break
        page += 1
        if page > 10:
            break
    return runs

def count_checks_in_run(owner, repo, run_id, regex_bytes):
    """
    Download de logs-zip van een run en tel hoeveel keer CHECK_REGEX voorkomt
    (we loggen elke iteratie met ::group::check ... in de daemon).
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    r = requests.get(url, headers=HDRS, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    total = 0
    for name in z.namelist():
        with z.open(name) as f:
            # lees als bytes en tel regex hits lijn-gebaseerd voor performance
            try:
                for line in f:
                    if line.startswith(regex_bytes):
                        total += 1
            except Exception:
                # fallback: hele file als tekst
                data = z.read(name)
                total += len(re.findall(regex_bytes, data))
    return total

def main():
    # Guards
    if not (TOKEN and REPO and TG_TOKEN and TG_CHAT):
        print("Missing env (GITHUB_TOKEN / GITHUB_REPOSITORY / TELEGRAM_*).", file=sys.stderr)
        return 2

    # Alleen om 18:00 lokale tijd versturen
    local_now = dt.datetime.now(ZoneInfo(LOCAL_TZ))
    if local_now.hour != LOCAL_HOUR:
        print(f"Skip (local time is {local_now.strftime('%Y-%m-%d %H:%M %Z')})")
        return 0

    owner, repo = REPO.split("/", 1)
    now_utc = dt.datetime.now(dt.timezone.utc)
    since_utc = now_utc - dt.timedelta(days=1)

    # Verzamel runs + tel checks uit logs
    grand_total_checks = 0
    wf_summaries = []
    for wf in WORKFLOW_FILES:
        runs = list_runs_since(owner, repo, wf, since_utc)
        run_ids = [r["id"] for r in runs]
        checks = 0
        for rid in run_ids:
            try:
                checks += count_checks_in_run(owner, repo, rid, CHECK_REGEX)
            except requests.HTTPError as e:
                print(f"Warn: failed to read logs for run {rid} ({e})", file=sys.stderr)
        grand_total_checks += checks
        wf_summaries.append((wf, len(run_ids), checks))

    # Bericht opbouwen
    window = f"{since_utc.isoformat(timespec='seconds')} → {now_utc.isoformat(timespec='seconds')} (UTC)"
    lines = [
        "📊 Dagelijkse statusupdate",
        f"Periode: {window}",
        f"Totale checks (uit logs): {grand_total_checks}"
    ]
    for (wf, runs_count, checks) in wf_summaries:
        lines.append(f"• {wf}: {runs_count} runs, {checks} checks")
    msg = "\n".join(lines)

    telegram(msg)
    print("Summary sent.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
