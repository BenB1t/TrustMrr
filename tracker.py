import os
import requests
import json
import time
from datetime import datetime
from html import escape

# --- CONFIGURATION (Pulled from GitHub Secrets) ---
API_KEY = os.environ.get("TRUSTMRR_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "full_revenue_state.json"
DOCS_DIR = "docs"
DASHBOARD_DATA_FILE = os.path.join(DOCS_DIR, "data.json")

MIN_INCREASE_CENTS = 5000  # Ignore growth under $50
MIN_MRR_TO_TRACK = 0       # Set to 50000 to only track startups making $500+ MRR
SLEEP_TIME = 6             # 6 seconds for Standard API keys (10 req/min)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def export_dashboard_data(state):
    os.makedirs(DOCS_DIR, exist_ok=True)
    
    startups = []
    total_mrr_cents = 0
    
    for slug, info in state.items():
        mrr_cents = info.get("mrr", 0)
        if not isinstance(mrr_cents, (int, float)):
            continue
            
        total_mrr_cents += mrr_cents
        startups.append({
            "slug": slug,
            "name": info.get("name", slug),
            "mrr_usd": round(mrr_cents / 100, 2),
            "url": f"https://trustmrr.com/startup/{slug}"
        })
        
    startups.sort(key=lambda x: x["mrr_usd"], reverse=True)
    
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_startups": len(startups),
        "total_mrr_usd": round(total_mrr_cents / 100, 2),
        "startups": startups
    }
    
    with open(DASHBOARD_DATA_FILE, "w") as f:
        json.dump(payload, f, indent=2)
        
    print(f"📊 Dashboard data exported: {DASHBOARD_DATA_FILE}")

def send_notification(name, slug, old_mrr, new_mrr, is_new=False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing Telegram credentials in GitHub Secrets.")
        return
    
    old_mrr_usd = old_mrr / 100
    new_mrr_usd = new_mrr / 100
    safe_name = escape(name)
    
    if is_new:
        text = f"🆕 <b>New Startup Found: {safe_name}</b>\n\n"
        text += f"💰 <b>Current MRR:</b> ${new_mrr_usd:,.2f}\n\n"
    else:
        text = f"🚀 <b>Revenue Spike: {safe_name}</b>\n\n"
        text += f"📉 <b>Previous MRR:</b> ${old_mrr_usd:,.2f}\n"
        text += f"📈 <b>Current MRR:</b> ${new_mrr_usd:,.2f}\n"
        text += f"🔥 <b>Growth:</b> +${new_mrr_usd - old_mrr_usd:,.2f}\n\n"
        
    text += f"🔗 <a href='https://trustmrr.com/startup/{slug}'>View on TrustMRR</a>"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    
    try:
        response = requests.post(url, json=payload)
        if not response.ok:
            print(f"❌ Telegram API Error: {response.text}")
    except Exception as e:
        print(f"❌ Failed to send Telegram message: {e}")

def fetch_all_startups():
    headers = {"Authorization": f"Bearer {API_KEY}"}
    base_url = "https://trustmrr.com/api/v1/startups"
    all_startups = []
    page = 1
    
    print(f"🔍 Starting full database fetch...")
    
    while True:
        params = {"page": page, "limit": 10, "sort": "listed-desc"}
        response = requests.get(base_url, headers=headers, params=params)
        
        if response.status_code == 429:
            reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait_time = max(1, reset_time - int(time.time()) + 2)
            print(f"⚠️ Rate limited. Sleeping for {wait_time} seconds...")
            time.sleep(wait_time)
            continue
            
        if not response.ok:
            print(f"❌ API Error: {response.status_code}")
            break
            
        data = response.json()
        startups = data.get("data", [])
        if not startups: break
            
        all_startups.extend(startups)
        print(f"✅ Page {page} fetched. Total: {len(all_startups)}")
        
        if not data.get("meta", {}).get("hasMore", False): break
        page += 1
        time.sleep(SLEEP_TIME)
        
    return all_startups

def main():
    if not API_KEY:
        print("❌ Missing TRUSTMRR_API_KEY environment variable.")
        return

    state = load_state()
    first_run = len(state) == 0
    
    if first_run:
        print("🌱 First run detected. Building baseline silently to avoid Telegram spam.")

    startups = fetch_all_startups()
    new_count = 0
    growth_count = 0
    
    for startup in startups:
        slug = startup.get("slug")
        name = startup.get("name", "Unknown Startup")
        
        if not slug:
            continue
            
        revenue = startup.get("revenue", {})
        new_mrr = revenue.get("mrr", 0)
        
        if new_mrr < MIN_MRR_TO_TRACK: continue

        if slug not in state:
            if not first_run:
                print(f"🆕 New: {name}")
                send_notification(name, slug, 0, new_mrr, is_new=True)
                new_count += 1
            state[slug] = {"mrr": new_mrr, "name": name}
        else:
            old_mrr = state[slug]["mrr"]
            if not first_run and new_mrr > old_mrr + MIN_INCREASE_CENTS:
                print(f"🚀 Growth: {name}")
                send_notification(name, slug, old_mrr, new_mrr)
                growth_count += 1
            state[slug]["mrr"] = new_mrr
            state[slug]["name"] = name

    save_state(state)
    export_dashboard_data(state)
    
    if first_run:
        print("✅ Baseline built. Dashboard data created. Future runs will send Telegram alerts.")
    else:
        print(f"✅ Done. {new_count} new, {growth_count} growth spikes.")

if __name__ == "__main__":
    main()
