"""
Stock market scanner - flags unusual price/volume moves and posts
alerts (with recent news) to a Discord webhook.

Runs once per invocation - designed to be triggered on a schedule
by GitHub Actions (see .github/workflows/scan.yml).

Required environment variables (set as GitHub Secrets):
    FINNHUB_API_KEY
    DISCORD_WEBHOOK_URL
"""

import os
import time
import requests

# ---------- Config ----------
PRICE_CHANGE_THRESHOLD = 2.0      # percent move to trigger an alert
REL_VOLUME_THRESHOLD = 2.0        # today's volume vs avg volume (last 9 days)
NEWS_LOOKBACK_DAYS = 1
CALLS_PER_MINUTE = 55             # stay under Finnhub's 60/min free limit
TICKERS_FILE = "tickers.txt"

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
BASE_URL = "https://finnhub.io/api/v1"

_call_times = []


def throttle():
    """Simple rate limiter to stay under Finnhub's free-tier cap."""
    now = time.time()
    _call_times[:] = [t for t in _call_times if now - t < 60]
    if len(_call_times) >= CALLS_PER_MINUTE:
        time.sleep(60 - (now - _call_times[0]) + 0.1)
    _call_times.append(time.time())


def get_quote(symbol):
    throttle()
    r = requests.get(f"{BASE_URL}/quote",
                      params={"symbol": symbol, "token": FINNHUB_API_KEY})
    r.raise_for_status()
    return r.json()


def get_daily_candles(symbol, days=10):
    throttle()
    end = int(time.time())
    start = end - days * 24 * 60 * 60
    r = requests.get(f"{BASE_URL}/stock/candle", params={
        "symbol": symbol, "resolution": "D",
        "from": start, "to": end, "token": FINNHUB_API_KEY,
    })
    r.raise_for_status()
    return r.json()


def get_recent_news(symbol):
    throttle()
    import datetime
    today = datetime.date.today()
    from_date = today - datetime.timedelta(days=NEWS_LOOKBACK_DAYS)
    r = requests.get(f"{BASE_URL}/company-news", params={
        "symbol": symbol, "from": from_date.isoformat(),
        "to": today.isoformat(), "token": FINNHUB_API_KEY,
    })
    r.raise_for_status()
    news = r.json()
    return news[0]["headline"] if news else None


def relative_volume(candles):
    vols = candles.get("v", [])
    if candles.get("s") != "ok" or len(vols) < 2:
        return None
    today_vol = vols[-1]
    avg_prior = sum(vols[:-1]) / len(vols[:-1])
    if avg_prior == 0:
        return None
    return today_vol / avg_prior


def send_discord_alert(symbol, pct_change, rel_vol, headline):
    lines = [
        f"**{symbol}** — {pct_change:+.2f}% | Rel. volume: {rel_vol:.1f}x avg"
    ]
    if headline:
        lines.append(f"📰 {headline}")
    payload = {"content": "\n".join(lines)}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload)
    r.raise_for_status()


def load_tickers():
    with open(TICKERS_FILE) as f:
        return [line.strip().upper() for line in f if line.strip()]


def main():
    tickers = load_tickers()
    print(f"Scanning {len(tickers)} tickers...")

    for symbol in tickers:
        try:
            quote = get_quote(symbol)
            pct_change = quote.get("dp")
            if pct_change is None:
                continue

            if abs(pct_change) < PRICE_CHANGE_THRESHOLD:
                continue

            candles = get_daily_candles(symbol)
            rel_vol = relative_volume(candles)
            if rel_vol is None or rel_vol < REL_VOLUME_THRESHOLD:
                continue

            headline = get_recent_news(symbol)
            send_discord_alert(symbol, pct_change, rel_vol, headline)
            print(f"ALERT: {symbol} {pct_change:+.2f}% rel_vol={rel_vol:.1f}x")

        except Exception as e:
            print(f"Error on {symbol}: {e}")

    print("Scan complete.")


if __name__ == "__main__":
    main()
