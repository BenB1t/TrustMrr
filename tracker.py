

import os
import requests
import json
import time
from datetime import datetime, timezone
from html import escape

# --- CONFIGURATION (Pulled from GitHub Secrets) ---
API_KEY = os.environ.get("TRUSTMRR_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "full_revenue_state.json"
DOCS_DIR = "docs"
DASHBOARD_DATA_FILE = os.path.join(DOCS_DIR, "data.json")

# --- THE RUBRIC, ENCODED ---
MIN_GROWTH_PCT = 15.0        # Min relative MRR jump between runs to alert
MIN_GROWTH_CENTS = 2500      # ...and min absolute jump (+$25) to kill noise
MIN_NEW_MRR_CENTS = 50000    # New listings need >= $500 MRR to alert at all
HOT_MAX_AGE_MONTHS = 18      # Young + growing = HOT
FLATLINE_AGE_MONTHS = 24     # Older than this...
FLATLINE_MRR_CENTS = 200000  # ...and under $2k MRR = market verdict. Never alert.
ALERT_COOLDOWN_DAYS = 3      # Don't re-alert the same startup within 3 days
HISTORY_LIMIT = 90           # Snapshots kept per startup
SLEEP_TIME = 6
PAGE_LIMIT = 100             # If the API errors with this, set back to 10


# ---------------------------------------------------------------
# ⚠️ BEFORE DEPLOYING: verify field names against the real API.
# Run this once locally:
#
#   import requests, json, os
#   r = requests.get("https://trustmrr.com/api/v1/startups",
#       headers={"Authorization": f"Bearer {os.environ['TRUSTMRR_API_KEY']}"},
#       params={"page": 1, "limit": 1})
#   print(json.dumps(r.json()["data"][0], indent=2))
#
# Then fix the key names inside extract_profile() to match.
# ---------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def parse_date(value):
    """Accept ISO strings, 'October 2021', or unix timestamps (s/ms)."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%B %Y"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None

def age_in_months(founded_dt):
    if not founded_dt:
        return None
    days = (datetime.now(timezone.utc) - founded_dt).days
    return round(days / 30.44, 1)

def extract_profile(startup):
    """Pull rubric fields out of the raw API object, defensively.
    Fix key names here after inspecting the real API response."""
    revenue = startup.get("revenue", {}) or {}
    sale = startup.get("sale", {}) or {}

    asking = startup.get("askingPrice") or sale.get("askingPrice")
    for_sale = bool(
        startup.get("forSale")
        or startup.get("isForSale")
        or asking
        or sale.get("listed")
    )

    return {
        "slug": startup.get("slug"),
        "name": startup.get("name", "Unknown"),
        "mrr": revenue.get("mrr", 0) or 0,
        "subs": revenue.get("activeSubscriptions") or startup.get("activeSubscriptions"),
        "founded": startup.get("foundedAt") or startup.get("founded"),
        "for_sale": for_sale,
        "asking_price": asking,
        "category": startup.get("category") or startup.get("market"),
    }

def evaluate(profile, age_months, old_mrr):
    """The rubric. Returns (verdict, flags).
    verdict in {'HOT', 'GROWTH', 'NEW', None}."""
    new_mrr = profile["mrr"]
    flags = []
    if profile["for_sale"]:
        flags.append("for_sale")

    # KILL: flatlined — old and small = market verdict, never alert
    if (age_months is not None
            and age_months > FLATLINE_AGE_MONTHS
            and new_mrr < FLATLINE_MRR_CENTS):
        return None, flags

    # New listing: only alert if it already has real traction
    if old_mrr <= 0:
        if new_mrr >= MIN_NEW_MRR_CENTS:
            return "NEW", flags
        return None, flags

    # Existing startup: meaningful relative + absolute growth
    delta = new_mrr - old_mrr
    pct = (delta / old_mrr) * 100
    if pct >= MIN_GROWTH_PCT and delta >= MIN_GROWTH_CENTS:
        if age_months is not None and age_months <= HOT_MAX_AGE_MONTHS:
            return "HOT", flags
        return "GROWTH", flags

    return None, flags

def on_cooldown(info):
    last = (info or {}).get("last_alert_t")
    return bool(last) and (time.time() - last) < ALERT_COOLDOWN_DAYS * 86400

def send_notification(profile, age_months, old_mrr, verdict, flags):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing Telegram credentials in GitHub Secrets.")
        return

    new_mrr = profile["mrr"]
    emoji = {"HOT": "🔥", "GROWTH": "🚀", "NEW": "🆕"}.get(verdict, "📈")
    lines = [f"{emoji} <b>{escape(profile['name'])}</b> — {verdict}"]

    if old_mrr > 0:
        delta = new_mrr - old_mrr
        pct = (delta / old_mrr) * 100
        lines.append(
            f"💰 ${old_mrr/100:,.0f} → ${new_mrr/100:,.0f} MRR "
            f"(+${delta/100:,.0f}, +{pct:.0f}%)"
        )
    else:
        lines.append(f"💰 MRR: ${new_mrr/100:,.0f}")

    if age_months is not None:
        velocity = (new_mrr / 100) / max(age_months, 1)
        lines.append(f"📅 {age_months:.0f} months old · velocity ${velocity:,.0f}/mo")

    if profile.get("subs"):
        arpu = (new_mrr / 100) / profile["subs"]
        lines.append(f"👥 {profile['subs']} subs · ${arpu:,.0f} ARPU")

    if "for_sale" in flags:
        lines.append("🏷️ <b>FOR SALE</b> — founder exiting, discount the signal")

    if profile.get("category"):
        lines.append(f"📂 {escape(str(profile['category']))}")

    lines.append(f"🔗 <a href='https://trustmrr.com/startup/{profile['slug']}'>View on TrustMRR</a>")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
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

    print("🔍 Starting full database fetch...")

    while True:
        params = {"page": page, "limit": PAGE_LIMIT, "sort": "listed-desc"}
        response = requests.get(base_url, headers=headers, params=params)

        if response.status_code == 429:
            reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait_time = max(1, reset_time - int(time.time()) + 2)
            print(f"⚠️ Rate limited. Sleeping {wait_time}s...")
            time.sleep(wait_time)
            continue

        if not response.ok:
            print(f"❌ API Error: {response.status_code}")
            break

        data = response.json()
        startups = data.get("data", [])
        if not startups:
            break

        all_startups.extend(startups)
        print(f"✅ Page {page} fetched. Total: {len(all_startups)}")

        if not data.get("meta", {}).get("hasMore", False):
            break
        page += 1
        time.sleep(SLEEP_TIME)

    return all_startups

def export_dashboard_data(state):
    os.makedirs(DOCS_DIR, exist_ok=True)

    startups = []
    total_mrr_cents = 0

    for slug, info in state.items():
        mrr_cents = info.get("mrr", 0)
        if not isinstance(mrr_cents, (int, float)):
            continue
        total_mrr_cents += mrr_cents

        age = None
        founded_dt = parse_date(info.get("founded"))
        if founded_dt:
            age = age_in_months(founded_dt)

        startups.append({
            "slug": slug,
            "name": info.get("name", slug),
            "mrr_usd": round(mrr_cents / 100, 2),
            "age_months": age,
            "velocity_usd": round((mrr_cents / 100) / age, 2) if age else None,
            "for_sale": bool(info.get("for_sale")),
            "url": f"https://trustmrr.com/startup/{slug}",
        })

    # Sort by SIGNAL (velocity), not raw MRR
    startups.sort(key=lambda x: x["velocity_usd"] or 0, reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_startups": len(startups),
        "total_mrr_usd": round(total_mrr_cents / 100, 2),
        "startups": startups,
    }

    with open(DASHBOARD_DATA_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"📊 Dashboard data exported: {DASHBOARD_DATA_FILE}")

def main():
    if not API_KEY:
        print("❌ Missing TRUSTMRR_API_KEY environment variable.")
        return

    state = load_state()
    first_run = len(state) == 0

    if first_run:
        print("🌱 First run detected. Building baseline silently to avoid Telegram spam.")

    startups = fetch_all_startups()
    alert_count = 0

    for startup in startups:
        profile = extract_profile(startup)
        slug = profile["slug"]
        if not slug:
            continue

        new_mrr = profile["mrr"]
        founded_dt = parse_date(profile["founded"])
        age_months = age_in_months(founded_dt)

        info = state.get(slug)
        old_mrr = info.get("mrr", 0) if info else 0

        verdict, flags = (None, []) if first_run else evaluate(profile, age_months, old_mrr)

        if verdict and on_cooldown(info):
            verdict = None

        if verdict:
            print(f"{verdict}: {profile['name']}")
            send_notification(profile, age_months, old_mrr, verdict, flags)
            alert_count += 1

        # --- Update state (backward-compatible with old entries) ---
        history = (info or {}).get("history", [])
        history.append({"t": int(time.time()), "mrr": new_mrr})
        history = history[-HISTORY_LIMIT:]

        state[slug] = {
            "name": profile["name"],
            "mrr": new_mrr,
            "founded": founded_dt.date().isoformat() if founded_dt else (info or {}).get("founded"),
            "for_sale": profile["for_sale"],
            "history": history,
            "last_alert_t": int(time.time()) if verdict else (info or {}).get("last_alert_t"),
        }

    save_state(state)
    export_dashboard_data(state)

    if first_run:
        print("✅ Baseline built. Future runs will send Telegram alerts.")
    else:
        print(f"✅ Done. {alert_count} alerts sent.")

if __name__ == "__main__":
    main()
