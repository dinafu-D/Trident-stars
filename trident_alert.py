"""
TRIDENT Reminder Bot  ->  Telegram          (matches TRIDENT V3.57)
=====================================================================
Index: NIFTY and SENSEX.  Data: Dhan (5m, 15m, 60m candles). Every 5 minutes on trading days.

THREE kinds of reminder
-----------------------
1) STAR GROUPS (new V3.57 rule: groups never overlap), on 5m, 15m, 1h, 4h, 1D, 1W and 1M charts. Sent ONLY when all 3 stars are formed
     * anchor = an UNTAKEN swing sitting EXACTLY on a higher-timeframe swing price
         5m chart -> 1h / 4h / 1D swings | 1h chart -> 4h / 1D | 4h chart -> 1D
     * Per side, oldest untaken anchor first: group = anchor + the next 2 untaken swings (3 stars).
       The scan then goes on AFTER star 3, so an HTF swing inside a group is only a member.
     * Alert only when the 3rd star forms, if the group formed today (1D/1W/1M: last 4 days) and is still live.
       (ALERT_2STAR=1 brings the 2-star heads-up back.)
     * 1D chart anchors on 1W swings, 1W on 1M, 1M on 3M.
     * When a group is taken (price trades through it) its Telegram message is deleted.
2) BOX BREAK: a consolidation box on the 1h, 4h or 1D chart broke UP (U) or DOWN (D) on a closed candle.
     Label = how long the box lasted:  "3d", "1.5h", "45m" (1D chart: number of daily candles)
3) DARK EOB on 15m: a dark EOB (FVG follows) or a dark SWEEP EOB just formed.
     Label = "Dark EOB" or "Dark Sweep EOB" + green / red

Each alert is sent once (state/sent_stars.json).  Only candles that CLOSED today are alerted.
"""
import json
import os
import sys
import time as _time
from datetime import time as dtime

import numpy as np
import pandas as pd
import requests

# ───────────────────────── SETTINGS ─────────────────────────
SYMBOLS = {"NIFTY": 13, "SENSEX": 51}             # Dhan index security ids
if os.getenv("SYMBOLS"):                          # optional, e.g. "NIFTY"
    SYMBOLS = {k: v for k, v in SYMBOLS.items() if k in [s.strip() for s in os.getenv("SYMBOLS").split(",")]}

STAR_TFS = [t.strip() for t in os.getenv("STAR_TFS", "5m,15m,1h,4h,1D,1W,1M").split(",") if t.strip()]
BOX_TFS = [t.strip() for t in os.getenv("BOX_TFS", "1h,4h,1D").split(",") if t.strip()]
EOB_ON = os.getenv("EOB_ON", "1") == "1"
ALERT_2STAR = os.getenv("ALERT_2STAR", "0") == "1"      # default OFF: only complete 3-star groups are sent
DELETE_TAKEN = os.getenv("DELETE_TAKEN", "1") == "1"
# a box gets a label (and an alert) once it lasted this many candles of ITS OWN timeframe (TRIDENT: 1h = 9, 4h / 1D = every box)
BOX_MIN_BARS = {"1h": int(os.getenv("BOX_MIN_BARS_1H", "9")), "4h": int(os.getenv("BOX_MIN_BARS_4H", "1")),
                "1D": int(os.getenv("BOX_MIN_BARS_1D", "1"))}

PIVOT_LEN = 1          # Swing Left/Right Candles
PIV_CAP = 20           # HTF pivot history
SWING_CAP = 100        # max active swings per side
SWP_KEEP = 300         # untaken swings remembered for the sweep test

DAYS_5M = int(os.getenv("DAYS_5M", "59"))
DAYS_15M = int(os.getenv("DAYS_15M", "180"))      # history for the sweep test
DAYS_60M = int(os.getenv("DAYS_60M", "400"))      # 1h / 4h / 1D charts
DAYS_1D = int(os.getenv("DAYS_1D", "3650"))       # daily history for the 1D / 1W / 1M star charts

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DHAN_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT = os.getenv("DHAN_CLIENT_ID", "")
STATE_FILE = os.getenv("STATE_FILE", "state/sent_stars.json")
LOCAL_DIR = os.getenv("LOCAL_DIR", "")            # test only: read <SYMBOL>_5m/15m/60m.parquet from this folder

IST = "Asia/Kolkata"
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
TF_MIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1D": 1440, "1W": 10080, "1M": 43200, "3M": 129600}
DHAN_URL = "https://api.dhan.co/v2/charts/intraday"


def now_ist():
    if os.getenv("AS_OF"):                         # test only: pretend it is this moment
        return pd.Timestamp(os.getenv("AS_OF")).tz_localize(IST)
    return pd.Timestamp.now(tz=IST)


# ───────────────────────── DATA (Dhan) ─────────────────────────
def _dhan_chunk(sec_id, interval, start, end):
    body = {"securityId": str(sec_id), "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": interval,
            "oi": False, "fromDate": start.strftime("%Y-%m-%d 00:00:00"), "toDate": end.strftime("%Y-%m-%d 23:59:59")}
    hdr = {"access-token": DHAN_TOKEN, "client-id": DHAN_CLIENT, "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(5):
        try:
            r = requests.post(DHAN_URL, json=body, headers=hdr, timeout=30)
        except requests.RequestException as e:
            print(f"  network error ({e}); retry {attempt + 1}/5")
            _time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            print(f"  Dhan HTTP {r.status_code}; retry {attempt + 1}/5")
            _time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Dhan HTTP {r.status_code}: {r.text[:200]}  (check DHAN_ACCESS_TOKEN - it expires every 24 h)")
    raise RuntimeError("Dhan: gave up after 5 retries")


def _session_only(df):
    t = df.index.time
    return df[(t >= SESSION_OPEN) & (t < SESSION_CLOSE)]


def fetch(name, interval_min, days):
    """Candles for one index. Returns DataFrame indexed by candle START time (IST): open high low close."""
    if LOCAL_DIR:
        p = os.path.join(LOCAL_DIR, f"{name}_{interval_min}m.parquet")
        d = pd.read_parquet(p)
        d["ts"] = pd.to_datetime(d["ts"])
        d = d.set_index("ts")[["open", "high", "low", "close"]].sort_index()
        d.index = d.index.tz_convert(IST) if d.index.tz is not None else d.index.tz_localize(IST)
        d = d[(d.index >= now_ist() - pd.Timedelta(days=days + 1)) & (d.index < now_ist())]   # same window as live
        return _session_only(d)
    now = now_ist()
    frames, end = [], now.normalize().tz_localize(None)
    start_all = end - pd.Timedelta(days=days)
    s = start_all
    while s <= end:
        e = min(s + pd.Timedelta(days=79), end)
        js = _dhan_chunk(SYMBOLS[name], interval_min, s, e)
        ts = js.get("timestamp") or []
        if ts:
            frames.append(pd.DataFrame({"ts": pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST),
                                        "open": js["open"], "high": js["high"], "low": js["low"], "close": js["close"]}))
        s = e + pd.Timedelta(days=1)
        _time.sleep(0.25)                       # stay below Dhan's rate limit
    if not frames:
        raise RuntimeError(f"no {interval_min}m data for {name}")
    d = pd.concat(frames).drop_duplicates("ts").set_index("ts").sort_index()
    d = d.round(2)
    return _session_only(d)


def fetch_daily(name, m60, days):
    """Daily candles (index = date) for the 1D / 1W / 1M star charts. Dhan daily endpoint; if it fails, built from 60m."""
    if not LOCAL_DIR:
        try:
            now = now_ist()
            body = {"securityId": str(SYMBOLS[name]), "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False,
                    "fromDate": (now - pd.Timedelta(days=days)).strftime("%Y-%m-%d"), "toDate": now.strftime("%Y-%m-%d")}
            hdr = {"access-token": DHAN_TOKEN, "client-id": DHAN_CLIENT, "Content-Type": "application/json", "Accept": "application/json"}
            r = requests.post("https://api.dhan.co/v2/charts/historical", json=body, headers=hdr, timeout=40)
            js = r.json() if r.ok else {}
            if js.get("timestamp"):
                d = pd.DataFrame({"d": pd.to_datetime(js["timestamp"], unit="s", utc=True).tz_convert(IST).normalize().tz_localize(None),
                                  "high": js["high"], "low": js["low"]}).drop_duplicates("d").set_index("d").sort_index().round(2)
                if len(d) > 100:
                    return d
            print(f"{name}: daily endpoint gave nothing usable (HTTP {r.status_code}); using daily built from 60m")
        except Exception as e:
            print(f"{name}: daily endpoint error {e}; using daily built from 60m")
    g = m60.groupby(m60.index.normalize().tz_localize(None)).agg(high=("high", "max"), low=("low", "min"))
    return g


def build_period(d1, tf, now):
    """1D / 1W / 1M / 3M candles from daily data (week = Mon-Fri, calendar month / quarter). A candle counts once its period is over."""
    dates = pd.DatetimeIndex(d1.index)
    if tf == "1D":
        key = dates
    elif tf == "1W":
        key = dates - pd.to_timedelta(dates.weekday, unit="D")
    elif tf == "1M":
        key = dates.to_period("M").to_timestamp()
    else:
        key = dates.to_period("Q").to_timestamp()
    g = d1.groupby(key).agg(high=("high", "max"), low=("low", "min"))
    starts = pd.DatetimeIndex(g.index)
    ends = starts if tf == "1D" else starts + pd.Timedelta(days=4) if tf == "1W" else starts + pd.offsets.MonthEnd(0) if tf == "1M" else starts + pd.offsets.QuarterEnd(0)
    out = pd.DataFrame({"high": g["high"].values, "low": g["low"].values,
                        "end": (ends + pd.Timedelta(hours=15, minutes=30)).tz_localize(IST)},
                       index=(starts + pd.Timedelta(hours=9, minutes=15)).tz_localize(IST))
    out.index.name = "start"
    return out[out["end"] <= now]


def session_close_of(ts):
    return pd.Timestamp.combine(ts.date(), SESSION_CLOSE).tz_localize(IST)


def build_tf(src, src_min, tf, now):
    """Rebuild a timeframe from finer candles (TradingView NSE alignment). CLOSED candles only.
    Returns DataFrame indexed by candle start: open high low close + end."""
    df = src
    if tf == "5m" or TF_MIN[tf] == src_min:
        key = df.index
    elif tf == "15m":
        key = df.index.floor("15min")
    elif tf == "1h":      # 09:15, 10:15 ... 15:15
        key = (df.index - pd.Timedelta(minutes=15)).floor("60min") + pd.Timedelta(minutes=15)
    elif tf == "4h":      # 09:15-13:15, 13:15-15:30
        day = df.index.normalize()
        key = day + pd.to_timedelta(["13h15min" if x else "9h15min" for x in (df.index.time >= dtime(13, 15))])
    elif tf == "1D":
        key = df.index.normalize() + pd.Timedelta(hours=9, minutes=15)
    else:
        raise ValueError(tf)
    # last constituent's end: a candle counts as closed only if its data runs to the end of the candle
    cend = pd.Series([min(s + pd.Timedelta(minutes=src_min), session_close_of(s)) for s in df.index], index=df.index)
    g = df.assign(_ce=cend).groupby(key).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                              close=("close", "last"), _ce=("_ce", "max"))
    g.index.name = "start"
    g["end"] = [min(s + pd.Timedelta(minutes=TF_MIN[tf]), session_close_of(s)) for s in g.index]
    g = g[(g["end"] <= now) & (g["_ce"] >= g["end"])]
    return g.drop(columns="_ce")


# ───────────────────────── 1) STARS (V3.57) ─────────────────────────
def htf_pivot_events(bars, tf):
    """TRIDENT f_pivotHTF: bar j-1 is a pivot high if high[j-1] > high[j-2] and > high[j]; known when bar j closes."""
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
    if not arr or arr[-1] != v:
        arr.append(v)
        if len(arr) > PIV_CAP:
            arr.pop(0)


def anchor_tfs(chart_tf):
    """5m/15m chart: 1h / 4h / 1D | 1h: 4h / 1D | 4h: 1D | 1D: 1W | 1W: 1M | 1M: 3M"""
    if chart_tf in ("1D", "1W", "1M"):
        return {"1D": ["1W"], "1W": ["1M"], "1M": ["3M"]}[chart_tf]
    return [t for t in ("1h", "4h", "1D") if TF_MIN[t] > TF_MIN[chart_tf]]


def star_groups(side_sw):
    """V3.57: oldest untaken anchor first, group = anchor + next 2 untaken swings, scan continues AFTER the group."""
    out, a, m = [], 0, len(side_sw)
    while a < m:
        if not side_sw[a]["anc"]:
            a += 1
            continue
        after = m - 1 - a
        if after >= 2:
            out.append(side_sw[a:a + 3]); a += 3
        elif after == 1 and ALERT_2STAR:
            out.append(side_sw[a:a + 2]); a += 2
        else:
            a += 1
    return out


def simulate(chart, events, chart_tf):
    """Walk the chart candles like TRIDENT does bar by bar.
    Returns (live groups at the last candle, {key: (formed time, side, swings)} for every group ever formed)."""
    a_tfs = anchor_tfs(chart_tf)
    piv = {(tf, side): [] for tf in a_tfs for side in (True, False)}
    ev_i, swings, formed, groups = 0, [], {}, []
    H, L = chart["high"].values, chart["low"].values
    S, E = list(chart.index), list(chart["end"])
    for k in range(len(chart)):
        bar_end = pd.Timestamp(E[k])
        while ev_i < len(events) and events[ev_i][0] <= bar_end:
            _, tf, side, price = events[ev_i]
            if (tf, side) in piv:
                push_piv(piv[(tf, side)], price)
            ev_i += 1
        swings = [s for s in swings if not ((s["hi"] and H[k] >= s["lvl"]) or (not s["hi"] and L[k] <= s["lvl"]))]
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
        for s in swings:
            if not s["anc"]:
                for tf in reversed(a_tfs):                 # highest timeframe name first (1D, 4h, 1h)
                    if s["lvl"] in piv[(tf, s["hi"])]:
                        s["anc"], s["anc_tf"] = True, tf
                        break
        groups = []
        for side in (True, False):
            for g in star_groups([s for s in swings if s["hi"] == side]):
                key = f"{len(g)}S|" + ("H" if side else "L") + "|" + "|".join(x["t"].isoformat() for x in g)
                if key not in formed:
                    formed[key] = (bar_end, side, [dict(x) for x in g])
                groups.append((key, side, g))
    return groups, formed


def fmt_t(ts, tf=""):
    return ts.strftime("%d %b %Y") if tf in ("1D", "1W", "1M") else ts.strftime("%d %b %H:%M")


def star_message(name, tf, side, g):
    lines = [f"{'🔴' if side else '🟢'} <b>{name} {tf}</b> {'★' * len(g)}",
             f"★ ({g[0]['anc_tf']}) {g[0]['lvl']:.2f} · {fmt_t(g[0]['t'], tf)}"]
    for i, x in enumerate(g[1:], start=2):
        prev = g[i - 2]["lvl"]
        lines.append(f"{'★' * i} {x['lvl']:.2f} · {fmt_t(x['t'], tf)}   ({abs(x['lvl'] - prev) / prev * 100:.2f}%)")
    return "\n".join(lines)


# ───────────────────────── 2) CONSOLIDATION BOX BREAKS ─────────────────────────
class _Rec:
    __slots__ = ("id", "hi", "lo", "ft", "fi", "rt", "ri", "live", "brk", "last", "ev", "chg")
    def __init__(s, id, hi, lo, ft, fi, rt, ri):
        s.id, s.hi, s.lo, s.ft, s.fi, s.rt, s.ri, s.live, s.brk, s.last, s.ev, s.chg = id, hi, lo, ft, fi, rt, ri, True, 0, 0, None, 0


def _ovl(t1, r1, h1, l1, t2, r2, h2, l2, tolT, tolP):
    return max(t1, t2) <= min(r1, r2) + tolT and max(l1, l2) <= min(h1, h2) + tolP


def mtf_break_events(c, tf_min, length=1, min_sw=2, atr_len=5, atr_mult=2.5, merge=True, merge_tol=1, mintick=0.05, max_recs=40):
    """Port of TRIDENT f_mcMulti. c = candles of ONE timeframe (index = candle start, closed candles only).
    Returns a list of break events: dict(k, t, side(+1 U / -1 D), hi, lo, ft, fi, bars)."""
    idx = c.index; n = len(c)
    O, H, L, C = (c[x].values.astype(float) for x in ("open", "high", "low", "close"))
    day_reset = tf_min <= 60                       # 4h and 1D: a chain may cross days
    EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
    ts = ((idx - EPOCH) // pd.Timedelta(seconds=1)).values.astype(np.int64)
    day_close = np.array([int((d.normalize() + pd.Timedelta(hours=15, minutes=30) - EPOCH) // pd.Timedelta(seconds=1)) for d in idx])
    tr = np.maximum(H - L, np.maximum(np.abs(H - np.r_[C[0], C[:-1]]), np.abs(L - np.r_[C[0], C[:-1]])))
    tr[0] = H[0] - L[0]
    av = np.full(n, np.nan); a = None
    for i in range(n):
        if i < atr_len - 1:
            continue
        a = tr[:atr_len].mean() if i == atr_len - 1 else (a * (atr_len - 1) + tr[i]) / atr_len
        av[i] = a
    tolT = merge_tol * tf_min * 60; tolP = mintick
    chAct = chStop = False; chCnt = 0; chH = chL = None; chFI = chLI = chFT = chDE = None; curId = -1; idc = 0
    recs, events = [], []

    def find(i):
        for r in recs:
            if r.id == i:
                return r
        return None

    def note(r, kk, tt):
        events.append(dict(k=kk, t=int(tt), side=r.brk, hi=r.hi, lo=r.lo, ft=int(r.ft), fi=r.fi, bars=kk - r.fi + 1))

    for k in range(n):
        t = ts[k]; cl = C[k]
        for r in recs:                                                   # (1) breaks
            if r.live:
                if cl > r.hi or cl < r.lo:
                    r.live = False; r.brk = 1 if cl > r.hi else -1; r.last = r.brk; r.ev = t; r.rt = t; r.ri = k; r.chg = k
                    note(r, k, t)
                else:
                    r.rt = t; r.ri = k
            elif r.brk in (1, -1):
                if (r.brk == 1 and cl < r.lo) or (r.brk == -1 and cl > r.hi):
                    r.last = -r.brk; r.brk = 2; r.ev = t
        if curId >= 0:
            c0 = find(curId)
            if c0 is None or not c0.live:
                chStop = True
        ph = H[k - length] if k >= 2 * length and all(H[k - length - j] < H[k - length] and H[k - length + j] < H[k - length] for j in range(1, length + 1)) else None
        pv = L[k - length] if k >= 2 * length and all(L[k - length - j] > L[k - length] and L[k - length + j] > L[k - length] for j in range(1, length + 1)) else None
        for isH in (True, False):                                        # (2) confirmed swings
            sv = ph if isH else pv
            if sv is None:
                continue
            sIdx = k - length; sT = ts[k - length]; sDE = day_close[k - length]
            nH = (sv if chH is None else max(chH, sv)) if isH else chH
            nL = chL if isH else (sv if chL is None else min(chL, sv))
            can = False
            if chAct and not chStop:
                gapOk = sIdx - chLI <= 4
                winOk = (not day_reset) or sT < chDE
                sizeOk = np.isnan(av[k]) or nH is None or nL is None or (nH - nL) <= atr_mult * av[k]
                can = gapOk and winOk and sizeOk
            if can:
                chLI = sIdx; chCnt += 1; chH = nH; chL = nL
            else:
                chAct = True; chStop = False; chFI = sIdx; chLI = sIdx; chFT = sT; chDE = sDE; chCnt = 1
                chH = sv if isH else None; chL = None if isH else sv; curId = -1
            if chAct and not chStop and chCnt >= min_sw and chH is not None and chL is not None:
                cur = find(curId) if curId >= 0 else None
                if cur is None:
                    mrg = None
                    if merge:
                        for cand in reversed(recs):
                            if _ovl(chFT, t, chH, chL, cand.ft, cand.rt, cand.hi, cand.lo, tolT, tolP):
                                mrg = cand; break
                    if mrg is not None:
                        mrg.hi = max(mrg.hi, chH); mrg.lo = min(mrg.lo, chL)
                        if chFT < mrg.ft:
                            mrg.ft = chFT; mrg.fi = chFI
                        mrg.live = True; mrg.brk = 0; mrg.last = 0; mrg.ev = None; mrg.rt = t; mrg.ri = k
                        cur = mrg
                    else:
                        idc += 1; cur = _Rec(idc, chH, chL, chFT, chFI, t, k); recs.append(cur)
                        if len(recs) > max_recs:
                            recs.pop(0)
                    curId = cur.id; chH = cur.hi; chL = cur.lo; chFI = cur.fi; chFT = cur.ft
                    back = k - chLI - 1
                    for kb in range(back, -1, -1):
                        if back < 0:
                            break
                        cv = C[k - kb]; tb = ts[k - kb]
                        if cur.live:
                            if cv > cur.hi or cv < cur.lo:
                                cur.live = False; cur.brk = 1 if cv > cur.hi else -1; cur.last = cur.brk; cur.ev = tb; cur.rt = tb; cur.ri = k - kb; chStop = True
                                note(cur, k - kb, tb)
                        elif (cur.brk == 1 and cv < cur.lo) or (cur.brk == -1 and cv > cur.hi):
                            cur.last = -cur.brk; cur.brk = 2; cur.ev = tb
                else:
                    cur.hi = chH; cur.lo = chL; cur.rt = t; cur.ri = k
                cur.chg = k
        if merge:                                                        # (3) merge pass
            i = len(recs) - 1
            while i >= 0:
                if i < len(recs):
                    ra = recs[i]
                    if ra.chg >= k - 1:
                        j = 0; done = False
                        while j < len(recs) and not done:
                            if j != i:
                                rb = recs[j]
                                if _ovl(ra.ft, ra.rt, ra.hi, ra.lo, rb.ft, rb.rt, rb.hi, rb.lo, tolT, tolP):
                                    kp, dp = (ra, rb) if i < j else (rb, ra)
                                    dropIdx = max(i, j)
                                    lv = kp.live or dp.live; bA, bB = kp.brk, dp.brk
                                    eA = kp.ev or 0; eB = dp.ev or 0
                                    kp.hi = max(kp.hi, dp.hi); kp.lo = min(kp.lo, dp.lo)
                                    if dp.ft < kp.ft:
                                        kp.ft = dp.ft; kp.fi = dp.fi
                                    kp.rt = max(kp.rt, dp.rt); kp.ri = max(kp.ri, dp.ri)
                                    kp.brk = 0 if lv else (2 if (bA == 2 or bB == 2 or (bA != 0 and bB != 0 and bA != bB)) else (bA if bA != 0 else bB))
                                    kp.last = 0 if lv else (dp.last if eB > eA else kp.last)
                                    kp.ev = None if lv else (dp.ev if eB > eA else kp.ev)
                                    kp.live = lv
                                    if lv:
                                        kp.rt = t; kp.ri = k
                                    kp.chg = k
                                    if dp.id == curId:
                                        curId = kp.id
                                    if kp.id == curId:
                                        chH = kp.hi; chL = kp.lo; chFI = kp.fi; chFT = kp.ft; chStop = not kp.live
                                    recs.pop(dropIdx); done = True
                            j += 1
                i -= 1
    return events


def sess_span(t1, t2):
    """Pine f_sessSpan: weekdays (dates) touched between two times, at least 1."""
    d1 = pd.Timestamp(t1, unit="s", tz="UTC").tz_convert(IST).normalize().tz_localize(None)
    d2 = pd.Timestamp(t2, unit="s", tz="UTC").tz_convert(IST).normalize().tz_localize(None)
    n = sum(1 for d in pd.date_range(d1, d2) if d.weekday() < 5)
    return max(n, 1)


def box_dur(tf, bars, t0, t_end):
    """Pine f_mcDur: 1D chart = candles as days | more than one session = '2d' | under 60 min = '45m' | else hours '1.5h'."""
    if tf == "1D":
        return f"{bars}d"
    ss = sess_span(t0, t_end)
    mins = bars * TF_MIN[tf]
    if ss > 1:
        return f"{ss}d"
    if mins < 60:
        return f"{int(round(mins))}m"
    return f"{mins / 60.0:.1f}".rstrip("0").rstrip(".") + "h"


def box_message(name, tf, ev, dur, c):
    up = ev["side"] == 1
    t0 = pd.Timestamp(ev["ft"], unit="s", tz="UTC").tz_convert(IST)
    bt = c.index[ev["k"]]
    return "\n".join([
        f"{'🟢' if up else '🔴'} <b>{name} {tf} box {'▲ U' if up else '▼ D'}</b> · <b>{dur}</b>",
        f"Box {ev['lo']:.2f} – {ev['hi']:.2f}  ({ev['hi'] - ev['lo']:.0f} pts)",
        f"Closed {c['close'].iloc[ev['k']]:.2f} {'above' if up else 'below'} · {fmt_t(bt)} candle",
        f"Box started {fmt_t(t0)}"])


# ───────────────────────── 3) DARK EOB on 15m ─────────────────────────
def eob_dark_events(c, pivot_len=1, back_max=10):
    """Bar-by-bar port of TRIDENT V3.56 EOB with the sweep test and the dark (FVG) test.
    c = closed 15m candles (index = candle start). Returns one dict per EOB that became DARK:
    dir (+1 green / -1 red), sweep, swing_t, level, top, bot, dark_k (candle where it turned dark), inv_k."""
    assert pivot_len == 1
    pl = pivot_len
    O, H, L, C = (c[x].values.astype(float) for x in ("open", "high", "low", "close"))
    n = len(c); idx = c.index
    untH, untL, tkHi, tkLo, recs = [], [], [], [], []
    for k in range(n):
        tkH = tkL = False
        for u in range(len(untH) - 1, -1, -1):
            if H[k] > untH[u]:
                tkH = True; del untH[u]
        for u in range(len(untL) - 1, -1, -1):
            if L[k] < untL[u]:
                tkL = True; del untL[u]
        if tkH:
            tkHi.append(k); tkHi = tkHi[-30:]
        if tkL:
            tkLo.append(k); tkLo = tkLo[-30:]
        p = k - pl
        isSH = isSL = False
        if p - pl >= 0:
            isSH = all(H[p - j] < H[p] and H[p + j] < H[p] for j in range(1, pl + 1))
            isSL = all(L[p - j] > L[p] and L[p + j] > L[p] for j in range(1, pl + 1))
        if p >= 0 and p + 1 <= k and (isSH or isSL):
            mid = (H[p] + L[p]) / 2.0; cf = C[p + 1]
            swpH = any(x in tkHi for x in (p - 1, p, p + 1))
            swpL = any(x in tkLo for x in (p - 1, p, p + 1))
            for bear in (True, False):
                if not ((bear and isSH and cf <= mid) or ((not bear) and isSL and cf >= mid)):
                    continue
                bP = p
                for b in range(p, max(0, p - 9) - 1, -1):
                    if (C[b] > O[b]) if bear else (C[b] < O[b]):
                        bP = b; break
                bo, bc, bh, bl = O[bP], C[bP], H[bP], L[bP]
                top_b, bot_b = max(bo, bc), min(bo, bc)
                qtr = max((bh - bl) * 0.25, 0.05)
                if bear:
                    bot = bl; top = bot_b if (bot_b - bl) >= 0.05 else bl + qtr
                else:
                    top = bh; bot = top_b if (bh - top_b) >= 0.05 else bh - qtr
                recs.append(dict(dir=-1 if bear else 1, p=p, bP=bP, level=float(H[p] if bear else L[p]), top=float(top), bot=float(bot),
                                 sweep=swpH if bear else swpL, dark_k=None, inv_k=None))
        if isSH:
            untH.append(H[p]); untH = untH[-SWP_KEEP:]
        if isSL:
            untL.append(L[p]); untL = untL[-SWP_KEEP:]
        for r in recs:
            if r["inv_k"] is None and k > r["p"] and ((r["dir"] == -1 and C[k] > r["level"]) or (r["dir"] == 1 and C[k] < r["level"])):
                r["inv_k"] = k
            if r["dark_k"] is None and k - r["p"] <= max(2, pl):
                for c1 in (r["p"] - 1, r["p"], r["p"] - 3, r["p"] - 2):
                    if c1 < 0 or c1 + 2 > k:
                        continue
                    if (H[c1 + 2] < L[c1]) if r["dir"] == -1 else (L[c1 + 2] > H[c1]):
                        r["dark_k"] = k; break
    out = []
    for r in recs:
        if r["dark_k"] is None:
            continue
        if r["inv_k"] is not None and r["inv_k"] <= r["dark_k"]:
            continue                                   # invalid by the time it turned dark -> TRIDENT hides it
        r = dict(r); r["swing_t"] = idx[r["p"]]
        out.append(r)
    return out


def eob_message(name, ev, c):
    red = ev["dir"] == -1
    label = ("Dark Sweep EOB" if ev["sweep"] else "Dark EOB") + (" (red)" if red else " (green)")
    return "\n".join([
        f"{'🔴' if red else '🟢'} <b>{name} 15m · {label}</b>",
        f"Box {ev['bot']:.2f} – {ev['top']:.2f}",
        f"Swing {ev['level']:.2f} · {fmt_t(ev['swing_t'])}",
        f"Turned dark on the {fmt_t(c.index[ev['dark_k']])} candle"])


# ───────────────────────── STATE + TELEGRAM ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    cutoff = (now_ist() - pd.Timedelta(days=10)).isoformat()
    msgs = {k: v for k, v in state.get("_msgs", {}).items() if v[1] >= cutoff}
    state = {k: v for k, v in state.items() if not k.startswith("_") and v >= cutoff}
    state["_msgs"] = msgs
    state["_last_run"] = now_ist().isoformat()
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[no Telegram secrets] would send:\n" + text + "\n")
        return -1
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        print("Telegram error:", e)
        return None
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)
        return None
    return r.json()["result"]["message_id"]


def remember_msg(state, skey, mid, text):
    if DELETE_TAKEN and isinstance(mid, int) and not isinstance(mid, bool) and mid > 0:
        state.setdefault("_msgs", {})[skey] = [mid, now_ist().isoformat(), text.split("\n")[0]]


def delete_msg(mid, head):
    """Delete a bot message (Telegram allows 48 h); older ones are edited to say TAKEN."""
    if not TG_TOKEN or not TG_CHAT or mid <= 0:
        print(f"[no Telegram secrets] would delete message {mid}")
        return True
    api = f"https://api.telegram.org/bot{TG_TOKEN}"
    try:
        r = requests.post(f"{api}/deleteMessage", json={"chat_id": TG_CHAT, "message_id": mid}, timeout=20)
        if r.ok:
            return True
        r = requests.post(f"{api}/editMessageText", timeout=20, json={
            "chat_id": TG_CHAT, "message_id": mid, "parse_mode": "HTML", "text": f"❌ TAKEN — <s>{head}</s>"})
        return r.ok or "not found" in r.text or "not modified" in r.text
    except Exception as e:
        print("Telegram delete error:", e)
        return False


def delete_taken(state, prefix, live_keys):
    if not DELETE_TAKEN:
        return 0
    msgs, n = state.get("_msgs", {}), 0
    for skey, (mid, _, head) in list(msgs.items()):
        if skey.startswith(prefix) and skey[len(prefix):] not in live_keys:
            if delete_msg(mid, head):
                msgs.pop(skey); n += 1
    return n


def dispatch(state, skey, text, track=False):
    mid = send(text)
    if mid:
        state[skey] = now_ist().isoformat()
        if track:
            remember_msg(state, skey, mid, text)
        return 1
    return 0


# ───────────────────────── MAIN ─────────────────────────
def run_symbol(name, now, state):
    today = now.date()
    sent = 0
    m5 = fetch(name, 5, DAYS_5M) if "5m" in STAR_TFS else None
    need_60 = any(t in ("1h", "4h", "1D", "1W", "1M") for t in STAR_TFS) or bool(BOX_TFS)
    m60 = fetch(name, 60, DAYS_60M) if need_60 else None
    m15 = fetch(name, 15, DAYS_15M) if (EOB_ON or "15m" in STAR_TFS) else None
    d1 = fetch_daily(name, m60, DAYS_1D) if any(t in ("1D", "1W", "1M") for t in STAR_TFS) else None

    # ---- 1) stars ----
    for tf in STAR_TFS:
        try:
            if tf == "5m":
                chart = build_tf(m5, 5, "5m", now)
                htf = {t: build_tf(m5, 5, t, now) for t in ("1h", "4h", "1D")}
            elif tf == "15m":
                chart = build_tf(m15, 15, "15m", now)
                htf = {t: build_tf(m15, 15, t, now) for t in ("1h", "4h", "1D")}
            elif tf in ("1D", "1W", "1M"):
                chart = build_period(d1, tf, now)
                htf = {t: build_period(d1, t, now) for t in ("1W", "1M", "3M")}
            elif tf == "1h":
                chart = build_tf(m60, 60, "1h", now)
                htf = {t: build_tf(m60, 60, t, now) for t in ("4h", "1D")}
            elif tf == "4h":
                chart = build_tf(m60, 60, "4h", now)
                htf = {"1D": build_tf(m60, 60, "1D", now)}
            else:
                print(f"star timeframe {tf} not supported"); continue
            if len(chart) < 5:
                continue
            events = []
            for atf in anchor_tfs(tf):
                events += htf_pivot_events(htf[atf], atf)
            events.sort(key=lambda x: x[0])
            groups, formed = simulate(chart, events, tf)
            live_keys = {key for key, _, _ in groups}
            for key, (f_at, side, g) in sorted(formed.items(), key=lambda kv: kv[1][0]):
                skey = f"{name}|{tf}|{key}"
                recent = f_at.date() == today if tf not in ("1D", "1W", "1M") else (now - f_at) <= pd.Timedelta(days=4)
                if skey in state or not recent:
                    continue
                if key in live_keys:
                    sent += dispatch(state, skey, star_message(name, tf, side, g), track=True)
                else:
                    state[skey] = now.isoformat()          # broke before any check saw it: no message
            gone = delete_taken(state, f"{name}|{tf}|", live_keys)
            if gone:
                print(f"{name} {tf}: deleted {gone} alert(s) whose group was taken")
            print(f"{name} {tf} stars: {len(chart)} candles, {sum(len(g) == 3 for _, _, g in groups)} 3-star, {sum(len(g) == 2 for _, _, g in groups)} 2-star live")
        except Exception as e:
            print(f"{name} {tf} stars: error {e}")

    # ---- 2) box breaks ----
    for tf in BOX_TFS:
        try:
            c = build_tf(m60, 60, tf, now)
            if len(c) < 20:
                continue
            evs = mtf_break_events(c, TF_MIN[tf])
            n_today = 0
            for ev in evs:
                bt = c.index[ev["k"]]
                bend = c["end"].iloc[ev["k"]]
                if bend.date() != today or ev["bars"] < BOX_MIN_BARS[tf]:
                    continue
                n_today += 1
                dur = box_dur(tf, ev["bars"], ev["ft"], int(bt.timestamp()))
                skey = f"{name}|box{tf}|{ev['ft']}|{ev['fi']}|{ev['side']}|{int(bt.timestamp())}"
                if skey in state:
                    continue
                sent += dispatch(state, skey, box_message(name, tf, ev, dur, c))
            print(f"{name} {tf} boxes: {len(c)} candles, {n_today} break(s) today")
        except Exception as e:
            print(f"{name} {tf} boxes: error {e}")

    # ---- 3) dark EOB on 15m ----
    if EOB_ON:
        try:
            c = build_tf(m15, 15, "15m", now)
            evs = eob_dark_events(c)
            n_today = 0
            for ev in evs:
                dend = c["end"].iloc[ev["dark_k"]]
                if dend.date() != today:
                    continue
                n_today += 1
                skey = f"{name}|eob15m|{ev['swing_t'].isoformat()}|{ev['dir']}"
                if skey in state:
                    continue
                sent += dispatch(state, skey, eob_message(name, ev, c))
            print(f"{name} 15m EOB: {len(c)} candles, {n_today} dark EOB(s) today")
        except Exception as e:
            print(f"{name} 15m EOB: error {e}")
    return sent


def main():
    now = now_ist()
    force = os.getenv("FORCE", "0") == "1"
    if not force and (now.weekday() >= 5 or not (dtime(9, 15) <= now.time() <= dtime(15, 45))):
        print("outside market hours, exit")
        return 0
    if not LOCAL_DIR and not (DHAN_TOKEN and DHAN_CLIENT):
        print("DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID missing")
        return 1
    state = load_state()
    sent = 0
    for name in SYMBOLS:
        try:
            sent += run_symbol(name, now, state)
        except Exception as e:
            print(f"{name}: data error {e}")
    save_state(state)
    print(f"sent {sent} alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
