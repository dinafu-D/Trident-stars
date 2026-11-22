"""
TRIDENT V3.57 consolidation box (chart engine) + market trend  ->  Telegram alerts, NIFTY / SENSEX 5m.

Con box = same rules as TRIDENT's chart con boxes (default settings):
  * swings: 1 candle left / right
  * a chain grows while the next swing comes within 4 candles, stays on the same trading day,
    and the box height stays within 2.5 x ATR(5); 2+ swings with a high and a low = box (highest high - lowest low)
  * overlapping / touching boxes merge into one
  * the box keeps extending until a candle CLOSES outside it: above = U, below = D;
    a later close beyond the other side = B
  * "formed" alert = when the box shows its label on TRIDENT ("Min Bars" = 9 candles, i.e. 45 min on 5m)

Trend (TRIDENT Market Structure, Structure Timeframe = Chart):
  * UP = last swing high HH and last swing low HL | DOWN = LH and LL | otherwise SIDEWAYS
  * confirmed = 5m close on the trend side of EMA 6
"""

import os

import pandas as pd

IST = "Asia/Kolkata"
TF_MIN = 5
MIN_BARS = int(os.getenv("CB_MIN_BARS", "9"))          # TRIDENT "Min Bars To Show Duration Text"
GAP_MAX = 4                                             # next swing within 4 candles
ATR_LEN, ATR_MULT = 5, 2.5
EMA_LEN = int(os.getenv("EMA_LEN", "6"))
WINDOW_MIN = int(os.getenv("CB_WINDOW_MIN", "20"))      # only send events from the last N minutes (no old flood)
CB_ON = os.getenv("CONBOX_ALERTS", "1") == "1"
TREND_ON = os.getenv("TREND_ALERTS", "1") == "1"
TICK = {"NIFTY": 0.05, "SENSEX": 0.01}


def _ms(ts):
    return int(pd.Timestamp(ts).value // 1_000_000)


# ───────────────────────── indicators ─────────────────────────
def _atr(h, l, c, n):
    """Pine ta.atr: RMA of true range, seeded with the SMA of the first n values."""
    out, tr = [None] * len(c), []
    for i in range(len(c)):
        t = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        tr.append(t)
        if i == n - 1:
            out[i] = sum(tr) / n
        elif i >= n:
            out[i] = (out[i - 1] * (n - 1) + t) / n
    return out


def _ema(c, n):
    """Pine ta.ema: seeded with the SMA of the first n closes."""
    out, a = [None] * len(c), 2 / (n + 1)
    for i in range(len(c)):
        if i == n - 1:
            out[i] = sum(c[:n]) / n
        elif i >= n:
            out[i] = a * c[i] + (1 - a) * out[i - 1]
    return out


def _overlap(t1, r1, h1, l1, t2, r2, h2, l2, tolT, tolP):
    return max(t1, t2) <= min(r1, r2) + tolT and max(l1, l2) <= min(h1, h2) + tolP


def _sessions(t1, t2):
    """Trading days (weekdays) a box touches."""
    d1, d2 = pd.Timestamp(t1, unit="ms", tz=IST).normalize(), pd.Timestamp(t2, unit="ms", tz=IST).normalize()
    return max(1, sum(1 for d in pd.date_range(d1, d2, freq="D") if d.weekday() < 5))


def _dur(rec):
    """TRIDENT box text: 45m / 1.5h / 2d"""
    bars = rec["rightIdx"] - rec["leftIdx"] + 1
    ss = _sessions(rec["firstT"], rec["rightT"])
    mins = bars * TF_MIN
    if ss > 1:
        return f"{ss}d"
    if mins < 60:
        return f"{int(round(mins))}m"
    h = mins / 60
    return (f"{h:.1f}".rstrip("0").rstrip(".")) + "h"


# ───────────────────────── replay ─────────────────────────
def replay(m5, tick):
    """Walk the closed 5m candles like TRIDENT. Returns a list of events (oldest first)."""
    t = [_ms(x) for x in m5.index]
    t_idx = {x: k for k, x in enumerate(t)}
    end = [x + TF_MIN * 60000 for x in t]
    H, L, C = list(m5["High"]), list(m5["Low"]), list(m5["Close"])
    n = len(C)
    dayEnd = [_ms(pd.Timestamp(x).normalize() + pd.Timedelta(hours=15, minutes=30)) for x in m5.index]
    atr = _atr(H, L, C, ATR_LEN)
    ema = _ema(C, EMA_LEN)
    tolT, tolP = 1 * TF_MIN * 60000, tick

    recs = []
    cAct = cStopped = False
    cFirstIdx = cFirstT = cLastIdx = cLastT = cDayEnd = cCnt = 0
    cHi = cLo = None
    cBox = curRec = None
    curId, recCounter = -1, 0
    fm_ids, bk_prev = set(), {}
    # trend
    lastH = lastL = None
    hT = lT = 0
    pivH = pivL = None
    trLastAl = 0
    events = []

    for i in range(n):
        close = C[i]
        # ── (A) every box: close outside -> U / D | later close beyond the other side -> B | else extend
        for rr in reversed(recs):
            if rr["live"] and not rr["dead"]:
                if close > rr["hi"] or close < rr["lo"]:
                    rr["live"] = False
                    rr["brk"] = rr["brk1"] = 1 if close > rr["hi"] else -1
                    rr["rightT"], rr["rightIdx"], rr["chg"] = t[i], i, i
                else:
                    rr["rightIdx"], rr["rightT"], rr["chg"] = i, t[i], i
            elif not rr["dead"] and rr["brk"] in (1, -1):
                if (rr["brk"] == 1 and close < rr["lo"]) or (rr["brk"] == -1 and close > rr["hi"]):
                    rr["brk"], rr["chg"] = 2, i
        if curRec is not None and not curRec["live"]:
            cStopped = True
            if curRec["dead"]:
                cBox = None

        # ── (B) swings confirmed on this candle (pivot at i-1, 1 left / 1 right)
        p = i - 1
        sw = []
        if p >= 1:
            if H[p] > H[p - 1] and H[p] > H[p + 1]:
                sw.append((True, H[p]))
            if L[p] < L[p - 1] and L[p] < L[p + 1]:
                sw.append((False, L[p]))
        for isH, sv in sw:
            sIdx, sT, sDE = p, t[p], dayEnd[p]
            nHi = (sv if cHi is None else max(cHi, sv)) if isH else cHi
            nLo = cLo if isH else (sv if cLo is None else min(cLo, sv))
            can = False
            if cAct and not cStopped:
                gapOk = sIdx - cLastIdx <= GAP_MAX
                winOk = sT < cDayEnd
                sizeOk = atr[i] is None or nHi is None or nLo is None or (nHi - nLo) <= ATR_MULT * atr[i]
                can = gapOk and winOk and sizeOk
            if can:
                cLastIdx, cLastT, cCnt, cHi, cLo = sIdx, sT, cCnt + 1, nHi, nLo
            else:
                cAct, cStopped = True, False
                cFirstIdx, cFirstT, cLastT, cDayEnd, cLastIdx, cCnt = sIdx, sT, sT, sDE, sIdx, 1
                cHi, cLo = (sv, None) if isH else (None, sv)
                cBox = curRec = None
                curId = -1
            if cAct and not cStopped and cCnt >= 2 and cHi is not None and cLo is not None:
                cRightT, rI = t[i], i
                if cBox is None:
                    mrg = None
                    for cand in reversed(recs):
                        if not cand["dead"] and _overlap(cFirstT, cRightT, cHi, cLo, cand["firstT"], cand["rightT"],
                                                         cand["hi"], cand["lo"], tolT, tolP):
                            mrg = cand
                            break
                    if mrg is not None:
                        cFirstIdx, cFirstT, cDayEnd = mrg["leftIdx"], mrg["firstT"], mrg["dayEnd"]
                        cHi, cLo = max(cHi, mrg["hi"]), min(cLo, mrg["lo"])
                        cLastT = max(cLastT, mrg["lastT"])
                        mrg.update(live=True, brk=0, brk1=0, leftIdx=cFirstIdx, rightIdx=rI)
                        cBox = curRec = mrg
                        curId = mrg["id"]
                    else:
                        recCounter += 1
                        curRec = dict(id=recCounter, leftIdx=cFirstIdx, firstT=cFirstT, lastT=cLastT, dayEnd=cDayEnd,
                                      rightT=cRightT, rightIdx=rI, hi=cHi, lo=cLo, live=True, dead=False, chg=0,
                                      brk=0, brk1=0)
                        cBox = curRec
                        curId = recCounter
                        recs.append(curRec)
                        if len(recs) > 100:
                            recs.pop(0)
                    # closes already printed after the last swing
                    hitJ = -1
                    for j in range(cLastIdx + 1, i + 1):
                        if C[j] > cHi or C[j] < cLo:
                            curRec["rightIdx"] = j
                            cStopped = True
                            curRec["live"] = False
                            curRec["brk"] = curRec["brk1"] = 1 if C[j] > cHi else -1
                            hitJ = j
                            break
                    if hitJ >= 0:
                        for j2 in range(hitJ + 1, i + 1):
                            if (curRec["brk"] == 1 and C[j2] < cLo) or (curRec["brk"] == -1 and C[j2] > cHi):
                                curRec["brk"] = 2
                                break
                else:
                    cBox["rightIdx"] = rI
                if curRec is not None:
                    curRec.update(hi=cHi, lo=cLo, rightT=cRightT, lastT=cLastT, chg=i)
                    if curRec["live"]:
                        curRec["rightIdx"] = rI

        # ── (C) merge pass: boxes changed on this candle vs all others (the older box keeps its id)
        mi2 = len(recs) - 1
        while mi2 >= 0:
            if mi2 < len(recs):
                ra = recs[mi2]
                if not ra["dead"] and ra["chg"] >= i - 1:
                    for mj in range(len(recs)):
                        if mj == mi2:
                            continue
                        rb = recs[mj]
                        if not rb["dead"] and _overlap(ra["firstT"], ra["rightT"], ra["hi"], ra["lo"],
                                                       rb["firstT"], rb["rightT"], rb["hi"], rb["lo"], tolT, tolP):
                            kp, dp = (ra, rb) if mi2 < mj else (rb, ra)
                            drop = max(mi2, mj)
                            kp["hi"], kp["lo"] = max(kp["hi"], dp["hi"]), min(kp["lo"], dp["lo"])
                            kp["leftIdx"] = min(kp["leftIdx"], dp["leftIdx"])
                            kp["firstT"] = min(kp["firstT"], dp["firstT"])
                            kp["rightT"] = max(kp["rightT"], dp["rightT"])
                            kp["lastT"] = max(kp["lastT"], dp["lastT"])
                            kp["live"] = kp["live"] or dp["live"]
                            bA, bB = kp["brk"], dp["brk"]
                            kp["brk"] = 0 if kp["live"] else (2 if (bA == 2 or bB == 2 or (bA and bB and bA != bB))
                                                             else (bA if bA else bB))
                            kp["brk1"] = 0 if kp["live"] else (bA if bA in (1, -1) else bB if bB in (1, -1)
                                                              else (kp["brk1"] or dp["brk1"]))
                            kp["chg"] = i
                            kp["rightIdx"] = i if kp["live"] else t_idx.get(kp["rightT"], i)   # TRIDENT: bar at rightT
                            dp["live"], dp["dead"] = False, True
                            if dp["id"] == curId:
                                curId, curRec, cBox = kp["id"], kp, kp
                            if kp["id"] == curId:
                                cHi, cLo, cFirstIdx, cFirstT, cLastT = kp["hi"], kp["lo"], kp["leftIdx"], kp["firstT"], kp["lastT"]
                                cStopped = not kp["live"]
                            recs.pop(drop)
                            break
            mi2 -= 1

        # ── (D) trend: HH / LH, HL / LL from the same 1-left / 1-right swings + EMA confirmation
        for isH, sv in sw:
            if isH:
                if lastH is not None and sv != lastH:
                    hT = 1 if sv > lastH else -1
                lastH, pivH = sv, sv
            else:
                if lastL is not None and sv != lastL:
                    lT = 1 if sv > lastL else -1
                lastL, pivL = sv, sv
        trDir = 1 if (hT == 1 and lT == 1) else -1 if (hT == -1 and lT == -1) else 0
        eSide = 0 if ema[i] is None else (1 if close > ema[i] else -1 if close < ema[i] else 0)
        trConf = trDir if (trDir != 0 and eSide == trDir) else 0
        trend = dict(dir=trDir, hT=hT, lT=lT, eSide=eSide, ema=ema[i], conf=trConf, lastH=lastH, lastL=lastL)
        if trConf != 0 and trConf != trLastAl:
            events.append(dict(kind="T", end=end[i], key=f"T|{t[i]}", trend=trend, close=close))
            trLastAl = trConf

        # ── (E) con box events on this candle (box must show its label: Min Bars)
        for r in recs:
            if r["chg"] != i or r["dead"]:
                continue
            bars = r["rightIdx"] - r["leftIdx"] + 1
            if bars < MIN_BARS:
                continue
            snap = dict(hi=r["hi"], lo=r["lo"], firstT=r["firstT"], dur=_dur(r))
            if r["id"] not in fm_ids:
                fm_ids.add(r["id"])
                events.append(dict(kind="F", end=end[i], key=f"F|{r['firstT']}", box=snap, trend=trend, close=close))
            prev = bk_prev.get(r["id"], 0)
            if r["brk"] != prev:
                bk_prev[r["id"]] = r["brk"]
                if r["brk"] in (1, -1, 2):
                    k = "U" if r["brk"] == 1 else "D" if r["brk"] == -1 else "B"
                    events.append(dict(kind=k, end=end[i], key=f"{k}|{r['firstT']}|{t[i]}", box=snap,
                                       trend=trend, close=close))
    return events


# ───────────────────────── messages ─────────────────────────
def _fmt_t(ms):
    return pd.Timestamp(ms, unit="ms", tz=IST).strftime("%d %b %H:%M")


def _trend_line(tr):
    d = "UP ▲" if tr["dir"] == 1 else "DOWN ▼" if tr["dir"] == -1 else "SIDEWAYS"
    s = ("HH" if tr["hT"] == 1 else "LH" if tr["hT"] == -1 else "-") + " · " + \
        ("HL" if tr["lT"] == 1 else "LL" if tr["lT"] == -1 else "-")
    e = "above" if tr["eSide"] == 1 else "below" if tr["eSide"] == -1 else "on"
    ok = " ✓" if tr["conf"] != 0 else ""
    return f"Trend: {d} ({s}) · {e} EMA{EMA_LEN}{ok}"


def message(name, ev):
    tr = ev["trend"]
    if ev["kind"] == "T":
        up = tr["conf"] == 1
        return "\n".join([
            f"{'🟢' if up else '🔴'} <b>{name} 5m</b> TREND {'UP ▲' if up else 'DOWN ▼'}",
            f"{'HH' if tr['hT'] == 1 else 'LH'} {tr['lastH']:.2f} · {'HL' if tr['lT'] == 1 else 'LL'} {tr['lastL']:.2f}",
            f"Close {ev['close']:.2f} {'above' if up else 'below'} EMA{EMA_LEN} {tr['ema']:.2f} ✓",
            f"{_fmt_t(ev['end'] - TF_MIN * 60000)}"])
    b = ev["box"]
    pct = (b["hi"] - b["lo"]) / b["lo"] * 100 if b["lo"] else 0
    if ev["kind"] == "F":
        head = f"⬜ <b>{name} 5m</b> CON BOX"
        mid = [f"High {b['hi']:.2f} · Low {b['lo']:.2f} ({pct:.2f}%)",
               f"Since {_fmt_t(b['firstT'])} · {b['dur']}"]
    else:
        head = {"U": f"🟢 <b>{name} 5m</b> CON BOX — BREAK UP ⬆",
                "D": f"🔴 <b>{name} 5m</b> CON BOX — BREAK DOWN ⬇",
                "B": f"🟣 <b>{name} 5m</b> CON BOX — B (both sides)"}[ev["kind"]]
        mid = [f"Box {b['lo']:.2f} – {b['hi']:.2f} ({pct:.2f}%) · {b['dur']}",
               f"Close {ev['close']:.2f} · {_fmt_t(ev['end'] - TF_MIN * 60000)}"]
    return "\n".join([head] + mid + [_trend_line(tr)])


# ───────────────────────── run (called from trident_star_alert.main) ─────────────────────────
def run(name, m5, state, now, send):
    if not (CB_ON or TREND_ON):
        return 0
    closed = m5[[(_ms(x) + TF_MIN * 60000) <= _ms(now) for x in m5.index]]
    if len(closed) < 10:
        return 0
    events = replay(closed, TICK.get(name, 0.05))
    cutoff = _ms(now) - WINDOW_MIN * 60000
    sent = 0
    for ev in events:
        if ev["kind"] == "T" and not TREND_ON:
            continue
        if ev["kind"] != "T" and not CB_ON:
            continue
        skey = f"{name}|cb5|{ev['key']}"
        if skey in state or ev["end"] < cutoff:
            continue
        mid = send(message(name, ev))
        if mid:
            state[skey] = now.isoformat()
            sent += 1
    print(f"{name} con box / trend: {sum(e['kind'] == 'F' for e in events)} boxes formed in history, sent {sent}")
    return sent
