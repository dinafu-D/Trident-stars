"""
TRIDENT V3.57 consolidation box (chart engine) + market trend  ->  Telegram alerts, NIFTY / SENSEX 5m.

Con box = same rules as TRIDENT's chart con boxes (default settings):
  * swings: 1 candle left / right
  * a chain grows while the next swing comes within 4 candles, stays on the same trading day,
    and the box height stays within 2.5 x ATR(5); 2+ swings with a high and a low = box (highest high - lowest low)
  * overlapping / touching boxes merge into one
  * the box keeps extending until a candle CLOSES outside it: above = U, below = D;
    a later close beyond the other side = B
  * "formed" alert = as soon as the box exists (Min Bars rule removed; CB_MIN_BARS can bring it back)
  * 1h MTF box = the same engine on closed 1h candles (09:15-aligned); shown like TRIDENT's label:
    duration (45m / 3h / 2d) + U / D / B + x move from the latest 5m close

Trend (TRIDENT Market Structure, Structure Timeframe = Chart):
  * UP = last swing high HH and last swing low HL | DOWN = LH and LL | otherwise SIDEWAYS
  * confirmed = 5m close on the trend side of EMA 6
"""

import os

import pandas as pd

IST = "Asia/Kolkata"
TF_MIN = 5
MIN_BARS = int(os.getenv("CB_MIN_BARS", "1"))          # 1 = every box (TRIDENT label rule was 9)
MTF_ON = os.getenv("CB_MTF_1H", "1") == "1"            # 1h MTF box line in con box alerts / status
GAP_MAX = 4                                             # next swing within 4 candles
ATR_LEN, ATR_MULT = 5, 2.5
EMA_LEN = int(os.getenv("EMA_LEN", "6"))
WINDOW_MIN = int(os.getenv("CB_WINDOW_MIN", "20"))      # only send events from the last N minutes (no old flood)
CB_ON = os.getenv("CONBOX_ALERTS", "1") == "1"
TREND_ON = os.getenv("TREND_ALERTS", "0") == "1"       # old EMA trend alert (off)
TCHG_ON = os.getenv("TREND_CHANGE_ALERTS", "1") == "1"  # trend change: HH+HL = UP, LL+LH = DOWN (one opposite swing allowed)
OVL_ON = os.getenv("CB_OVERLAP", "0") == "1"            # same-day con box overlap alert (off)
CHAIN_ON = os.getenv("HLLH_ALERTS", "1") == "1"         # 3+ HL / LH in a row (one LL / HH in between allowed)
CHAIN_MIN = 3
TICK = {"NIFTY": 0.05, "SENSEX": 0.01}


LAST = {}          # last replay: boxes + closes (for the status message)
STATUS = {}        # per index: (signature, text) for the combined status message
MTF = {}           # per index: current 1h MTF box line (short)
HBOX = {}          # per index: 1h MTF boxes (firstT, hi, lo) for the 5m+1h tag
STATUS_ON = os.getenv("CB_STATUS", "1") == "1"


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


def _dur(rec, tf_min=TF_MIN):
    """TRIDENT box text: 45m / 1.5h / 2d"""
    bars = rec["rightIdx"] - rec["leftIdx"] + 1
    ss = _sessions(rec["firstT"], rec["rightT"])
    mins = bars * tf_min
    if ss > 1:
        return f"{ss}d"
    if mins < 60:
        return f"{int(round(mins))}m"
    h = mins / 60
    return (f"{h:.1f}".rstrip("0").rstrip(".")) + "h"


# ───────────────────────── replay ─────────────────────────
def replay(m5, tick, tf_min=TF_MIN):
    """Walk the closed candles like TRIDENT. Returns a list of events (oldest first)."""
    t = [_ms(x) for x in m5.index]
    t_idx = {x: k for k, x in enumerate(t)}
    end = [x + tf_min * 60000 for x in t]
    H, L, C = list(m5["High"]), list(m5["Low"]), list(m5["Close"])
    n = len(C)
    dayEnd = [_ms(pd.Timestamp(x).normalize() + pd.Timedelta(hours=15, minutes=30)) for x in m5.index]
    atr = _atr(H, L, C, ATR_LEN)
    ema = _ema(C, EMA_LEN)
    tolT, tolP = 1 * tf_min * 60000, tick

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
    trState, lastHt, lastLt = 0, None, None          # trend state (HH+HL = UP, LL+LH = DOWN) for change alerts
    allsw = []                                      # every swing: (time, price, tag HH/LH/HL/LL)
    events = []
    # HL / LH chains: list of (price, time, tag) incl. the starting swing; one opposite swing allowed (not back to back)
    loPrev = hiPrev = None
    loChain, loCnt, loBad = [], 0, False
    hiChain, hiCnt, hiBad = [], 0, False

    for i in range(n):
        close = C[i]
        # ── (A) every box: close outside -> U / D | later close beyond the other side -> B | else extend
        for rr in reversed(recs):
            if rr["live"] and not rr["dead"]:
                if close > rr["hi"] or close < rr["lo"]:
                    rr["live"] = False
                    rr["brk"] = rr["brk1"] = 1 if close > rr["hi"] else -1
                    rr["brkIdx"] = i
                    rr["rightT"], rr["rightIdx"], rr["chg"] = t[i], i, i
                else:
                    rr["rightIdx"], rr["rightT"], rr["chg"] = i, t[i], i
            elif not rr["dead"] and rr["brk"] in (1, -1):
                if (rr["brk"] == 1 and close < rr["lo"]) or (rr["brk"] == -1 and close > rr["hi"]):
                    rr["brk"], rr["chg"], rr["bIdx"] = 2, i, i
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
                            curRec["brkIdx"] = j
                            hitJ = j
                            break
                    if hitJ >= 0:
                        for j2 in range(hitJ + 1, i + 1):
                            if (curRec["brk"] == 1 and C[j2] < cLo) or (curRec["brk"] == -1 and C[j2] > cHi):
                                curRec["brk"] = 2
                                curRec["bIdx"] = j2
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
                            kp["brkIdx"] = max(kp.get("brkIdx", -1), dp.get("brkIdx", -1))
                            kp["bIdx"] = max(kp.get("bIdx", -1), dp.get("bIdx", -1))
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
                    allsw.append((t[p], sv, "HH" if hT == 1 else "LH"))
                lastH, pivH, lastHt = sv, sv, t[p]
            else:
                if lastL is not None and sv != lastL:
                    lT = 1 if sv > lastL else -1
                    allsw.append((t[p], sv, "HL" if lT == 1 else "LL"))
                lastL, pivL, lastLt = sv, sv, t[p]
        # HL / LH chain (same swings and labels as TRIDENT: compared with the previous swing low / high)
        for isH, sv in sw:
            if not isH:
                if loPrev is not None and sv != loPrev[0]:
                    if sv > loPrev[0]:                      # HL
                        if not loChain:
                            loChain, loCnt = [(loPrev[0], loPrev[1], "")], 0
                        loChain.append((sv, t[p], "HL"))
                        loCnt += 1
                        loBad = False
                        if loCnt >= CHAIN_MIN:
                            events.append(dict(kind="HL", end=end[i], key=f"HL|{t[p]}", n=loCnt,
                                               chain=list(loChain), trend=None, close=close, st=trState,
                                               sw=[x for x in allsw if x[0] >= loChain[0][1]]))
                    elif loChain:                           # LL inside a chain
                        if loBad:                           # 2nd LL back to back -> chain broken
                            loChain, loCnt, loBad = [], 0, False
                        else:
                            loChain.append((sv, t[p], "LL"))
                            loBad = True
                loPrev = (sv, t[p])
            else:
                if hiPrev is not None and sv != hiPrev[0]:
                    if sv < hiPrev[0]:                      # LH
                        if not hiChain:
                            hiChain, hiCnt = [(hiPrev[0], hiPrev[1], "")], 0
                        hiChain.append((sv, t[p], "LH"))
                        hiCnt += 1
                        hiBad = False
                        if hiCnt >= CHAIN_MIN:
                            events.append(dict(kind="LH", end=end[i], key=f"LH|{t[p]}", n=hiCnt,
                                               chain=list(hiChain), trend=None, close=close, st=trState,
                                               sw=[x for x in allsw if x[0] >= hiChain[0][1]]))
                    elif hiChain:                           # HH inside a chain
                        if hiBad:                           # 2nd HH back to back -> chain broken
                            hiChain, hiCnt, hiBad = [], 0, False
                        else:
                            hiChain.append((sv, t[p], "HH"))
                            hiBad = True
                hiPrev = (sv, t[p])
        trDir = 1 if (hT == 1 and lT == 1) else -1 if (hT == -1 and lT == -1) else 0
        eSide = 0 if ema[i] is None else (1 if close > ema[i] else -1 if close < ema[i] else 0)
        trConf = trDir if (trDir != 0 and eSide == trDir) else 0
        trend = dict(dir=trDir, hT=hT, lT=lT, eSide=eSide, ema=ema[i], conf=trConf, lastH=lastH, lastL=lastL)
        if trDir != 0 and trDir != trState:            # trend change: both last high and low flipped
            if trState != 0:
                last2 = sorted([(lastHt, lastH, "HH" if hT == 1 else "LH"), (lastLt, lastL, "HL" if lT == 1 else "LL")])
                events.append(dict(kind="TC", end=end[i], key=f"TC|{t[i]}", frm=trState, to=trDir,
                                   sw=last2, trend=dict(dir=trDir), close=close))
            trState = trDir
        trend["st"] = trState
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
            snap = dict(hi=r["hi"], lo=r["lo"], firstT=r["firstT"], dur=_dur(r, tf_min))
            if r["id"] not in fm_ids:
                fm_ids.add(r["id"])
                events.append(dict(kind="F", end=end[i], key=f"F|{r['firstT']}", box=snap, trend=trend, close=close))
                # price overlap with EARLIER boxes of the same day (separate in time, so TRIDENT keeps them apart)
                day = pd.Timestamp(r["firstT"], unit="ms", tz=IST).date()
                ovl = []
                for o in recs:
                    if o is r or o["dead"] or o["firstT"] >= r["firstT"]:
                        continue
                    if o["rightIdx"] - o["leftIdx"] + 1 < MIN_BARS:
                        continue
                    if pd.Timestamp(o["firstT"], unit="ms", tz=IST).date() != day:
                        continue
                    zlo, zhi = max(o["lo"], r["lo"]), min(o["hi"], r["hi"])
                    if zlo <= zhi:
                        ovl.append(dict(hi=o["hi"], lo=o["lo"], firstT=o["firstT"], rightT=o["rightT"],
                                        brk=o["brk"], brk1=o["brk1"], dur=_dur(o, tf_min), zlo=zlo, zhi=zhi))
                if ovl:
                    events.append(dict(kind="O", end=end[i], key=f"O|{r['firstT']}", box=snap, ovl=ovl,
                                       trend=trend, close=close))
            prev = bk_prev.get(r["id"], 0)
            if r["brk"] != prev:
                bk_prev[r["id"]] = r["brk"]
                if r["brk"] in (1, -1, 2):
                    k = "U" if r["brk"] == 1 else "D" if r["brk"] == -1 else "B"
                    events.append(dict(kind=k, end=end[i], key=f"{k}|{r['firstT']}|{t[i]}", box=snap,
                                       trend=trend, close=close))
    LAST.update(recs=recs, C=C, t=t, end=end)          # final state, used by the status message
    return events


# ───────────────────────── 1h MTF box (same engine on 1h candles) ─────────────────────────
def hourly(m5, now):
    """Closed 1h candles from 5m, NSE/BSE alignment 09:15, 10:15 ... 15:15 (last one ends 15:30)."""
    if m5 is None or len(m5) == 0:
        return m5
    df = m5[[(_ms(x) + TF_MIN * 60000) <= _ms(now) for x in m5.index]]
    starts = []
    for x in df.index:
        base = x.normalize() + pd.Timedelta(hours=9, minutes=15)
        starts.append(base + pd.Timedelta(hours=int((x - base) // pd.Timedelta(hours=1))))
    g = df.groupby(starts)
    h = pd.DataFrame({"Open": g["Open"].first() if "Open" in df else g["Close"].first(),
                      "High": g["High"].max(), "Low": g["Low"].min(), "Close": g["Close"].last()})
    ends = [min(x + pd.Timedelta(hours=1), x.normalize() + pd.Timedelta(hours=15, minutes=30)) for x in h.index]
    return h[[e <= pd.Timestamp(now) for e in ends]]


def _mins(n5):
    """number of 5m candles -> 45m / 1h20m"""
    m = int(n5) * TF_MIN
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def mtf_status(m5, now, tick):
    """Latest 1h MTF con box, TRIDENT-style: duration text (45m / 3h / 2d) from the 1h candles,
    break U / D / B on a 1h close, x move from the latest 5m close. None if no 1h box."""
    h = hourly(m5, now)
    if h is None or len(h) < 4:
        return None
    saved = dict(LAST)
    try:
        replay(h, tick, tf_min=60)
        recs = [r for r in LAST["recs"] if not r["dead"]]
        ht = list(LAST["t"])
    finally:
        LAST.clear()
        LAST.update(saved)
    if not recs:
        return None
    r = max(recs, key=lambda x: (x["rightT"], x["firstT"]))
    closed = m5[[(_ms(x) + TF_MIN * 60000) <= _ms(now) for x in m5.index]]
    c = float(closed["Close"].iloc[-1])
    hi, lo = r["hi"], r["lo"]
    rg = (hi - lo) or 1e-9
    side = 0 if r["live"] else (r["brk"] if r["brk"] in (1, -1) else -r["brk1"])
    x = (c - lo) / rg if side >= 0 else (hi - c) / rg
    bi = r.get("bIdx", -1) if r["brk"] == 2 else r.get("brkIdx", -1)
    brk_t = None
    if 0 <= bi < len(ht):                                   # close time of the 1h candle (last one ends 15:30)
        st = pd.Timestamp(ht[bi], unit="ms", tz=IST)
        brk_t = _ms(min(st + pd.Timedelta(hours=1), st.normalize() + pd.Timedelta(hours=15, minutes=30)))
    return dict(hi=hi, lo=lo, firstT=r["firstT"], live=r["live"], brk=r["brk"], side=side, x=x,
                dur=_dur(r, 60), brk_t=brk_t, close=c)


def mtf_boxes(m5, now, tick):
    """All 1h MTF boxes (not merged away): list of (firstT, hi, lo)."""
    h = hourly(m5, now)
    if h is None or len(h) < 4:
        return []
    saved = dict(LAST)
    try:
        replay(h, tick, tf_min=60)
        return [(r["firstT"], r["hi"], r["lo"]) for r in LAST["recs"] if not r["dead"]]
    finally:
        LAST.clear()
        LAST.update(saved)


def box_tag(b, hboxes):
    """'5m+1h' when the 5m box lies inside a 1h box that started before it, else '5m'."""
    for ft, hi, lo in hboxes:
        if ft <= b["firstT"] and lo <= b["lo"] and b["hi"] <= hi:
            return "5m+1h"
    return "5m"


def _p(v):
    """84,120 for big numbers, 371.20 for option premiums"""
    return f"{v:,.0f}" if abs(v) >= 10000 else f"{v:,.2f}"


def mtf_short(st):
    """3h · in 0.5  |  3h U 1.30x  |  — (no 1h box)"""
    if not st:
        return "—"
    if st["live"]:
        x = st["x"]
        lvl = 1.0 if x >= 1 else 0.75 if x >= 0.75 else 0.5 if x >= 0.5 else 0.25 if x >= 0.25 else 0.0
        if st["lo"] <= st["close"] <= st["hi"]:
            return f"{st['dur']} · in {lvl:g}"
        return f"{st['dur']} · 5m {'above' if st['close'] > st['hi'] else 'below'}"
    lab = "U" if st["brk"] == 1 else "D" if st["brk"] == -1 else "B"
    return f"{st['dur']} {lab} {st['x']:.1f}x"


def mtf_line(st, now):
    """📦 1h box 24500.00–24620.00 · 3h · inside at 0.5   |   📦 1h box ... · 3h U 1.30x · broke 11:15"""
    if not st:
        return "📦 1h box: none"
    d = pd.Timestamp(st["firstT"], unit="ms", tz=IST)
    old = f" ({d:%d %b})" if d.date() != pd.Timestamp(now).date() else ""
    box = f"📦 1h box{old} {st['lo']:.2f}–{st['hi']:.2f}"
    if st["live"]:
        x = st["x"]
        lvl = 1.0 if x >= 1 else 0.75 if x >= 0.75 else 0.5 if x >= 0.5 else 0.25 if x >= 0.25 else 0.0
        where = f"inside at {lvl:g}" if st["lo"] <= st["close"] <= st["hi"] else \
            f"5m {'above' if st['close'] > st['hi'] else 'below'} box (1h not closed out)"
        return f"{box} · {st['dur']} · {where}"
    lab = "U" if st["brk"] == 1 else "D" if st["brk"] == -1 else "B"
    when = ""
    if st["brk_t"]:
        bt = pd.Timestamp(st["brk_t"], unit="ms", tz=IST)
        when = f" · broke {bt:%H:%M}" if bt.date() == pd.Timestamp(now).date() else f" · broke {bt:%d %b %H:%M}"
    return f"{box} · {st['dur']} {lab} {st['x']:.2f}x{when}"

# ───────────────────────── messages ─────────────────────────
def _fmt_t(ms):
    return pd.Timestamp(ms, unit="ms", tz=IST).strftime("%d %b %H:%M")


def _trend_line(tr):
    """Trend: UP ▲ (HH + HL) · above EMA6 ✓"""
    d = "UP ▲" if tr["dir"] == 1 else "DOWN ▼" if tr["dir"] == -1 else "SIDEWAYS ↔"
    s = ("HH" if tr["hT"] == 1 else "LH" if tr["hT"] == -1 else "-") + " + " + \
        ("HL" if tr["lT"] == 1 else "LL" if tr["lT"] == -1 else "-")
    e = "above" if tr["eSide"] == 1 else "below" if tr["eSide"] == -1 else "on"
    ok = " ✓" if tr["conf"] != 0 else ""
    return f"Trend: {d} ({s}) · {e} EMA{EMA_LEN}{ok}"


def _tr_word(d):
    return "UP" if d == 1 else "DOWN" if d == -1 else "SIDE"


def _chain_message(name, ev):
    """📈 SENSEX 5m · UP · HL ×4 · 11:10  +  table of every swing in the chain"""
    up = ev["kind"] == "HL"
    tr = ev.get("st", 0)
    rows = ev.get("sw") or [(x, v, tag or ("L" if up else "H")) for v, x, tag in ev["chain"]]
    rows = rows[-16:]
    when = pd.Timestamp(ev["end"] - TF_MIN * 60000, unit="ms", tz=IST).strftime("%H:%M")
    head = f"{'📈' if up else '📉'} <b>{name} 5m · {_tr_word(tr)} · {'HL' if up else 'LH'} ×{ev['n']} · {when}</b>"
    last_day = pd.Timestamp(rows[-1][0], unit="ms", tz=IST).date()

    def tm(ms):
        x = pd.Timestamp(ms, unit="ms", tz=IST)
        return x.strftime("%H:%M") if x.date() == last_day else x.strftime("%d%b %H:%M")
    body = [f"{'Swing':<7}{'Time':<12}Price"] + [f"{tag:<7}{tm(x):<12}{_p(v)}" for x, v, tag in rows]
    return head + "\n<pre>" + "\n".join(body) + "</pre>"


def _tc_message(name, ev):
    """🔄 SENSEX 5m · TREND CHANGE · 11:30 / DOWN → UP / last high + low"""
    when = pd.Timestamp(ev["end"] - TF_MIN * 60000, unit="ms", tz=IST).strftime("%H:%M")
    body = [f"{'Swing':<7}{'Time':<7}Price"] + \
           [f"{tag:<7}{pd.Timestamp(x, unit='ms', tz=IST):%H:%M}  {_p(v)}" for x, v, tag in ev["sw"]]
    return (f"🔄 <b>{name} 5m · TREND CHANGE · {when}</b>\n{_tr_word(ev['frm'])} → {_tr_word(ev['to'])}\n<pre>"
            + "\n".join(body) + "</pre>")


def _targets(lo, hi, side):
    rg = hi - lo
    return [(m, lo + m * rg if side == 1 else hi - m * rg) for m in (1.5, 2.0, 2.5)]


def message(name, ev, tag="5m"):
    """Index 5m alerts (aligned tables)."""
    if ev["kind"] in ("HL", "LH"):
        return _chain_message(name, ev)
    if ev["kind"] == "TC":
        return _tc_message(name, ev)
    if ev["kind"] == "O":
        return _overlap_message(name, ev)
    tr = ev["trend"]
    when = pd.Timestamp(ev["end"] - TF_MIN * 60000, unit="ms", tz=IST).strftime("%H:%M")
    if ev["kind"] == "T":
        up = tr["conf"] == 1
        return f"{'📈' if up else '📉'} <b>{name} 5m · TREND {'UP' if up else 'DOWN'} · {when}</b>"
    b = ev["box"]
    st = {"F": "NEW", "U": "U", "D": "D", "B": "B"}[ev["kind"]]
    head = f"🔔 <b>{name} 5m · {'NEW BOX' if ev['kind'] == 'F' else 'BREAK'} · {when}</b>"
    rows = [f"{'Box':<8}{'Time':<6}{'Status':<8}Trend", f"{tag:<8}{b['dur']:<6}{st:<8}{_tr_word(tr.get('st', tr['dir']))}"]
    if ev["kind"] != "F":
        side = 1 if ev["kind"] == "U" else -1 if ev["kind"] == "D" else (1 if ev["close"] > b["hi"] else -1)
        rows += [f"{f'{m:g}x':<8}{_p(lv)}" for m, lv in _targets(b["lo"], b["hi"], side)]
    return head + "\n<pre>" + "\n".join(rows) + "</pre>"


# ───────────────────────── live con box status (one combined message, sent only on change) ─────────────────────────
def _side_txt(b):
    return "UP ⬆" if b == 1 else "DOWN ⬇" if b == -1 else "BOTH SIDES"


def _hm(ms, with_date=False):
    x = pd.Timestamp(ms, unit="ms", tz=IST)
    return x.strftime("%d %b %H:%M" if with_date else "%H:%M")


def _status_block(name, now):
    """(signature, text):  📦 SENSEX · 11:05 · 84,120.50  +  Box / Time / Status"""
    recs, C, t = LAST.get("recs") or [], LAST.get("C") or [], LAST.get("t") or []
    head = f"📦 <b>{name}</b> · {pd.Timestamp(now):%H:%M}" + (f" · {C[-1]:,.2f}" if C else "")
    boxes = [r for r in recs if not r["dead"] and r["rightIdx"] - r["leftIdx"] + 1 >= MIN_BARS]
    hdr = f"{'Box':<8}{'Time':<6}Status"
    if not boxes or not C:
        return (f"{name}|none", head + "\n<pre>" + hdr + "\n—</pre>")
    r = max(boxes, key=lambda x: (x["rightT"], x["firstT"]))          # latest box, like TRIDENT
    tag = box_tag(r, HBOX.get(name, []))
    d = pd.Timestamp(r["firstT"], unit="ms", tz=IST).date()
    if d != pd.Timestamp(now).date():
        head += f" · box from {d:%d %b}"
    st = "In" if r["live"] else {1: "U", -1: "D", 2: "B"}.get(r["brk"], "?")
    return (f"{name}|{r['firstT']}|{st}|{tag}", head + "\n<pre>" + hdr + "\n" + f"{tag:<8}{_dur(r):<6}{st}" + "</pre>")


def order_names(now):
    """Index with the NEAREST option expiry first (expiry day -> that index on top); same expiry -> NIFTY first.
    Uses the options contract list (state/opt_contracts.json); falls back to Wed-Thu SENSEX first."""
    import json
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    try:
        f = os.path.join(os.path.dirname(os.getenv("STATE_FILE", "state/sent_stars.json")) or ".", "opt_contracts.json")
        with open(f) as fh:
            c = json.load(fh)
        exp = {k: v["expiry"] for k, v in c["data"].items()}
        if c.get("date") == today and "NIFTY" in exp and "SENSEX" in exp:
            return sorted(["NIFTY", "SENSEX"], key=lambda k: (exp[k], k != "NIFTY"))
    except Exception:
        pass
    wd = pd.Timestamp(now).weekday()          # Mon 0 ... Sun 6
    return ["SENSEX", "NIFTY"] if wd in (2, 3) else ["NIFTY", "SENSEX"]


def send_status(state, now, send, edit=None):
    """Index con box board: ONE message per day, edited silently every 5 min
    (break / new box alerts with sound come from run())."""
    if not STATUS_ON or not STATUS:
        return 0
    last = state.setdefault("_cbstat", {})
    txt = "\n\n".join(STATUS[k][1] for k in order_names(now) if k in STATUS)
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    mid = last.get("mid")
    if mid and last.get("day") == today:
        if last.get("text") == txt or (edit and edit(mid, txt)):
            last["text"] = txt
            return 0
    mid = send(txt)
    if mid:
        last.update(mid=mid if isinstance(mid, int) and mid > 0 else None, day=today, text=txt)
        return 1
    return 0


def _overlap_message(name, ev):
    b = ev["box"]
    lines = [f"🔀 <b>{name} 5m · CON BOX OVERLAP (same day)</b>"]
    for o in ev["ovl"]:
        st = "still active" if o["brk"] == 0 else f"broke {'UP' if o['brk1'] == 1 else 'DOWN'}" + (" (then B)" if o["brk"] == 2 else "")
        lines.append(f"Earlier box: {o['lo']:.2f} – {o['hi']:.2f} · {_hm(o['firstT'])}–{_hm(o['rightT'])} · {st}")
    lines.append(f"New box:     {b['lo']:.2f} – {b['hi']:.2f} · from {_hm(b['firstT'])}")
    for o in ev["ovl"]:
        lines.append(f"Overlap zone: {o['zlo']:.2f} – {o['zhi']:.2f} ({o['zhi'] - o['zlo']:.2f} pts)")
    return "\n".join(lines)


# ───────────────────────── run (called from trident_star_alert.main) ─────────────────────────
def run(name, m5, state, now, send):
    if not (CB_ON or TREND_ON or CHAIN_ON or STATUS_ON or TCHG_ON):
        return 0
    closed = m5[[(_ms(x) + TF_MIN * 60000) <= _ms(now) for x in m5.index]]
    if len(closed) < 10:
        return 0
    if MTF_ON:
        try:
            MTF[name] = "1h box: " + mtf_short(mtf_status(closed, now, TICK.get(name, 0.05)))
            HBOX[name] = mtf_boxes(closed, now, TICK.get(name, 0.05))
        except Exception as e:
            print(f"{name} 1h MTF box error {e}")
            MTF.pop(name, None)
            HBOX.pop(name, None)
    events = replay(closed, TICK.get(name, 0.05))
    if STATUS_ON:
        STATUS[name] = _status_block(name, now)
    cutoff = _ms(now) - WINDOW_MIN * 60000
    sent = 0
    for ev in events:
        if ev["kind"] == "T" and not TREND_ON:
            continue
        if ev["kind"] in ("HL", "LH") and not CHAIN_ON:
            continue
        if ev["kind"] in ("F", "U", "D", "B", "O") and not CB_ON:
            continue
        if ev["kind"] == "O" and not OVL_ON:
            continue
        if ev["kind"] == "TC" and not TCHG_ON:
            continue
        skey = f"{name}|cb5|{ev['key']}"
        if skey in state or ev["end"] < cutoff:
            continue
        tag = box_tag(ev["box"], HBOX.get(name, [])) if "box" in ev else "5m"
        mid = send(message(name, ev, tag))
        if mid:
            state[skey] = now.isoformat()
            sent += 1
    print(f"{name} 5m con box / HL-LH: {sum(e['kind'] == 'F' for e in events)} boxes, "
          f"{sum(e['kind'] in ('HL', 'LH') for e in events)} HL/LH chain alerts in history, sent {sent}"
          + (f" | {MTF[name]}" if MTF.get(name) else ""))
    return sent
