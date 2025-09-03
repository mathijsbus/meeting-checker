import os, sys, time, datetime as dt, requests

TOKEN   = os.getenv("GITHUB_TOKEN")
REPO    = os.getenv("GITHUB_REPOSITORY")  # "owner/repo"
WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", "check.yml")  # onze checker workflow
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

def telegram(msg: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TG_CHAT, "text": msg}, timeout=20)
    r.raise_for_status()

def list_runs_since(owner, repo, workflow_file, since_utc):
    runs = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
        r = requests.get(url, headers=HEADERS, params={"per_page": 100, "page": page}, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data.get("workflow_runs", [])
        if not items:
            break
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
        if page > 10:  # safety
            break
    return runs

def main():
    if not (TOKEN and REPO and TG_TOKEN and TG_CHAT):
        print("Missing env (GITHUB_TOKEN / REPO / TELEGRAM vars)", file=sys.stderr)
        sys.exit(2)
    owner, repo = REPO.split("/", 1)
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=1)

    runs = list_runs_since(owner, repo, WORKFLOW_FILE, since)
    total = len(runs)
    success = sum(1 for r in runs if r.get("conclusion") == "success")
    failure = sum(1 for r in runs if r.get("conclusion") not in (None, "success"))
    # (queued/in_progress hebben conclusion None; die tellen we niet als success/failure)

    # Laatste run-info (indien aanwezig)
    last_txt = "geen runs"
    if runs:
        last = max(runs, key=lambda r: r["created_at"])
        last_conc = last.get("conclusion") or last.get("status")
        last_time = last["created_at"].replace("T"," ").replace("Z"," UTC")
        last_txt = f"{last_conc} @ {last_time}"

    # Bericht
    window = f"{since.isoformat(timespec='seconds')} → {now.isoformat(timespec='seconds')}"
    msg = (
        "📊 Dagelijkse statusupdate\n"
        f"Periode (UTC): {window}\n"
        f"Workflow: {WORKFLOW_FILE}\n"
        f"Totaal checks: {total}\n"
        f"✅ Succes: {success}\n"
        f"❌ Fout: {failure}\n"
        f"Laatste run: {last_txt}"
    )
    telegram(msg)
    print("Summary sent.")

if __name__ == "__main__":
    main()
