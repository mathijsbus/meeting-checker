import os
import requests

# Haalt de waarden uit GitHub Secrets (komt via workflow)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": msg}
    r = requests.post(url, data=data, timeout=10)
    r.raise_for_status()
    print("Bericht verstuurd:", msg)

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID mist")
    send_telegram("🚀 Testbericht van GitHub Actions!")

if __name__ == "__main__":
    main()
