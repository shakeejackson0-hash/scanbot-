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

CANDLE_LOOKBACK_DAYS = 60         # history pulled per ticker for S/R + rel volume
SWING_WINDOW = 3                  # bars on each side to confirm a swing high/low
LEVEL_PROXIMITY_PCT = 1.0         # price within this % of a level counts as "at" it

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


def get_daily_candles(symbol, days=CANDLE_LOOKBACK_DAYS):
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


def relative_volume(candles, window=10):
    vols = candles.get("v", [])
    if candles.get("s") != "ok" or len(vols) < 2:
        return None
    recent = vols[-window:] if len(vols) >= window else vols
    today_vol = recent[-1]
    prior = recent[:-1]
    avg_prior = sum(prior) / len(prior) if prior else 0
    if avg_prior == 0:
        return None
    return today_vol / avg_prior


def find_key_levels(candles):
    """Identify swing-high/swing-low levels from daily candle history.

    A bar is a swing high if its high is the max within SWING_WINDOW bars
    on each side (same logic, inverted, for swing lows). Returns a sorted
    list of distinct price levels.
    """
    if candles.get("s") != "ok":
        return []

    highs = candles.get("h", [])
    lows = candles.get("l", [])
    n = len(highs)
    levels = []

    for i in range(SWING_WINDOW, n - SWING_WINDOW):
        window_highs = highs[i - SWING_WINDOW:i + SWING_WINDOW + 1]
        window_lows = lows[i - SWING_WINDOW:i + SWING_WINDOW + 1]
        if highs[i] == max(window_highs):
            levels.append(highs[i])
        if lows[i] == min(window_lows):
            levels.append(lows[i])

    # Collapse levels that are basically the same price
    levels = sorted(levels)
    merged = []
    for lvl in levels:
        if merged and abs(lvl - merged[-1]) / merged[-1] * 100 < LEVEL_PROXIMITY_PCT:
            merged[-1] = (merged[-1] + lvl) / 2
        else:
            merged.append(lvl)
    return merged


def check_key_level_event(current_price, prev_close, levels):
    """Returns a description string if price is breaking or testing a
    key level, otherwise None."""
    for lvl in levels:
        pct_dist = abs(current_price - lvl) / lvl * 100
        crossed_up = prev_close < lvl <= current_price
        crossed_down = prev_close > lvl >= current_price

        if crossed_up:
            return f"Broke above key level ${lvl:.2f}"
        if crossed_down:
            return f"Broke below key level ${lvl:.2f}"
        if pct_dist <= LEVEL_PROXIMITY_PCT:
            return f"Testing key level ${lvl:.2f}"
    return None


def send_discord_alert(symbol, pct_change, rel_vol, headline, level_event):
    lines = [
        f"**{symbol}** — {pct_change:+.2f}% | Rel. volume: {rel_vol:.1f}x avg"
    ]
    if level_event:
        lines.append(f"📊 {level_event}")
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
            current_price = quote.get("c")
            prev_close = quote.get("pc")
            if pct_change is None or current_price is None or prev_close is None:
                continue

            candles = get_daily_candles(symbol)
            rel_vol = relative_volume(candles)

            levels = find_key_levels(candles)
            level_event = check_key_level_event(current_price, prev_close, levels)

            meets_move_threshold = (
                abs(pct_change) >= PRICE_CHANGE_THRESHOLD
                and rel_vol is not None
                and rel_vol >= REL_VOLUME_THRESHOLD
            )

            if not meets_move_threshold and not level_event:
                continue

            headline = get_recent_news(symbol)
            send_discord_alert(
                symbol, pct_change, rel_vol or 0, headline, level_event
            )
            print(f"ALERT: {symbol} {pct_change:+.2f}% "
                  f"rel_vol={rel_vol} level_event={level_event}")

        except Exception as e:
            print(f"Error on {symbol}: {e}")

    print("Scan complete.")


if __name__ == "__main__":
    main()
