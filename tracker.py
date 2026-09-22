import os
import sys
import json
import time
import requests
from datetime import datetime, timezone
from html import escape

# --- CONFIGURATION (Pulled from GitHub Secrets) ---
API_KEY = os.environ.get("TRUSTMRR_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "full_revenue_state.json"
DOCS_DIR = "docs"
DASHBOARD_DATA_FILE = os.path.join(DOCS_DIR, "data.json")

# --- THE RUBRIC, RECALIBRATED ---
MIN_MRR_FLOOR_CENTS = 50000      # $500 — universal floor; nothing below alerts
MIN_GROWTH_PCT = 15.0            # Min relative MRR jump between runs
MIN_GROWTH_CENTS = 10000         # +$100 absolute (was $25 — let $1→$35 through)
HOT_MAX_AGE_MONTHS = 18          # Young + growing = HOT
FLATLINE_AGE_MONTHS = 24         # Older than this...
FLATLINE_MRR_CENTS = 200000      # ...and under $2k MRR = market verdict. Never alert.
SUPPRESS_NEW_IF_FOR_SALE = True  # Listings ARE the event; no extra signal
ALERT_COOLDOWN_DAYS = 3          # Don't re-alert the same startup within 3 days
HISTORY_LIMIT = 90               # Snapshots kept per startup (~45 days at 12h cadence)
SLEEP_TIME = 6
PAGE_LIMIT = 10                  # API serves 10 per page regardless
TELEGRAM_GAP_SECONDS = 1.0       # Telegram rate-limit insurance between sends
REQUEST_TIMEOUT = 30
MAX_429_RETRIES = 10
PRUNE_ABSENT_DAYS = 14           # Drop delisted startups from state

# --- WEEKLY SUMMARY ---
DIGEST_WEEKDAY = 0               # 0 = Monday (set to today's weekday to test)
DIGEST_TOP_N = 5
SLOPE_WINDOW_DAYS = 14
SLOPE_MIN_POINTS = 5             # ~3 days of history at 12h cadence


# === state persistence ===

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    """Atomic — a killed run can't corrupt the state file."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def prune_state(state, now_ts):
    """Drop startups not seen in PRUNE_ABSENT_DAYS — they delisted from TrustMRR."""
    cutoff = now_ts - PRUNE_ABSENT_DAYS * 86400
    return {
        slug: info for slug, info in state.items()
        if info.get("last_seen_t", 0) >= cutoff
    }


# === parsing helpers ===

def parse_date(value):
    """Accept ISO strings (incl. offsets/Z), 'October 2021', or unix timestamps (s/ms)."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        # Python 3.11+: fromisoformat handles 'Z' and offsets natively.
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
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


# === domain logic ===

def extract_profile(startup):
    """Pull rubric fields out of the raw API object, defensively."""
    revenue = startup.get("revenue", {}) or {}
    sale = startup.get("sale", {}) or {}

    asking = startup.get("askingPrice") or sale.get("askingPrice")
    for_sale = bool(
        startup.get("forSale")
        or startup.get("isForSale")
        or asking
        or sale.get("listed")
    )

    raw_mrr = revenue.get("mrr", 0) or 0
    try:
        mrr = int(float(raw_mrr))  # API sometimes sends null or "5000"
    except (TypeError, ValueError):
        mrr = 0

    raw_subs = revenue.get("activeSubscriptions") or startup.get("activeSubscriptions")
    try:
        subs = int(raw_subs) if raw_subs is not None else None
    except (TypeError, ValueError):
        subs = None

    return {
        "slug": startup.get("slug"),
        "name": startup.get("name") or "Unknown",
        "mrr": mrr,
        "subs": subs,
        "founded": startup.get("foundedAt") or startup.get("founded"),
        "for_sale": for_sale,
        "asking_price": asking,
        "category": startup.get("category") or startup.get("market"),
    }


def is_anonymous(profile):
    """Stealth/anonymous listings are junk-tier for signal purposes."""
    name = (profile.get("name") or "").strip().lower()
    slug = (profile.get("slug") or "").strip().lower()
    return (
        name in {"", "unknown", "anonymous", "anonymous startup"}
        or "stealth" in slug
        or "anonymous" in slug
    )


def evaluate(profile, age_months, old_mrr):
    """The rubric. Returns (verdict, flags).
    verdict in {'HOT', 'GROWTH', 'NEW', None}."""
    new_mrr = profile["mrr"]
    flags = []
    if profile["for_sale"]:
        flags.append("for_sale")

    # KILL: below the universal floor
    if new_mrr < MIN_MRR_FLOOR_CENTS:
        return None, flags

    # KILL: zombie — old and small = market verdict, never alert
    if (age_months is not None
            and age_months > FLATLINE_AGE_MONTHS
            and new_mrr < FLATLINE_MRR_CENTS):
        return None, flags

    # NEW listing: a for_sale NEW is just the listing itself — no extra signal
    if old_mrr <= 0:
        if SUPPRESS_NEW_IF_FOR_SALE and profile["for_sale"]:
            return None, flags
        return "NEW", flags

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


# === notifications ===

def send_telegram(text):
    """Shared sender. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing Telegram credentials in GitHub Secrets.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if not response.ok:
            print(f"❌ Telegram API Error: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ Failed to send Telegram message: {e}")
        return False


def send_notification(profile, age_months, old_mrr, verdict, flags):
    """Builds the alert message. Returns True if Telegram actually delivered it."""
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

    subs = profile.get("subs")
    if isinstance(subs, (int, float)) and subs > 0:
        arpu = (new_mrr / 100) / subs
        arpu_str = f"${arpu:,.2f}" if arpu < 10 else f"${arpu:,.0f}"
        lines.append(f"👥 {int(subs):,} users · {arpu_str}/user")

    if "for_sale" in flags:
        lines.append("🏷️ <b>FOR SALE</b> — founder exiting, discount the signal")

    if profile.get("category"):
        lines.append(f"📂 {escape(str(profile['category']))}")

    safe_slug = escape(str(profile.get("slug") or ""), quote=True)
    lines.append(f"🔗 <a href='https://trustmrr.com/startup/{safe_slug}'>View on TrustMRR</a>")

    sent = send_telegram("\n".join(lines))
    if TELEGRAM_GAP_SECONDS:
        time.sleep(TELEGRAM_GAP_SECONDS)
    return sent


# === fetch + analytics ===

def fetch_all_startups():
    """Returns the startup list, or None on hard failure (caller exits nonzero)."""
    base_url = "https://trustmrr.com/api/v1/startups"
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {API_KEY}"})
    all_startups = []
    page = 1
    retries_429 = 0

    print("🔍 Starting full database fetch...")

    while True:
        try:
            response = session.get(
                base_url,
                params={"page": page, "limit": PAGE_LIMIT, "sort": "listed-desc"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"❌ Request failed: {e}")
            return None

        if response.status_code == 429:
            retries_429 += 1
            if retries_429 > MAX_429_RETRIES:
                print("❌ Too many rate limits — giving up.")
                return None
            raw = (response.headers.get("X-RateLimit-Reset")
                   or response.headers.get("Retry-After"))
            try:
                reset = int(raw)
                if reset > 1e12:        # API sends milliseconds
                    reset //= 1000
                wait_time = max(5, min(120, reset - int(time.time()) + 2))
            except (TypeError, ValueError):
                wait_time = 15          # unknown format — safe default
            print(f"⚠️ Rate limited ({retries_429}/{MAX_429_RETRIES}). Sleeping {wait_time}s...")
            time.sleep(wait_time)
            continue

        retries_429 = 0

        if not response.ok:
            print(f"❌ API Error: {response.status_code}")
            return None

        try:
            data = response.json()
        except ValueError:
            print("❌ API returned non-JSON body.")
            return None

        if "data" not in data:
            print("❌ Unexpected API shape (no 'data' key) — API may have changed.")
            return None

        startups = data.get("data") or []
        if not startups:
            break

        all_startups.extend(startups)
        print(f"✅ Page {page} fetched. Total: {len(all_startups)}")

        if not data.get("meta", {}).get("hasMore", False):
            break
        page += 1
        time.sleep(SLEEP_TIME)

    return all_startups


def slope_per_day(history, window_days):
    """Least-squares MRR slope in cents/day over recent history.
    Catches steady compounders the run-over-run spike detector can't see."""
    cutoff = time.time() - window_days * 86400
    pts = [
        (h["t"], h["mrr"]) for h in history
        if isinstance(h, dict)
        and isinstance(h.get("t"), (int, float))
        and isinstance(h.get("mrr"), (int, float))
        and h["t"] >= cutoff
    ]
    if len(pts) < SLOPE_MIN_POINTS:
        return None
    n = len(pts)
    t0 = pts[0][0]
    xs = [(t - t0) / 86400.0 for t, _ in pts]
    ys = [float(m) for _, m in pts]
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def build_digest(state):
    """Weekly top growers + category mix. Catches steady compounders."""
    today = datetime.now(timezone.utc)
    tracked = 0
    total_mrr_cents = 0
    eligible = []
    cat_counts = {}

    for slug, info in state.items():
        if not isinstance(info, dict):
            continue
        mrr = info.get("mrr") or 0
        if not isinstance(mrr, (int, float)):
            continue
        tracked += 1
        total_mrr_cents += mrr
        if mrr < MIN_MRR_FLOOR_CENTS:
            continue
        slope = slope_per_day(info.get("history", []), SLOPE_WINDOW_DAYS)
        if slope is not None and slope > 0:
            eligible.append((info.get("name") or slug, mrr, slope))
        cat = info.get("category")
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

    eligible.sort(key=lambda x: x[2], reverse=True)
    top = eligible[:DIGEST_TOP_N]

    lines = [
        "📊 <b>Weekly Digest</b>",
        f"🗓 {today.strftime('%a %b %d, %Y')}",
        f"🧮 Tracking: <b>{tracked}</b> startups · "
        f"<b>${total_mrr_cents/100:,.0f}</b> total MRR",
    ]
    if top:
        lines.append(f"🚀 Top {len(top)} growers (least-squares slope, $/day):")
        for name, mrr, slope in top:
            lines.append(
                f"  • {escape(str(name))} — "
                f"${mrr/100:,.0f} MRR · +${slope/100:,.2f}/day"
            )
    else:
        lines.append("(no qualifying slope yet — need ~3 days of history)")

    if cat_counts:
        top_cats = sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        cat_lines = ", ".join(f"{escape(str(c))}: {n}" for c, n in top_cats)
        lines.append(f"📂 Category mix: {cat_lines}")

    return "\n".join(lines)


def build_heartbeat(state):
    """Silent-failure detector. If this stops arriving, the monitor is down."""
    now = datetime.now(timezone.utc)
    total_cents = 0
    tracked = 0
    last_alert_ts = 0
    for info in state.values():
        if not isinstance(info, dict):
            continue
        mrr = info.get("mrr", 0)
        if isinstance(mrr, (int, float)):
            tracked += 1
            total_cents += mrr
        la = info.get("last_alert_t") or 0
        if isinstance(la, (int, float)) and la > last_alert_ts:
            last_alert_ts = la

    last_alert_str = (
        datetime.fromtimestamp(last_alert_ts, tz=timezone.utc)
        .strftime("%Y-%m-%d %H:%M UTC")
        if last_alert_ts else "never"
    )

    return (
        f"📡 <b>Heartbeat</b>\n"
        f"🗓 {now.strftime('%a %b %d, %H:%M UTC')}\n"
        f"🧮 Tracking <b>{tracked}</b> startups · "
        f"<b>${total_cents/100:,.0f}</b> total MRR\n"
        f"🔔 Last alert: <b>{last_alert_str}</b>\n"
        f"<i>Silence beyond this = monitor is down.</i>"
    )


def build_weekly_summary(state):
    return f"{build_heartbeat(state)}\n\n— — —\n\n{build_digest(state)}"


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

        # Same clamp as notification (`max(age, 1)`) keeps the dashboard consistent.
        velocity = (
            round((mrr_cents / 100) / max(age, 1), 2) if age and age > 0
            else None
        )

        startups.append({
            "slug": slug,
            "name": info.get("name") or slug,
            "mrr_usd": round(mrr_cents / 100, 2),
            "age_months": age,
            "velocity_usd": velocity,
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


# === entry point ===

def main():
    if not API_KEY:
        print("❌ Missing TRUSTMRR_API_KEY environment variable.")
        sys.exit(1)

    state = load_state()
    state = prune_state(state, time.time())
    first_run = len(state) == 0

    if first_run:
        print("🌱 First run detected. Building baseline silently to avoid Telegram spam.")

    startups = fetch_all_startups()
    if startups is None:
        print("❌ Fetch failed; exiting.")
        sys.exit(1)

    alert_count = 0
    sent_count = 0
    now_ts = int(time.time())

    for startup in startups:
        profile = extract_profile(startup)
        if is_anonymous(profile):
            continue
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

        sent_ok = False
        if verdict:
            print(f"{verdict}: {profile['name']}")
            sent_ok = send_notification(profile, age_months, old_mrr, verdict, flags)
            alert_count += 1
            if sent_ok:
                sent_count += 1

        # --- Update state (backward-compatible with old entries) ---
        history = (info or {}).get("history", [])
        history.append({"t": now_ts, "mrr": new_mrr})
        history = history[-HISTORY_LIMIT:]

        state[slug] = {
            "name": profile["name"],
            "mrr": new_mrr,
            "founded": founded_dt.date().isoformat() if founded_dt else (info or {}).get("founded"),
            "for_sale": profile["for_sale"],
            "category": profile.get("category"),
            "history": history,
            "last_seen_t": now_ts,
            # Cooldown stamps ONLY on confirmed Telegram delivery.
            "last_alert_t": now_ts if sent_ok else (info or {}).get("last_alert_t"),
        }

    save_state(state)
    export_dashboard_data(state)

    # Weekly summary (heartbeat + digest) on the configured weekday; skip first run.
    if not first_run and datetime.now(timezone.utc).weekday() == DIGEST_WEEKDAY:
        if send_telegram(build_weekly_summary(state)):
            print("📬 Weekly summary sent.")

    if first_run:
        print("✅ Baseline built. Future runs will send Telegram alerts.")
    else:
        print(f"✅ Done. {alert_count} candidates · {sent_count} delivered.")


if __name__ == "__main__":
    main()
