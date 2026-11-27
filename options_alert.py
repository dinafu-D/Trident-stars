"""
Options con box table (TRIDENT 5m con box on each option chart) -> Telegram, NIFTY + SENSEX, Dhan data only.

CON BOX : ATM +-2, 5 strikes (ATM recalculated every 5 min from the index spot), buy side only
          (index DOWN -> PE, UP -> CE; index inside / no box -> CE + PE). Nearest weekly expiry
          (the expiring contract on expiry day). One BOARD message per index, edited silently every 5 min:
          latest box today (or the last old box), time in box, break side / time, x move, 1.5x / 2x / 2.5x,
          back inside, and the option's own 1h MTF box (TRIDENT label: duration, U/D/B, x, break time).
          A NEW message (with sound) when a box forms, breaks, goes back inside or breaks again.
Order   : index with the nearest option expiry first (expiry day -> on top); same expiry -> NIFTY first.

OPTIONS EOB (ATM +-2, option buying): index 5m con box DOWN -> PE bull EOBs, UP -> CE bull EOBs
          (index inside / no box -> nothing). Table: Strike · option colour + price · swing time ·
          CE/PE ✅ if the other option made the opposite (bear) EOB on the same swing candle ·
          IN ✅ if the index made the same-direction EOB (DOWN: bear) on the same swing candle.
          One message per check; edited every 5 min (price, dark colour, ticks, ❌ INVALID).
"""

import json
import os
import time

import pandas as pd

import conbox_alert as CB
import eob_alert as EOB

IST = "Asia/Kolkata"
OPT_ON = os.getenv("OPTION_ALERTS", "1") == "1"
WINDOW_MIN = int(os.getenv("OPT_WINDOW_MIN", "20"))
SIDE_N = int(os.getenv("OPT_STRIKES_EACH_SIDE", "2"))  # con box board: ATM +-2 = 5 strikes
CFG = {"NIFTY": dict(exch="NSE", seg="NSE_FNO", prefix="NIFTY-", step=50),
       "SENSEX": dict(exch="BSE", seg="BSE_FNO", prefix="SENSEX-", step=100)}
EOBC_ON = os.getenv("OPT_EOB_CONFIRM", "1") == "1"
H1_ALERTS = os.getenv("H1_BOX_ALERTS", "1") == "1"   # option's own 1h box: 5m break / 1h confirmed / RE-IN
EOBC_SIDE = int(os.getenv("OPT_EOB_STRIKES_EACH_SIDE", "2"))   # ATM +-2 = 5 strikes
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"


def order_names(now):
    """Nearest option expiry first (see conbox_alert.order_names)."""
    return CB.order_names(now)


# ───────────────────────── contracts (Dhan instrument list, cached once a day) ─────────────────────────
def _cache_file():
    return os.path.join(os.path.dirname(os.getenv("STATE_FILE", "state/sent_stars.json")) or ".", "opt_contracts.json")


def load_contracts(now):
    """{name: {"expiry": "YYYY-MM-DD", "map": {"24600CE": security_id, ...}}} for the nearest expiry."""
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    try:
        with open(_cache_file()) as f:
            c = json.load(f)
        if c.get("date") == today:
            return c["data"]
    except Exception:
        pass
    cols = ("SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID",
            "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE")
    df = pd.read_csv(SCRIP_MASTER, usecols=lambda x: x in cols, low_memory=False)
    df = df[df["SEM_INSTRUMENT_NAME"].astype(str) == "OPTIDX"]
    out = {}
    for name, cfg in CFG.items():
        d = df[(df["SEM_EXM_EXCH_ID"].astype(str) == cfg["exch"]) &
               (df["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.startswith(cfg["prefix"]))].copy()
        d["exp"] = pd.to_datetime(d["SEM_EXPIRY_DATE"], errors="coerce").dt.strftime("%Y-%m-%d")
        d = d[d["exp"] >= today]
        if d.empty:
            continue
        exp = d["exp"].min()                                   # nearest expiry (today on expiry day)
        d = d[d["exp"] == exp]
        m = {}
        for _, r in d.iterrows():
            sym = str(r["SEM_TRADING_SYMBOL"]).upper()
            typ = str(r.get("SEM_OPTION_TYPE", "")).upper()
            typ = typ if typ in ("CE", "PE") else ("CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else "")
            if not typ:
                continue
            m[f"{int(round(float(r['SEM_STRIKE_PRICE'])))}{typ}"] = str(int(r["SEM_SMST_SECURITY_ID"]))
        out[name] = {"expiry": exp, "map": m}
    os.makedirs(os.path.dirname(_cache_file()) or ".", exist_ok=True)
    with open(_cache_file(), "w") as f:
        json.dump({"date": today, "data": out}, f)
    print("options: contract list refreshed " + ", ".join(f"{k} exp {v['expiry']} ({len(v['map'])})" for k, v in out.items()))
    return out


# ───────────────────────── one option chart: track its latest con box ─────────────────────────
def _today_closed(df, now):
    today = pd.Timestamp(now).date()
    df = df[[x.date() == today for x in df.index]]
    return df[[(x + pd.Timedelta(minutes=5)) <= now for x in df.index]]


def _track(df, now):
    """Tracking events of one option chart (today):
    a box is tracked from when it forms (label, Min Bars) until the next box forms;
    BRK = a 5m close outside the box (up / down), IN = a close back inside after a break."""
    df = _today_closed(df, now)
    if len(df) < 3:
        return None, []
    events = CB.replay(df, 0.05)
    recs, C, t = CB.LAST["recs"], CB.LAST["C"], CB.LAST["t"]
    formed, out = {}, []
    for e in events:
        if e["kind"] == "F" and e["box"]["firstT"] not in formed:
            formed[e["box"]["firstT"]] = e["end"]
            out.append(dict(kind="NEW", side=0, end=e["end"], hi=e["box"]["hi"], lo=e["box"]["lo"],
                            firstT=e["box"]["firstT"], close=e["close"], after=[]))
    boxes = sorted([(formed[r["firstT"]], r) for r in recs if r["firstT"] in formed], key=lambda x: x[0])
    for n, (f_end, r) in enumerate(boxes):
        until = boxes[n + 1][0] if n + 1 < len(boxes) else float("inf")      # tracking moves to the next box
        bi = r.get("brkIdx", -1)
        if bi < 0:
            continue
        state = 0
        for j in range(bi, len(C)):
            e_end = t[j] + 5 * 60000
            if e_end > until:
                break
            sd = 1 if C[j] > r["hi"] else -1 if C[j] < r["lo"] else 0
            if sd != state:
                out.append(dict(kind="BRK" if sd else "IN", side=sd, end=e_end, j=j, hi=r["hi"], lo=r["lo"],
                                firstT=r["firstT"], close=C[j], after=C[j:]))
                state = sd
    return C[-1], out


def _index_dir(m5, now):
    """(direction 1 up / -1 down / 0 inside or no box, text line) from the index's latest 5m con box."""
    df = m5[[(x + pd.Timedelta(minutes=5)) <= now for x in m5.index]]
    CB.replay(df, 0.05)
    recs, C, t = CB.LAST["recs"], CB.LAST["C"], CB.LAST["t"]
    boxes = [r for r in recs if not r["dead"] and r["rightIdx"] - r["leftIdx"] + 1 >= CB.MIN_BARS]
    spot = C[-1]
    if not boxes:
        return 0, f"Index: no con box · spot {spot:.2f}"
    r = max(boxes, key=lambda x: (x["rightT"], x["firstT"]))
    rg = (r["hi"] - r["lo"]) or 1e-9
    if r["live"]:
        x = (spot - r["lo"]) / rg
        lvl = 1.0 if x >= 1 else 0.75 if x >= 0.75 else 0.5 if x >= 0.5 else 0.25 if x >= 0.25 else 0.0
        return 0, f"Index: inside con box at {lvl:g} · spot {spot:.2f}"
    side = r["brk"] if r["brk"] in (1, -1) else -r["brk1"]
    xm = (spot - r["lo"]) / rg if side == 1 else (r["hi"] - spot) / rg
    bi = r.get("bIdx", -1) if r["brk"] == 2 else r.get("brkIdx", -1)
    bt = pd.Timestamp(t[bi] + 5 * 60000, unit="ms", tz=IST).strftime("%H:%M") if 0 <= bi < len(t) else ""
    word = "UP ⬆" if side == 1 else "DOWN ⬇"
    extra = " (both sides, last " + ("UP)" if side == 1 else "DOWN)") if r["brk"] == 2 else ""
    return side, f"Index: con box broken {word}{extra} at {bt} · spot {spot:.2f} · {xm:.2f}x"


# ───────────────────────── con box BOARD (one edited message) + alerts on NEW box / U / D ─────────────────────────
def _hm2(ms, now):
    x = pd.Timestamp(ms, unit="ms", tz=IST)
    return x.strftime("%H:%M") if x.date() == pd.Timestamp(now).date() else x.strftime("%d %b %H:%M")


def strike_state(df, now):
    """Latest 5m con box of one option (today, else the last old box):
    dict(tag '5m' / '5m+1h', dur, st 'In' / 'U' / 'D', firstT, today) or None if no box."""
    dfc = df[[(x + pd.Timedelta(minutes=5)) <= now for x in df.index]]
    if len(dfc) < 3:
        return None
    CB.replay(dfc, 0.05)
    recs, C = CB.LAST["recs"], CB.LAST["C"]
    live = [r for r in recs if not r["dead"]]
    if not live:
        return None
    today = pd.Timestamp(now).date()
    tb = [r for r in live if pd.Timestamp(r["firstT"], unit="ms", tz=IST).date() == today]
    r = max(tb, key=lambda x: x["firstT"]) if tb else max(live, key=lambda x: (x["rightT"], x["firstT"]))
    box = dict(firstT=r["firstT"], hi=r["hi"], lo=r["lo"])
    try:
        tag = CB.box_tag(box, CB.mtf_boxes(dfc, now, 0.05))
    except Exception as e:
        print(f"options 1h box: {e}")
        tag = "5m"
    c = C[-1]
    st = "In" if r["live"] or r["lo"] <= c <= r["hi"] else ("U" if c > r["hi"] else "D")
    try:
        h = CB.mtf_status(dfc, now, 0.05)                   # the option's own 1h box (for 1h box alerts)
    except Exception as e:
        print(f"options 1h status: {e}")
        h = None
    return dict(tag=tag, dur=CB._dur(r), st=st, firstT=r["firstT"], today=bool(tb), h=h)


def _brow(label, s):
    """Strike  Box     Time  Status"""
    if not s:
        return f"{label:<9}—"
    return f"{label:<9}{s['tag']:<8}{s['dur']:<6}{s['st']}"


def _upd(now):
    """start time of the last closed 5m candle"""
    t = pd.Timestamp(now).floor("5min") - pd.Timedelta(minutes=5)
    return t.strftime("%H:%M")


def _al_text(head, rows, upd=None):
    """🔔 alert table: Strike / Box / Time / Status (NEW, U, D, RE-IN)"""
    hdr = f"{'Strike':<9}{'Box':<8}{'Time':<6}Status"
    return head + (f" · upd {upd}" if upd else "") + "\n<pre>" + "\n".join([hdr] + [f"{r['label']:<9}{r['tag']:<8}{r['dur']:<6}{r['lab']}"
                                                  for r in rows]) + "</pre>"


def _post_board(state, name, txt, now, send, edit, delete=None, moved=False):
    """One board per day, edited silently. moved=True (something was sent this run): delete the old board
    and send a fresh one silently, so the board stays at the bottom of the chat."""
    bd = state.setdefault("_optboard", {})
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    cur = bd.get(name)
    if cur and cur.get("day") == today and cur.get("mid"):
        if moved and delete:
            delete(cur["mid"])
        elif cur.get("text") == txt:
            return 0
        elif edit and edit(cur["mid"], txt):
            cur["text"] = txt
            return 0
    try:
        mid = send(txt, silent=True)
    except TypeError:
        mid = send(txt)
    if mid:
        bd[name] = dict(day=today, mid=mid if isinstance(mid, int) and mid > 0 else None, text=txt)
        return 1
    return 0


# ───────────────────────── EOB CONFIRMED: CE + PE opposite EOBs on the same 5m candle ─────────────────────────
def _eobs(df, now):
    df = _today_closed(df, now)
    if len(df) < 3 or "Open" not in df:
        return []
    eobs, _ = EOB.replay(df, 0.05, tf_ms=5 * 60000)
    return eobs


def _col(r):
    return EOB.COLOURS[("bear" if r["bear"] else "bull", r["hp"], r["swp"])]


def _hh(ms):
    return pd.Timestamp(ms, unit="ms", tz=IST).strftime("%H:%M")


def _idx_eobs(m5, now):
    """5m EOBs of the index (last ~3 days of closed candles)."""
    df = m5[[(x + pd.Timedelta(minutes=5)) <= now for x in m5.index]].tail(240)
    if len(df) < 3 or "Open" not in df:
        return []
    eobs, _ = EOB.replay(df, 0.05, tf_ms=5 * 60000)
    return eobs


def _row(row, sides, idx_eobs):
    """Look up the buy-side option's bull EOB + the two same-time checks for a sent/new row."""
    buy, opp = ("PE", "CE") if row["dir"] == -1 else ("CE", "PE")
    r = next((e for e in sides.get(buy, []) if not e["bear"] and e["swT"] == row["swT"]), None)
    if r is None:
        return None
    t_opp = any(e["bear"] and e["swT"] == row["swT"] for e in sides.get(opp, []))
    t_idx = any(e["bear"] == (row["dir"] == -1) and e["swT"] == row["swT"] for e in idx_eobs)
    return dict(r=r, px=sides.get(buy + "px"), opp=t_opp, idx=t_idx)


def _eob_rows(rs):
    """aligned rows: Strike  PE         Time   CE  IN"""
    out = []
    for row in sorted(rs, key=lambda x: (x["swT"], x["k"])):
        v = row["v"]
        k = f"{row['k']:<8}"
        if v["r"]["invalid"]:
            out.append(f"{k}❌ INVALID {_hh(v['r']['invAt'])}")
            continue
        emo = _col(v["r"])[0]
        px = f"{v['px']:.2f}" if v["px"] is not None else "–"
        out.append(f"{k}{emo} {px:<8} {_hh(row['swT']):<7}{'✅' if v['opp'] else '❌'}  {'✅' if v['idx'] else '❌'}")
    return out


def _eobc_message(rows, now):
    """🔱 SENSEX · EOB · ⬇ PE  +  table"""
    out = []
    groups = {}
    for row in rows:
        groups.setdefault((row["name"], row["dir"]), []).append(row)
    for (name, d), rs in groups.items():
        buy, opp = ("PE", "CE") if d == -1 else ("CE", "PE")
        head = f"🔱 <b>{name} · EOB · {'⬇ PE' if d == -1 else '⬆ CE'}</b> · upd {_upd(now)}"
        lines = [f"{'Strike':<8}{buy:<12}{'Time':<7}{opp:<4}IN"] + _eob_rows(rs)
        out.append(head + "\n<pre>" + "\n".join(lines) + "</pre>")
    return "\n\n".join(out)


def run_eob_confirm(found, idxinfo, state, now, send, edit, names):
    """found: {name: {strike: {"new": bool, "CE": eobs, "PE": eobs, "CEpx": .., "PEpx": ..}}}
    idxinfo: {name: (index direction 1/-1/0, index 5m eobs)}"""
    st = state.setdefault("_eobc", {})
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    for mid in [m for m, v in st.items() if v.get("day") != today or "rows" not in v]:
        st.pop(mid)                                   # yesterday's (or old-format) messages are not updated
    cutoff = CB._ms(now) - WINDOW_MIN * 60000
    sent = {f"{r['name']}|{r['k']}|{r['dir']}|{r['swT']}" for v in st.values() for r in v["rows"]}
    new = []
    for name in names:
        d, idx_eobs = idxinfo.get(name, (0, []))
        if d == 0:
            continue                                  # index inside its box / no box -> nothing new
        buy = "PE" if d == -1 else "CE"
        for k in sorted(found.get(name, {})):
            sides = found[name][k]
            if not sides.get("new"):
                continue
            for e in sides.get(buy, []):
                if e["bear"] or e["invalid"] or e["madeEnd"] < cutoff:
                    continue
                row = dict(name=name, k=k, dir=d, swT=e["swT"])
                if f"{name}|{k}|{d}|{e['swT']}" in sent:
                    continue
                row["v"] = _row(row, sides, idx_eobs)
                new.append(row)
    n, just = 0, None
    if new:
        txt = _eobc_message(new, now)
        mid = send(txt)
        if mid:
            just = str(mid)
            st[just] = dict(day=today, text=txt,
                            rows=[{k: r[k] for k in ("name", "k", "dir", "swT")} for r in new])
            n = 1
    # update earlier messages: price, colour (dark), ticks, invalid
    for mid, v in st.items():
        if mid == just:
            continue
        rows = []
        for r in v["rows"]:
            sides = found.get(r["name"], {}).get(r["k"])
            vv = _row(r, sides, idxinfo.get(r["name"], (0, []))[1]) if sides else None
            if vv is None:
                break
            rows.append(dict(r, v=vv))
        else:
            txt = _eobc_message(rows, now)
            if txt != v["text"] and edit and edit(int(mid), txt):
                v["text"] = txt
    return n


def _ask_reply(name, k, types, contracts, fetch_option, now, cache):
    """Reply to a typed strike: con box + EOB for CE and / or PE of that strike."""
    rows, erows = [], []
    for typ in types:
        sid = contracts[name]["map"].get(f"{k}{typ}")
        if not sid:
            rows.append(f"{typ:<9}no contract")
            continue
        df = cache.get((k, typ))
        if df is None:
            df = fetch_option(CFG[name]["seg"], sid, now)
            time.sleep(0.22)
        rows.append(_brow(typ, strike_state(df, now)))
        eobs = [e for e in _eobs(df, now)]
        dc = _today_closed(df, now)
        px = f"{float(dc['Close'].iloc[-1]):.2f}" if len(dc) else "–"
        if eobs:
            e = max(eobs, key=lambda x: x["swT"])
            emo = _col(e)[0]
            cell = "❌ invalid" if e["invalid"] else f"{emo} {'bear' if e['bear'] else 'bull'}"
            erows.append(f"{typ:<6}{cell:<11}{_hh(e['swT']):<7}{px}")      # emoji = 2 cells
        else:
            erows.append(f"{typ:<6}{'—':<12}{'':<7}{px}")
    head = f"🔎 <b>{name} {k} · {pd.Timestamp(now):%H:%M}</b>"
    body = [f"{'Type':<9}{'Box':<8}{'Time':<6}Status"] + rows + ["", f"{'EOB':<6}{'Colour':<12}{'Time':<7}Price"] + erows
    return head + "\n<pre>" + "\n".join(body) + "</pre>"


def run(m5_by_name, state, now, send, fetch_option, edit=None, asks=None, delete=None):
    """Called once per check after the index loop. fetch_option(seg, security_id, now) -> 5m DataFrame (few days).
    asks: texts typed to the options bot since the last run ('eob', '84000', '84000PE')."""
    if not OPT_ON:
        return 0
    contracts = load_contracts(now)
    found = {}                                             # option EOB table: {name: {strike: {"CE": [...], ...}}}
    want = {}                                              # strikes of today's sent EOB rows (kept updated)
    for v in state.get("_eobc", {}).values():
        for r in v.get("rows", []):
            want.setdefault(r["name"], set()).add(int(r["k"]))
    idxinfo, log, alerts, sent = {}, [], [], 0
    last_st = state.setdefault("_optst", {})
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    names = [n for n in order_names(now) if n in contracts and n in m5_by_name]
    cache, cur, boards, changed = {}, {}, {}, set()
    h1st = state.setdefault("_opt1h", {})
    for name in names:
        step = CFG[name]["step"]
        m5 = m5_by_name[name]
        closed = m5[[(x + pd.Timedelta(minutes=5)) <= now for x in m5.index]]
        if closed.empty:
            continue
        spot = float(closed["Close"].iloc[-1])
        atm = int(round(spot / step)) * step                 # recalculated every check
        idx_dir, _ = _index_dir(m5, now)
        if EOBC_ON:
            try:
                idxinfo[name] = (idx_dir, _idx_eobs(m5, now))
            except Exception as e:
                print(f"options {name} index EOB: {e}")
        strikes = [atm + j * step for j in range(-SIDE_N, SIDE_N + 1)]
        eob_new = {atm + j * step for j in range(-EOBC_SIDE, EOBC_SIDE + 1)}
        buy = ("PE",) if idx_dir == -1 else ("CE",) if idx_dir == 1 else ("CE", "PE")
        board, al, h1rows = [], [], []
        for k in sorted(set(strikes) | eob_new | want.get(name, set())):
            for typ in ("CE", "PE"):
                sid = contracts[name]["map"].get(f"{k}{typ}")
                if not sid:
                    continue
                try:
                    df = fetch_option(CFG[name]["seg"], sid, now)
                    cache[(k, typ)] = df
                    time.sleep(0.22)                       # stay under Dhan's data rate limit
                    if EOBC_ON:
                        slot = found.setdefault(name, {}).setdefault(k, {"new": k in eob_new})
                        slot[typ] = _eobs(df, now)
                        dc = _today_closed(df, now)
                        slot[typ + "px"] = float(dc["Close"].iloc[-1]) if len(dc) else None
                    if k not in strikes or typ not in buy:
                        continue
                    st = strike_state(df, now)
                except Exception as e:
                    print(f"options {name} {k}{typ}: {e}")
                    continue
                label = f"{k}{typ[0]}" if len(buy) == 2 else str(k)
                board.append(_brow(label, st))
                # alert: a NEW box today, or the status changed to U / D (silent while inside)
                key = f"{name}|{k}{typ}"
                prev = last_st.get(key)
                if st:
                    cur[key] = st
                if st and prev and prev.get("day") == today:
                    row = dict(key=key, label=f"{k}{typ}", firstT=st["firstT"], tag=st["tag"], dur=st["dur"])
                    if st["today"] and st["firstT"] != prev.get("firstT"):
                        al.append(dict(row, lab="NEW", active=st["st"] == "In"))          # new box
                    elif st["firstT"] == prev.get("firstT") and st["st"] in ("U", "D") and st["st"] != prev.get("st"):
                        al.append(dict(row, lab=st["st"], active=False))                # broke up / down
                    elif st["firstT"] == prev.get("firstT") and st["st"] == "In" and prev.get("st") in ("U", "D"):
                        al.append(dict(row, lab="RE-IN", active=True))                  # broken box re-entered
                if st:
                    last_st[key] = dict(day=today, firstT=st["firstT"], st=st["st"])
                # the option's own 1h box: 5m close broke it / 1h close confirmed / back inside
                if st and st.get("h") and H1_ALERTS:
                    evs, hcur = CB.h1_transitions(h1st.get(key), st["h"])
                    for e in evs:
                        lab = f"{e[2]} 5m" if e.startswith("5m") else f"{e[2]} 1h ✅" if e.startswith("1h") else "RE-IN"
                        h1rows.append(dict(key=key, label=f"{k}{typ}", dur=st["h"]["dur"], lab=lab,
                                           reply=hcur.get("mid") if e.startswith("1h") else None, ev=e))
                    h1st[key] = hcur
        arrow = "⬇ PE" if idx_dir == -1 else "⬆ CE" if idx_dir == 1 else "CE + PE"
        hdr = f"{'Strike':<9}{'Box':<8}{'Time':<6}Status"
        boards[name] = f"📊 <b>{name} OPTIONS · {_upd(now)} · {arrow}</b>\n<pre>" + \
            "\n".join([hdr] + (board or ["—"])) + "</pre>"
        if al:
            alerts.append((f"🔔 <b>{name} OPTIONS · {_upd(now)}</b>", al))
            changed.add(name)
        if h1rows:
            changed.add(name)
            when = _upd(now)
            body = [f"{'Strike':<9}{'Box':<5}{'Time':<6}Status"] + \
                   [f"{r['label']:<9}{'1h':<5}{r['dur']:<6}{r['lab']}" for r in h1rows]
            rto = next((r["reply"] for r in h1rows if r["reply"]), None)
            mid = send(f"🔔 <b>{name} OPTIONS · 1h BOX · {when}</b>\n<pre>" + "\n".join(body) + "</pre>", reply_to=rto)
            if mid:
                sent += 1
                for r in h1rows:                                     # remember the 5m-break message for replies
                    if r["ev"].startswith("5m") and isinstance(mid, int) and mid > 0:
                        h1st[r["key"]]["mid"] = mid
        log.append(f"{name} ATM {atm} ({len(al)} alerts)")
    print("options: " + ", ".join(log))
    alst = state.setdefault("_optal", {})
    for mid in [m for m, v in alst.items() if v.get("day") != today]:
        alst.pop(mid)
    just = set()
    for head, rows in alerts:
        mid = send(_al_text(head, rows))
        if mid:
            sent += 1
            if isinstance(mid, int) and mid > 0 and any(r["active"] for r in rows):
                alst[str(mid)] = dict(day=today, head=head, rows=rows)
                just.add(str(mid))
    # live edit: rows still inside their box get the new Time / Box every 5 min; frozen once they break
    for mid, v in alst.items():
        if mid in just:
            continue
        old = _al_text(v["head"], v["rows"])
        for r in v["rows"]:
            if not r["active"] or r["key"] not in cur:
                continue
            c = cur[r["key"]]
            if c["firstT"] == r["firstT"] and c["st"] == "In":
                r.update(tag=c["tag"], dur=c["dur"])
            else:
                r["active"] = False
        txt = _al_text(v["head"], v["rows"], upd=_upd(now))
        if any(r["active"] for r in v["rows"]) and edit:
            edit(int(mid), txt)
        elif txt != old and edit:
            edit(int(mid), _al_text(v["head"], v["rows"], upd=_upd(now)))
    for mid in [m for m, v in alst.items() if not any(r["active"] for r in v["rows"])]:
        alst.pop(mid)
    if EOBC_ON:
        try:
            n_eob = run_eob_confirm(found, idxinfo, state, now, send, edit, names)
            sent += n_eob
            if n_eob:
                changed.update(names)
        except Exception as e:
            print("options EOB confirm error:", e)
    # boards last, so a board moved to the bottom sits below this run's alerts
    for name, txt in boards.items():
        try:
            sent += _post_board(state, name, txt, now, send, edit, delete, moved=name in changed)
        except Exception as e:
            print(f"options {name} board: {e}")
    # replies to typed requests
    import re
    for q in asks or []:
        q = q.strip().upper().replace(" ", "")
        if not names:
            break
        name = names[0]
        try:
            if q in ("EOB", "/EOB"):
                rows = []
                for v in state.get("_eobc", {}).values():
                    if v.get("day") != today:
                        continue
                    for r in v["rows"]:
                        sides = found.get(r["name"], {}).get(int(r["k"]))
                        vv = _row(r, sides, idxinfo.get(r["name"], (0, []))[1]) if sides else None
                        if vv:
                            rows.append(dict(r, v=vv))
                send(_eobc_message(rows, now) if rows else f"🔱 <b>{name} · EOB</b>\nnone today")
                continue
            m = re.fullmatch(r"/?(\d{4,6})(CE|PE)?", q)
            if m and name in contracts:
                types = (m.group(2),) if m.group(2) else ("CE", "PE")
                send(_ask_reply(name, int(m.group(1)), types, contracts, fetch_option, now, cache))
        except Exception as e:
            print(f"options reply '{q}': {e}")
    return sent
