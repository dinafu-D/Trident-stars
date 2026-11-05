"""
TRIDENT 3-Star Alert  ->  Telegram
===================================
Python copy of the TRIDENT V3.45/V3.46 "star group" rule, for NIFTY and SENSEX (index).

Star group (same kind = all swing highs OR all swing lows):
  * anchor  = an UNTAKEN chart swing whose price is EXACTLY a higher-timeframe swing price
              (1h, then 4h, then 1D; 15m too if SKIP_15M is False; only TFs above the chart TF)
  * group   = anchor + the next 2 untaken chart swings after it on the same side  ->  3 stars
  * a swing is "taken" when price trades through it (high >= swing high / low <= swing low)
  * if star 2 or 3 is taken the group re-forms with the next untaken swings; anchor taken -> group ends
  * HTF swings count only after their HTF candle has closed (same as TRIDENT on history)

Swings follow TRIDENT defaults: Swing Left/Right Candles = 1 (strict), max 100 active swings per side,
HTF pivot history = last 20 pivots per timeframe, exact price match (tolerance 0).

Alerts (each sent once, only if it formed today and is still live):
  * 2 stars  = anchor + 1 untaken swing after it  -> heads-up, the next swing on that side completes the group
  * 3 stars  = complete group
Data: Yahoo Finance via yfinance (free). Candles are rebuilt from 5m data so prices match exactly.
"""

import json
import os
import sys
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import requests
import yfinance as yf

# ───────────────────────── SETTINGS ─────────────────────────
SYMBOLS = {"NIFTY": "^NSEI", "SENSEX": "^BSESN"}
# chart timeframes to watch (comma list in env CHART_TFS, e.g. "5m,15m")
CHART_TFS = [t.strip() for t in os.getenv("CHART_TFS", "5m,15m").split(",") if t.strip()]
SKIP_15M = os.getenv("SKIP_15M", "1") == "1"   # TRIDENT default: ON (only 1h / 4h / 1D start a group)
PIVOT_LEN = 1                                  # TRIDENT "Swing Left/Right Candles"
PIV_CAP = 20                                   # TRIDENT swingTFLookback
SWING_CAP = 100                                # TRIDENT max active swing highs / lows
ALERT_2STAR = os.getenv("ALERT_2STAR", "1") == "1"   # heads-up when anchor + 1 swing exist (3rd star pending)

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = os.getenv("STATE_FILE", "state/sent_stars.json")

IST = "Asia/Kolkata"
SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
TF_MIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1D": 1440}


# ───────────────────────── DATA ─────────────────────────
def now_ist():
    return pd.Timestamp.now(tz=IST)


def fetch(ticker):
    """5m candles (last ~59 days) + daily candles (1 year), IST, rounded to 2 decimals."""
    t = yf.Ticker(ticker)
    m5 = t.history(interval="5m", period="59d", auto_adjust=False)
    d1 = t.history(interval="1d", period="1y", auto_adjust=False)
    if m5.empty:
        raise RuntimeError(f"no 5m data for {ticker}")
    m5 = m5.tz_convert(IST)[["High", "Low", "Close"]].dropna().round(2)
    m5 = m5[(m5.index.time >= SESSION_OPEN) & (m5.index.time < SESSION_CLOSE)]
    d1 = d1[["High", "Low"]].dropna().round(2)
    d1.index = pd.to_datetime(d1.index.date)
    return m5, d1


def session_close_of(ts):
    return pd.Timestamp.combine(ts.date(), SESSION_CLOSE).tz_localize(IST)


def build_bars(m5, tf, now):
    """Rebuild a timeframe from 5m candles, TradingView NSE alignment, closed candles only.
    Returns DataFrame: start, end, high, low."""
    df = m5.copy()
    if tf == "5m":
        key = df.index
    elif tf == "15m":
        key = df.index.floor("15min")
    elif tf == "1h":   # 09:15, 10:15, ... 15:15
        key = (df.index - pd.Timedelta(minutes=15)).floor("60min") + pd.Timedelta(minutes=15)
    elif tf == "4h":   # 09:15-13:15, 13:15-15:30
        day = df.index.normalize()
        second = df.index.time >= dtime(13, 15)
        key = day + pd.to_timedelta(["13h15min" if s else "9h15min" for s in second])
    else:
        raise ValueError(tf)
    g = df.groupby(key).agg(high=("High", "max"), low=("Low", "min"))
    g.index.name = "start"
    g = g.reset_index()
    g["end"] = [min(s + pd.Timedelta(minutes=TF_MIN[tf]), session_close_of(s)) for s in g["start"]]
    g = g[g["end"] <= now]                       # closed candles only
    return g[["start", "end", "high", "low"]].reset_index(drop=True)


def build_daily(d1, now):
    g = pd.DataFrame({"start": [pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15) for d in d1.index],
                      "high": d1["High"].values, "low": d1["Low"].values})
    g["end"] = [session_close_of(s) for s in g["start"]]
    g = g[g["end"] <= now]                       # today's daily candle only after 15:30
    return g[["start", "end", "high", "low"]].reset_index(drop=True)


# ───────────────────────── TRIDENT LOGIC ─────────────────────────
def htf_pivot_events(bars, tf):
    """TRIDENT f_pivotHTF: bar j-1 is a pivot high if high[j-1] > high[j-2] and > high[j];
    known when bar j closes. Returns list of (known_time, tf, is_high, price)."""
    ev = []
    h, l, e = bars["high"].values, bars["low"].values, list(bars["end"])
    for j in range(2, len(bars)):
        if h[j - 1] > h[j - 2] and h[j - 1] > h[j]:
            ev.append((pd.Timestamp(e[j]), tf, True, float(h[j - 1])))
        if l[j - 1] < l[j - 2] and l[j - 1] < l[j]:
            ev.append((pd.Timestamp(e[j]), tf, False, float(l[j - 1])))
    ev.sort(key=lambda x: x[0])
    return ev


def push_piv(arr, v):
    """TRIDENT f_pushPiv: skip if same as last, keep last PIV_CAP."""
    if not arr or arr[-1] != v:
        arr.append(v)
        if len(arr) > PIV_CAP:
            arr.pop(0)


def anchor_tfs(chart_tf):
    order = (["15m"] if not SKIP_15M else []) + ["1h", "4h", "1D"]
    return [t for t in order if TF_MIN[t] > TF_MIN[chart_tf]]


def simulate(chart, events, chart_tf):
    """Walk the chart candles like TRIDENT does bar by bar.
    Returns (live groups at the last candle, first-formed time per group key)."""
    a_tfs = anchor_tfs(chart_tf)
    piv = {(tf, side): [] for tf in a_tfs for side in (True, False)}
    ev_i = 0
    swings = []          # dicts: lvl, hi, t, anc, anc_tf  (in push order)
    formed = {}          # group key -> candle end time when first complete
    groups = []
    H, L, S, E = chart["high"].values, chart["low"].values, list(chart["start"]), list(chart["end"])

    for k in range(len(chart)):
        bar_end = pd.Timestamp(E[k])
        # 1) HTF pivots known by this candle's close
        while ev_i < len(events) and events[ev_i][0] <= bar_end:
            _, tf, side, price = events[ev_i]
            if (tf, side) in piv:
                push_piv(piv[(tf, side)], price)
            ev_i += 1
        # 2) mitigation (before new swings)
        swings = [s for s in swings if not ((s["hi"] and H[k] >= s["lvl"]) or (not s["hi"] and L[k] <= s["lvl"]))]
        # 3) new swing at k-1 (strict, PIVOT_LEN each side)
        p = k - PIVOT_LEN
        if p - PIVOT_LEN >= 0:
            if all(H[p - j] < H[p] and H[p + j] < H[p] for j in range(1, PIVOT_LEN + 1)):
                swings.append({"lvl": float(H[p]), "hi": True, "t": pd.Timestamp(S[p]), "anc": False, "anc_tf": None})
                if sum(s["hi"] for s in swings) > SWING_CAP:
                    swings.pop(next(i for i, s in enumerate(swings) if s["hi"]))
            if all(L[p - j] > L[p] and L[p + j] > L[p] for j in range(1, PIVOT_LEN + 1)):
                swings.append({"lvl": float(L[p]), "hi": False, "t": pd.Timestamp(S[p]), "anc": False, "anc_tf": None})
                if sum(not s["hi"] for s in swings) > SWING_CAP:
                    swings.pop(next(i for i, s in enumerate(swings) if not s["hi"]))
        # 4) anchors (sticky) + groups
        for s in swings:
            if not s["anc"]:
                for tf in a_tfs:
                    if s["lvl"] in piv[(tf, s["hi"])]:
                        s["anc"], s["anc_tf"] = True, tf
                        break
        groups = []
        for side in (True, False):
            side_sw = [s for s in swings if s["hi"] == side]
            for a in range(len(side_sw)):
                if not side_sw[a]["anc"]:
                    continue
                # 3 stars = anchor + next 2 untaken swings; 2 stars = anchor + only 1 untaken swing so far
                n_after = len(side_sw) - 1 - a
                if n_after >= 2:
                    g = side_sw[a:a + 3]
                elif n_after == 1 and ALERT_2STAR:
                    g = side_sw[a:a + 2]
                else:
                    continue
                key = f"{len(g)}S|" + ("H" if side else "L") + "|" + "|".join(x["t"].isoformat() for x in g)
                formed.setdefault(key, bar_end)
                groups.append((key, side, g))
    return groups, formed


# ───────────────────────── STATE + TELEGRAM ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    cutoff = (now_ist() - pd.Timedelta(days=10)).isoformat()
    state = {k: v for k, v in state.items() if v >= cutoff}
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[no Telegram secrets] would send:\n" + text)
        return True
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=20)
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)
    return r.ok


def fmt_t(ts):
    return ts.strftime("%d %b %H:%M")


def message(name, tf, side, g, formed_at, last_price):
    kind = "SWING HIGHS 🔴" if side else "SWING LOWS 🟢"
    lv = [x["lvl"] for x in g]
    if len(g) == 3:
        head = f"★★★ <b>{name} {tf}</b> — 3-STAR group formed on {kind}"
    else:
        head = (f"★★☆ <b>{name} {tf}</b> — 2 stars on {kind}\n"
                f"Get ready: the next untaken {'swing high' if side else 'swing low'} completes the group")
    lines = [head, f"Anchor: {lv[0]:.2f}  ({g[0]['anc_tf']} swing)  @ {fmt_t(g[0]['t'])}",
             f"Star 2: {lv[1]:.2f}  @ {fmt_t(g[1]['t'])}"]
    if len(g) == 3:
        lines.append(f"Star 3: {lv[2]:.2f}  @ {fmt_t(g[2]['t'])}")
    lines += [f"Zone: {min(lv):.2f} – {max(lv):.2f}",
             f"Formed on candle closing {fmt_t(formed_at)} IST  |  Price ≈ {last_price:.2f}"]
    return "\n".join(lines)


# ───────────────────────── MAIN ─────────────────────────
def main():
    now = now_ist()
    force = os.getenv("FORCE", "0") == "1"
    if not force and (now.weekday() >= 5 or not (dtime(9, 15) <= now.time() <= dtime(15, 45))):
        print("outside market hours, exit")
        return 0
    state = load_state()
    today = now.date()
    sent_any = 0
    for name, ticker in SYMBOLS.items():
        try:
            m5, d1 = fetch(ticker)
        except Exception as e:
            print(f"{name}: data error {e}")
            continue
        htf = {"15m": build_bars(m5, "15m", now), "1h": build_bars(m5, "1h", now),
               "4h": build_bars(m5, "4h", now), "1D": build_daily(d1, now)}
        for tf in CHART_TFS:
            chart = build_bars(m5, tf, now)
            if len(chart) < 5:
                continue
            events = []
            for atf in anchor_tfs(tf):
                events += htf_pivot_events(htf[atf], atf)
            events.sort(key=lambda x: x[0])
            groups, formed = simulate(chart, events, tf)
            last_price = float(m5["Close"].iloc[-1])
            for key, side, g in groups:
                skey = f"{name}|{tf}|{key}"
                f_at = formed[key]
                if skey in state or f_at.date() != today:
                    continue
                if send(message(name, tf, side, g, f_at, last_price)):
                    state[skey] = now.isoformat()
                    sent_any += 1
            print(f"{name} {tf}: {len(chart)} candles, {sum(len(g) == 3 for _, _, g in groups)} 3-star, {sum(len(g) == 2 for _, _, g in groups)} 2-star live")
    save_state(state)
    print(f"sent {sent_any} alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
