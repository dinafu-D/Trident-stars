"""
Options con box table (TRIDENT 5m con box on each option chart) -> Telegram, NIFTY + SENSEX, Dhan data only.

CON BOX : ATM +-2 (ATM recalculated every 5 min from the index spot), buy side only
          (index DOWN -> PE, UP -> CE; index inside / no box -> CE + PE). Nearest weekly expiry
          (the expiring contract on expiry day). One BOARD message per index, edited silently every 5 min:
          latest box today (or the last old box), time in box, break side / time, x move, 1.5x / 2x / 2.5x,
          back inside, and the option's own 1h MTF box (inside at level + time inside / broke + time).
          A NEW message (with sound) when a box forms, breaks, goes back inside or breaks again.
Order   : Fri-Tue NIFTY first, Wed-Thu SENSEX first.

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
SIDE_N = int(os.getenv("OPT_STRIKES_EACH_SIDE", "2"))  # con box board: strikes each side of the ATM
CFG = {"NIFTY": dict(exch="NSE", seg="NSE_FNO", prefix="NIFTY-", step=50),
       "SENSEX": dict(exch="BSE", seg="BSE_FNO", prefix="SENSEX-", step=100)}
EOBC_ON = os.getenv("OPT_EOB_CONFIRM", "1") == "1"
EOBC_SIDE = int(os.getenv("OPT_EOB_STRIKES_EACH_SIDE", "2"))   # ATM +-2
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"


def order_names(now):
    """Fri-Tue NIFTY first, Wed-Thu SENSEX first."""
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


# ───────────────────────── con box BOARD (one edited message per index) ─────────────────────────
def _hm2(ms, now):
    x = pd.Timestamp(ms, unit="ms", tz=IST)
    return x.strftime("%H:%M") if x.date() == pd.Timestamp(now).date() else x.strftime("%d %b %H:%M")


def _fibs(lo, hi, side, after):
    rg = hi - lo
    out = []
    for m in (1.5, 2.0, 2.5):
        lv = lo + m * rg if side == 1 else hi - m * rg
        hit = any(v >= lv for v in after) if side == 1 else any(v <= lv for v in after)
        out.append((m, lv, hit))
    return out


def _board_strike(k, typ, df, now):
    """(board text, 1h box line) for one option: latest box today, else the last old box."""
    dfc = df[[(x + pd.Timedelta(minutes=5)) <= now for x in df.index]]
    if len(dfc) < 3:
        return f"<b>{k} {typ}</b> · no data yet", ""
    CB.replay(dfc, 0.05)
    recs, C, t = CB.LAST["recs"], CB.LAST["C"], CB.LAST["t"]
    price = C[-1]
    mtf = ""
    try:
        mtf = CB.mtf_line(CB.mtf_status(dfc, now, 0.05), now)
    except Exception as e:
        print(f"options {k}{typ} 1h box: {e}")
    head = f"<b>{k} {typ}</b> · {price:.2f}"
    live = [r for r in recs if not r["dead"]]
    if not live:
        return "\n".join([head, "  no con box yet"] + ([f"  {mtf}"] if mtf else [])), mtf
    today = pd.Timestamp(now).date()
    tb = [r for r in live if pd.Timestamp(r["firstT"], unit="ms", tz=IST).date() == today]
    r = max(tb, key=lambda x: x["firstT"]) if tb else max(live, key=lambda x: (x["rightT"], x["firstT"]))
    hi, lo = r["hi"], r["lo"]
    rg = (hi - lo) or 1e-9
    bars = r["rightIdx"] - r["leftIdx"] + 1
    old = "Box" if tb else f"Old box ({pd.Timestamp(r['firstT'], unit='ms', tz=IST):%d %b})"
    lines = [head]
    if r["live"]:
        x = (price - lo) / rg
        lvl = 1.0 if x >= 1 else 0.75 if x >= 0.75 else 0.5 if x >= 0.5 else 0.25 if x >= 0.25 else 0.0
        lines.append(f"  {'🆕 ' if tb else ''}{old} {lo:.2f}–{hi:.2f} · from {_hm2(r['firstT'], now)} "
                     f"({CB._mins(bars)}) · inside at {lvl:g}")
    else:
        bi = r.get("brkIdx", -1)
        bi = bi if 0 <= bi < len(C) else len(C) - 1
        lines.append(f"  {old} {lo:.2f}–{hi:.2f} · {_hm2(r['firstT'], now)}→{_hm2(t[bi] + 300000, now)} "
                     f"({CB._mins(bars)} in box)")
        # side now: walk closes after the first break (break / back inside / break again)
        sd_now, since = 0, bi
        for j in range(bi, len(C)):
            sd = 1 if C[j] > hi else -1 if C[j] < lo else 0
            if sd != sd_now:
                sd_now, since = sd, j
        first = r["brk1"] if r["brk1"] in (1, -1) else (1 if C[bi] > hi else -1)
        b1 = f"{'⬆ BROKE UP' if first == 1 else '⬇ BROKE DOWN'} {_hm2(t[bi] + 300000, now)}"
        if sd_now == 0:
            lines.append(f"  {b1} · ↩ back inside since {_hm2(t[since] + 300000, now)}")
        else:
            xm = (price - lo) / rg if sd_now == 1 else (hi - price) / rg
            again = "" if sd_now == first and since == bi else \
                f" · now {'UP ⬆' if sd_now == 1 else 'DOWN ⬇'} since {_hm2(t[since] + 300000, now)}"
            lines.append(f"  {b1}{again} · {xm:.2f}x")
            fb = _fibs(lo, hi, sd_now, C[since:])
            lines.append("  " + " · ".join(f"{m:g}x {lv:.2f}" + (" ✓" if h else "") for m, lv, h in fb))
    if mtf:
        lines.append(f"  {mtf}")
    return "\n".join(lines), mtf


def _sound_text(k, typ, ev, price, mtf, now):
    """Short alert (new message, with sound) for NEW box / BRK / IN."""
    hh = _hm2(ev["end"], now)
    box = f"Box {ev['lo']:.2f}–{ev['hi']:.2f}"
    if ev["kind"] == "NEW":
        out = f"🆕 <b>{k} {typ} · NEW CON BOX {hh}</b> · {price:.2f}\n{box}"
    elif ev["kind"] == "IN":
        out = f"↩ <b>{k} {typ} · BACK INSIDE {hh}</b> · {price:.2f}\n{box}"
    else:
        sd = ev["side"]
        rows = [f"{m:g}x".ljust(4) + f" = {lv:.2f}" + (" ✓" if h else "")
                for m, lv, h in _fibs(ev["lo"], ev["hi"], sd, ev["after"])]
        out = (f"{'⬆' if sd == 1 else '⬇'} <b>{k} {typ} · BROKE {'UP' if sd == 1 else 'DOWN'} {hh}</b> · {price:.2f}\n"
               f"{box}\n<pre>" + "\n".join(rows) + "</pre>")
    return out + (f"\n{mtf}" if mtf else "")


def _post_board(state, name, txt, now, send, edit):
    bd = state.setdefault("_optboard", {})
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    cur = bd.get(name)
    if cur and cur.get("day") == today and cur.get("mid"):
        if cur.get("text") == txt:
            return 0
        if edit and edit(cur["mid"], txt):
            cur["text"] = txt
            return 0
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


def _eobc_message(rows, now):
    """Table per index + direction:  Strike  PE  Time  CE  IN"""
    out = ["🔱 <b>OPTIONS · EOB</b>"]
    groups = {}
    for row in rows:
        groups.setdefault((row["name"], row["dir"]), []).append(row)
    for (name, d), rs in groups.items():
        buy, opp = ("PE", "CE") if d == -1 else ("CE", "PE")
        head = f"📉 Index DOWN ⬇ → buy PE" if d == -1 else f"📈 Index UP ⬆ → buy CE"
        lines = [f"{'Strike':<8} {buy:<11} {'Time':<7} {opp:<4} IN"]
        for row in sorted(rs, key=lambda x: (x["swT"], x["k"])):
            v = row["v"]
            k = f"{row['k']:<8}"
            if v["r"]["invalid"]:
                lines.append(f"{k} ❌ INVALID · closed below {v['r']['lvl']:.2f} at {_hh(v['r']['invAt'])}")
                continue
            emo = _col(v["r"])[0]
            px = f"{v['px']:.2f}" if v["px"] is not None else "–"
            lines.append(f"{k} {emo} {px:<8} {_hh(row['swT']):<7} {'✅' if v['opp'] else '❌'}   "
                         f"{'✅' if v['idx'] else '❌'}")
        out.append(f"\n<b>{name}</b> · {head}\n<pre>" + "\n".join(lines) + "</pre>")
    out.append(f"<i>Price = last 5m close at {pd.Timestamp(now):%H:%M} (updates every 5 min)\n"
               f"Time = swing candle · CE/PE = other option made the opposite EOB at the same time · "
               f"IN = index made the same-direction EOB at the same time\n"
               f"🟩 green 🟢 dark green · 🟦 blue 🔵 dark blue</i>")
    return "\n".join(out)


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


def run(m5_by_name, state, now, send, fetch_option, edit=None):
    """Called once per check after the index loop. fetch_option(seg, security_id, now) -> 5m DataFrame (few days)."""
    if not OPT_ON:
        return 0
    contracts = load_contracts(now)
    cutoff = CB._ms(now) - WINDOW_MIN * 60000
    found = {}                                             # option EOB table: {name: {strike: {"CE": [...], ...}}}
    want = {}                                              # strikes of today's sent EOB rows (kept updated)
    for v in state.get("_eobc", {}).values():
        for r in v.get("rows", []):
            want.setdefault(r["name"], set()).add(int(r["k"]))
    idxinfo, log, sounds, keys, sent = {}, [], [], [], 0
    names = order_names(now)
    for name in names:
        if name not in contracts or name not in m5_by_name:
            continue
        step = CFG[name]["step"]
        m5 = m5_by_name[name]
        closed = m5[[(x + pd.Timedelta(minutes=5)) <= now for x in m5.index]]
        if closed.empty:
            continue
        spot = float(closed["Close"].iloc[-1])
        atm = int(round(spot / step)) * step                 # recalculated every check
        idx_dir, idx_line = _index_dir(m5, now)
        if EOBC_ON:
            try:
                idxinfo[name] = (idx_dir, _idx_eobs(m5, now))
            except Exception as e:
                print(f"options {name} index EOB: {e}")
        strikes = [atm + j * step for j in range(-SIDE_N, SIDE_N + 1)]
        eob_new = {atm + j * step for j in range(-EOBC_SIDE, EOBC_SIDE + 1)}
        buy = ("PE",) if idx_dir == -1 else ("CE",) if idx_dir == 1 else ("CE", "PE")
        board, snd = [], []
        for k in sorted(set(strikes) | eob_new | want.get(name, set())):
            for typ in ("CE", "PE"):
                sid = contracts[name]["map"].get(f"{k}{typ}")
                if not sid:
                    continue
                try:
                    df = fetch_option(CFG[name]["seg"], sid, now)
                    time.sleep(0.22)                       # stay under Dhan's data rate limit
                    if EOBC_ON:
                        slot = found.setdefault(name, {}).setdefault(k, {"new": k in eob_new})
                        slot[typ] = _eobs(df, now)
                        dc = _today_closed(df, now)
                        slot[typ + "px"] = float(dc["Close"].iloc[-1]) if len(dc) else None
                    if k not in strikes or typ not in buy:
                        continue
                    btxt, mtf = _board_strike(k, typ, df, now)
                    board.append(btxt)
                    price, evs = _track(df, now)
                except Exception as e:
                    print(f"options {name} {k}{typ}: {e}")
                    continue
                for ev in evs:
                    skey = f"{name}|optcb|{k}{typ}|{ev['firstT']}|{ev['kind']}{ev['side']}|{ev['end']}"
                    if skey in state or ev["end"] < cutoff:
                        continue
                    snd.append(_sound_text(k, typ, ev, price, mtf, now))
                    keys.append(skey)
        exp = pd.Timestamp(contracts[name]["expiry"]).strftime("%d %b")
        side_txt = "Index DOWN ⬇ → PE" if idx_dir == -1 else "Index UP ⬆ → CE" if idx_dir == 1 else "Index inside / no box → CE + PE"
        head = f"📊 <b>{name} OPTIONS · CON BOX</b> · {pd.Timestamp(now):%H:%M} · exp {exp}\nATM {atm} · {side_txt}"
        txt = head + "\n" + idx_line + "\n\n" + "\n\n".join(board)
        if len(txt) > 4000:
            txt = txt[:3990] + "…"
        try:
            sent += _post_board(state, name, txt, now, send, edit)
        except Exception as e:
            print(f"options {name} board: {e}")
        if snd:
            sounds.append(f"🔔 <b>{name} OPTIONS · {pd.Timestamp(now):%H:%M}</b> · ATM {atm}\n\n" + "\n\n".join(snd))
        log.append(f"{name} ATM {atm} ({len(snd)} new)")
    print("options: " + ", ".join(log))
    if sounds:
        msgs, cur = [], ""
        for blk in sounds:
            if cur and len(cur) + len(blk) > 3800:
                msgs.append(cur)
                cur = ""
            cur = (cur + "\n\n" + blk) if cur else blk
        msgs.append(cur)
        if all(send(m[:4090]) for m in msgs):
            for k in keys:
                state[k] = pd.Timestamp(now).isoformat()
            sent += len(msgs)
    if EOBC_ON:
        try:
            sent += run_eob_confirm(found, idxinfo, state, now, send, edit, names)
        except Exception as e:
            print("options EOB confirm error:", e)
    return sent
