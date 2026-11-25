"""
TRIDENT V3.57 EOB  ->  Telegram alerts, NIFTY / SENSEX 1h (EOB / Swing timeframe = Chart = 1h).

EOB rule (same as TRIDENT):
  * bearish (red) EOB : swing high candle (1 left / 1 right) and the NEXT candle closes at or below its midpoint
  * bullish (green) EOB: swing low candle and the next candle closes at or above its midpoint
  * box candle = the last GREEN candle (red EOB) / last RED candle (green EOB), searched from the swing candle back
    up to 10 candles (none -> the swing candle); zone = its lower wick (red) / upper wick (green),
    or 25 % of its range when there is no wick
  * invalid = a candle CLOSES beyond the swing candle's high (red) / low (green)  -> alert message deleted
Colours:
  * sweep  : the swing candle, the candle before or the candle after was the FIRST to take (wick beyond)
             an older untaken swing on the same side           -> bull BLUE / bear PURPLE
  * dark   : a 3-candle FVG in the EOB direction uses the EOB candle or the candle before it as candle 1 or 3
             (known at most 2 candles after the swing)          -> DARK ... ; the alert is edited + a short reply sent
  * normal : GREEN / RED
"""

import os

import pandas as pd

IST = "Asia/Kolkata"
EOB_ON = os.getenv("EOB_ALERTS", "1") == "1"
TF = "1h"
TF_MS = 3600 * 1000
WINDOW_MIN = int(os.getenv("EOB_WINDOW_MIN", "30"))     # only send EOBs formed in the last N minutes (no old flood)
SWEEP_KEEP = 300                                        # untaken swings remembered per side
TICK = {"NIFTY": 0.05, "SENSEX": 0.01}

#            normal            dark                 (emoji, name)
COLOURS = {("bull", False, False): ("🟩", "GREEN"),
           ("bull", True, False):  ("🟢", "DARK GREEN"),
           ("bear", False, False): ("🟥", "RED"),
           ("bear", True, False):  ("🔴", "DARK RED"),
           ("bull", False, True):  ("🟦", "BLUE"),
           ("bull", True, True):   ("🔵", "DARK BLUE"),
           ("bear", False, True):  ("🟪", "PURPLE"),
           ("bear", True, True):   ("🟣", "DARK PURPLE")}


def _ms(ts):
    return int(pd.Timestamp(ts).value // 1_000_000)


def _closed_1h(h1, now):
    """Closed 1h candles only (last candle of the day 15:15-15:30)."""
    keep = []
    for x in h1.index:
        end = min(x + pd.Timedelta(hours=1), x.normalize() + pd.Timedelta(hours=15, minutes=30))
        keep.append(end <= now)
    return h1[keep]


def replay(h1, tick):
    """Walk the closed 1h candles like TRIDENT. Returns every EOB (dicts), oldest first."""
    t = [_ms(x) for x in h1.index]
    O, H, L, C = list(h1["Open"]), list(h1["High"]), list(h1["Low"]), list(h1["Close"])
    n = len(C)
    uh, ul = [], []            # untaken swing highs / lows (sweep check)
    tkH, tkL = set(), set()    # candles that took an older swing
    eobs, active = [], []
    for k in range(n):
        # (1) does candle k take older untaken swings?
        if any(H[k] > v for v in uh):
            tkH.add(k)
            uh = [v for v in uh if not H[k] > v]
        if any(L[k] < v for v in ul):
            tkL.add(k)
            ul = [v for v in ul if not L[k] < v]
        # (2) swing at k-1 (1 left / 1 right, strict)
        p = k - 1
        isSH = isSL = False
        if p >= 1:
            isSH = H[p] > H[p - 1] and H[p] > H[p + 1]
            isSL = L[p] < L[p - 1] and L[p] < L[p + 1]
        # (3) EOB: next candle (k) closes beyond the swing candle's midpoint
        if p >= 1:
            mid = (H[p] + L[p]) / 2.0
            for bear in (True, False):
                if not ((bear and isSH and C[k] <= mid) or ((not bear) and isSL and C[k] >= mid)):
                    continue
                bP = p
                for b in range(p, max(0, p - 9) - 1, -1):          # last green (red EOB) / last red (green EOB)
                    if (C[b] > O[b]) if bear else (C[b] < O[b]):
                        bP = b
                        break
                bO, bH, bL, bC = O[bP], H[bP], L[bP], C[bP]
                bodyTop, bodyBot = max(bO, bC), min(bO, bC)
                qtr = max((bH - bL) * 0.25, tick)
                if bear:
                    bot, top = bL, (bodyBot if (bodyBot - bL) >= tick else bL + qtr)
                else:
                    top, bot = bH, (bodyTop if (bH - bodyTop) >= tick else bH - qtr)
                swp = any(x in (tkH if bear else tkL) for x in (p - 1, p, p + 1))
                rec = dict(bear=bear, sw=p, swT=t[p], lvl=H[p] if bear else L[p], top=top, bot=bot, boxT=t[bP],
                           swp=swp, hp=False, hpAt=None, made=k, madeEnd=t[k] + TF_MS, invalid=False)
                eobs.append(rec)
                active.append(rec)
                if len(active) > 100:
                    active.pop(0)
        # (4) new swings join the untaken lists (checked from the next candle)
        if isSH:
            uh.append(H[p])
            uh = uh[-SWEEP_KEEP:]
        if isSL:
            ul.append(L[p])
            ul = ul[-SWEEP_KEEP:]
        # (5) dark EOB: FVG in the EOB direction with the EOB candle (E) or E-1 as candle 1 or 3
        for r in active:
            if r["hp"] or k - r["sw"] > 2:
                continue
            for c1 in (r["sw"] - 1, r["sw"], r["sw"] - 3, r["sw"] - 2):
                if c1 >= 0 and c1 + 2 <= k:
                    if (H[c1 + 2] < L[c1]) if r["bear"] else (L[c1 + 2] > H[c1]):
                        r["hp"], r["hpAt"] = True, t[k] + TF_MS
                        break
        # (6) invalidation: a close beyond the swing high (red) / low (green), from the candle after the swing
        for r in list(active):
            if k > r["sw"] and ((C[k] > r["lvl"]) if r["bear"] else (C[k] < r["lvl"])):
                r["invalid"] = True
                active.remove(r)
    return eobs, active


def _fmt(ms):
    return pd.Timestamp(ms, unit="ms", tz=IST).strftime("%d %b %H:%M")


def message(name, r):
    """🟪 NIFTY 1h · EOB PURPLE / Bearish · sweep / Zone / Swing high / Invalid if ..."""
    emo, col = COLOURS[("bear" if r["bear"] else "bull", r["hp"], r["swp"])]
    kind = ("Bearish (sell zone)" if r["bear"] else "Bullish (buy zone)") + \
           (" · swept liquidity" if r["swp"] else "") + (" · FVG followed" if r["hp"] else "")
    side = "high" if r["bear"] else "low"
    return "\n".join([
        f"{emo} <b>{name} {TF} · EOB {col}</b>",
        kind,
        f"Zone: {r['bot']:.2f} – {r['top']:.2f}",
        f"Swing {side}: {r['lvl']:.2f} · {_fmt(r['swT'])}",
        f"Invalid if a {TF} candle closes {'above' if r['bear'] else 'below'} {r['lvl']:.2f}"])


def run(name, h1, state, now, send, edit, remember, delete_taken):
    if not EOB_ON or h1 is None or "Open" not in h1:
        return 0
    bars = _closed_1h(h1, now)
    if len(bars) < 3:
        return 0
    eobs, active = replay(bars, TICK.get(name, 0.05))
    prefix = f"{name}|eob1h|"
    cutoff = _ms(now) - WINDOW_MIN * 60000
    msgs = state.setdefault("_msgs", {})
    sent = 0
    for r in eobs:
        key = f"{'S' if r['bear'] else 'B'}|{r['swT']}"
        skey = prefix + key
        if skey not in state:
            # new EOB: send it if it formed recently and is still valid
            if r["invalid"] or r["madeEnd"] < cutoff:
                continue
            txt = message(name, r)
            mid = send(txt)
            if mid:
                state[skey] = now.isoformat()
                if r["hp"]:
                    state[skey + "|hp"] = now.isoformat()
                remember(state, skey, mid, txt)
                sent += 1
        elif r["hp"] and (skey + "|hp") not in state and not r["invalid"]:
            # turned DARK after the alert: update the alert and reply with the new colour
            mid = msgs[skey][0] if skey in msgs else None
            txt = message(name, r)
            if mid:
                edit(mid, txt)
                msgs[skey][2] = txt.split("\n")[0]
            emo, col = COLOURS[("bear" if r["bear"] else "bull", True, r["swp"])]
            rtxt = f"{emo} <b>{name} {TF} · EOB {_fmt(r['swT'])} is now {col}</b> (FVG followed)"
            rid = send(rtxt, reply_to=mid)
            if rid:
                remember(state, skey + "|re", rid, rtxt)          # deleted together with the alert later
            state[skey + "|hp"] = now.isoformat()
            sent += 1
    # delete alerts whose EOB became invalid (a close beyond its swing)
    live = {f"{'S' if r['bear'] else 'B'}|{r['swT']}" for r in active}
    live |= {k + "|re" for k in live}
    gone = delete_taken(state, prefix, live)
    print(f"{name} 1h EOB: {len(eobs)} in history, {len(active)} valid, sent {sent}, deleted {gone}")
    return sent
