"""
Options con box table (TRIDENT 5m con box on each option chart) -> Telegram, NIFTY + SENSEX, Dhan data only.

Strikes : ATM +-5 (11 CE + 11 PE) of the nearest weekly expiry (the expiring contract on expiry day).
          ATM is taken at 09:20 (close of the 09:15 candle) and moved ONLY when the index closes
          beyond the 5th (outer) strike on either side -> new ATM +-5 around that close.
Tracking: each strike's latest con box is tracked from when it forms (label, Min Bars).
Message : ONLY when a tracked box BREAKS (5m close outside), goes BACK INSIDE (tracking paused),
          or BREAKS AGAIN. A new box on that strike -> tracking moves to it.
          Index direction first (index 5m con box), then each strike with ✅ if it agrees with the index
          (index UP: CE up / PE down, index DOWN: CE down / PE up), 1.5x / 2x / 2.5x listed vertically.
Order   : Fri-Tue NIFTY first, Wed-Thu SENSEX first.

EOB CONFIRMED (ATM +-2, same strike): on the SAME 5m candle the CE makes a bull EOB and the PE a bear EOB
          (both point UP), or CE bear + PE bull (both point DOWN). TRIDENT EOB rules + colours on 5m option
          candles. One message per check; later edited for a colour change (dark) or ❌ INVALID
          (either side closes beyond its swing). No index check.
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
SIDE_N = int(os.getenv("OPT_STRIKES_EACH_SIDE", "5"))  # strikes shown on each side of the ATM
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


# ───────────────────────── strikes: ATM at 09:20, re-centre only beyond the 5th strike ─────────────────────────
def centre(m5_index, now, step):
    """ATM from the 09:15 candle close; moves only when a 5m close is beyond the outer strike (ATM +- SIDE_N steps)."""
    today = pd.Timestamp(now).date()
    d = m5_index[[x.date() == today for x in m5_index.index]]
    d = d[[(x + pd.Timedelta(minutes=5)) <= now for x in d.index]]
    if d.empty:
        return None, None
    atm = lambda p: int(round(p / step)) * step
    c = atm(float(d["Close"].iloc[0]))
    for v in d["Close"].iloc[1:]:
        if v > c + SIDE_N * step or v < c - SIDE_N * step:
            c = atm(float(v))
    return c, float(d["Close"].iloc[-1])


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
    formed = {}
    for e in events:
        if e["kind"] == "F":
            formed.setdefault(e["box"]["firstT"], e["end"])
    boxes = sorted([(formed[r["firstT"]], r) for r in recs if r["firstT"] in formed], key=lambda x: x[0])
    out = []
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


def _event_text(k, typ, ev, price, idx_dir):
    hh = pd.Timestamp(ev["end"], unit="ms", tz=IST).strftime("%H:%M")
    if ev["kind"] == "IN":
        return (f"↩ <b>{k} {typ} · BACK INSIDE at {hh}</b> · now {price:.2f}\n"
                f"Box {ev['lo']:.2f} – {ev['hi']:.2f} · tracking paused"), 3
    sd = ev["side"]
    want = idx_dir if typ == "CE" else -idx_dir             # CE moves with the index, PE against it
    mark, grp = ("✅", 0) if idx_dir and sd == want else ("❌", 1) if idx_dir else ("▫️", 2)
    lo, hi = ev["lo"], ev["hi"]
    rg = hi - lo
    rows = []
    for m in (1.5, 2.0, 2.5):
        lv = lo + m * rg if sd == 1 else hi - m * rg
        hit = any(v >= lv for v in ev["after"]) if sd == 1 else any(v <= lv for v in ev["after"])
        rows.append(f"{m:g}x".ljust(4) + f" = {lv:.2f}" + (" ✓" if hit else ""))
    return (f"{mark} <b>{k} {typ} · BROKE {'UP ⬆' if sd == 1 else 'DOWN ⬇'} at {hh}</b> · now {price:.2f}\n"
            f"Box {lo:.2f} – {hi:.2f}\n<pre>" + "\n".join(rows) + "</pre>"), grp


# ───────────────────────── EOB CONFIRMED: CE + PE opposite EOBs on the same 5m candle ─────────────────────────
def _eobs(df, now):
    df = _today_closed(df, now)
    if len(df) < 3 or "Open" not in df:
        return []
    eobs, _ = EOB.replay(df, 0.05, tf_ms=5 * 60000)
    return eobs


def _pairs(ce, pe):
    """Confirmed pairs: CE bull + PE bear (UP) / CE bear + PE bull (DOWN), same swing candle."""
    out = []
    for a in ce:
        for b in pe:
            if a["swT"] == b["swT"] and a["bear"] != b["bear"]:
                out.append(dict(dir=-1 if a["bear"] else 1, ce=a, pe=b))
    return out


def _side_line(typ, r):
    emo, col = EOB.COLOURS[("bear" if r["bear"] else "bull", r["hp"], r["swp"])]
    kind = "bear" if r["bear"] else "bull"
    return f"{typ} {emo} {col} ({kind}) · zone {r['bot']:.2f} – {r['top']:.2f}"


def _pair_text(p):
    hh = pd.Timestamp(p["ce"]["madeEnd"], unit="ms", tz=IST).strftime("%H:%M")
    head = f"{p['name']} {p['k']} · {hh}"
    if p["ce"]["invalid"] or p["pe"]["invalid"]:
        why = []
        for typ in ("ce", "pe"):
            r = p[typ]
            if r["invalid"]:
                at = pd.Timestamp(r["invAt"], unit="ms", tz=IST).strftime("%H:%M")
                why.append(f"{typ.upper()} closed {'above' if r['bear'] else 'below'} {r['lvl']:.2f} at {at}")
        return f"❌ <b>INVALID</b> — <s>{head} · {'UP' if p['dir'] == 1 else 'DOWN'}</s>\n" + " · ".join(why)
    arrow = "📈 Both point UP ⬆" if p["dir"] == 1 else "📉 Both point DOWN ⬇"
    ce, pe = p["ce"], p["pe"]
    return "\n".join([
        f"<b>{head}</b> · {arrow}",
        _side_line("CE", ce),
        _side_line("PE", pe),
        f"Invalid if CE closes {'above' if ce['bear'] else 'below'} {ce['lvl']:.2f} "
        f"or PE closes {'above' if pe['bear'] else 'below'} {pe['lvl']:.2f}"])


def _eobc_message(pairs, label):
    return "\n\n".join([f"🔱 <b>OPTIONS · EOB CONFIRMED (5m)</b> · {label}"] +
                       [_pair_text(p) for p in pairs])


def run_eob_confirm(found, state, now, send, edit, names):
    """found: {name: {strike: {"CE": eobs, "PE": eobs}}} for ATM +-2 (plus strikes of today's sent pairs)."""
    st = state.setdefault("_eobc", {})
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    for mid in [m for m, v in st.items() if v.get("day") != today]:
        st.pop(mid)                                   # yesterday's messages are no longer updated
    cutoff = CB._ms(now) - WINDOW_MIN * 60000
    sent_keys = {k for v in st.values() for k in v["keys"]}
    new = []
    for name in names:
        for k in sorted(found.get(name, {})):
            sides = found[name][k]
            if not sides.get("new"):
                continue
            for p in _pairs(sides.get("CE", []), sides.get("PE", [])):
                key = f"{name}|{k}|{p['ce']['swT']}|{p['dir']}"
                if key in sent_keys or p["ce"]["invalid"] or p["pe"]["invalid"] or p["ce"]["madeEnd"] < cutoff:
                    continue
                p.update(name=name, k=k, key=key)
                new.append(p)
    n, just = 0, None
    if new:
        label = f"{pd.Timestamp(now):%d %b %H:%M}"
        txt = _eobc_message(new, label)
        mid = send(txt)
        if mid:
            just = str(mid)
            st[just] = dict(day=today, keys=[p["key"] for p in new], text=txt, label=label)
            n = 1
    # update earlier messages (colour turned dark / a side became invalid)
    for mid, v in st.items():
        if mid == just:
            continue
        pairs = []
        for key in v["keys"]:
            name, k, swT, d = key.split("|")
            sides = found.get(name, {}).get(int(k))
            if sides is None:
                break
            match = [p for p in _pairs(sides.get("CE", []), sides.get("PE", []))
                     if str(p["ce"]["swT"]) == swT and str(p["dir"]) == d]
            if not match:
                break
            match[0].update(name=name, k=int(k), key=key)
            pairs.append(match[0])
        else:
            txt = _eobc_message(pairs, v["label"])
            if txt != v["text"] and edit and edit(int(mid), txt):
                v["text"] = txt
    return n


def run(m5_by_name, state, now, send, fetch_option, edit=None):
    """Called once per check after the index loop. fetch_option(seg, security_id, now) -> 5m DataFrame (today)."""
    if not OPT_ON:
        return 0
    contracts = load_contracts(now)
    cutoff = CB._ms(now) - WINDOW_MIN * 60000
    blocks, keys, log = [], [], []
    found = {}                                             # EOB confirm: {name: {strike: {"CE": [...], "PE": [...]}}}
    want = {}                                              # strikes of today's sent EOB-confirm pairs (kept updated)
    for v in state.get("_eobc", {}).values():
        for key in v["keys"]:
            nm, kk = key.split("|")[:2]
            want.setdefault(nm, set()).add(int(kk))
    names = order_names(now)
    for name in names:
        if name not in contracts or name not in m5_by_name:
            continue
        step = CFG[name]["step"]
        c, spot = centre(m5_by_name[name], now, step)
        if c is None:
            continue
        idx_dir, idx_line = _index_dir(m5_by_name[name], now)
        items = []
        strikes = [c + j * step for j in range(-SIDE_N, SIDE_N + 1)]
        eob_new = {c + j * step for j in range(-EOBC_SIDE, EOBC_SIDE + 1)}
        eob_want = eob_new | want.get(name, set())
        for k in strikes + sorted(eob_want - set(strikes)):
            for typ in ("CE", "PE"):
                sid = contracts[name]["map"].get(f"{k}{typ}")
                if not sid:
                    continue
                try:
                    df = fetch_option(CFG[name]["seg"], sid, now)
                    if EOBC_ON and k in eob_want:
                        slot = found.setdefault(name, {}).setdefault(k, {"new": k in eob_new})
                        slot[typ] = _eobs(df, now)
                    if k not in strikes:
                        time.sleep(0.22)
                        continue
                    price, evs = _track(df, now)
                except Exception as e:
                    print(f"options {name} {k}{typ}: {e}")
                    continue
                for ev in evs:
                    skey = f"{name}|optk|{k}{typ}|{ev['firstT']}|{ev['kind']}{ev['side']}|{ev['end']}"
                    if skey in state or ev["end"] < cutoff:
                        continue
                    txt, grp = _event_text(k, typ, ev, price, idx_dir)
                    items.append((grp, k, typ, txt))
                    keys.append(skey)
                time.sleep(0.22)                          # stay under Dhan's data rate limit
        log.append(f"{name} ATM {c} ({len(items)} new)")
        if items:
            items.sort(key=lambda x: (x[0], x[1], x[2]))
            exp = pd.Timestamp(contracts[name]["expiry"]).strftime("%d %b")
            head = "\n".join([f"🔁 <b>{name} OPTIONS · {pd.Timestamp(now):%H:%M}</b> · exp {exp} · ATM {c}", idx_line])
            blocks.append((head, [x[3] for x in items]))
    print("options: " + ", ".join(log))
    eobc = 0
    if EOBC_ON:
        try:
            eobc = run_eob_confirm(found, state, now, send, edit, names)
        except Exception as e:
            print("options EOB confirm error:", e)
    if not blocks:
        return eobc
    legend = "✅ agrees with index (index UP: CE up / PE down · index DOWN: CE down / PE up) · ❌ against · ▫️ index inside box"
    # build messages up to ~3800 characters, split only between strikes (keeps the formatting intact)
    msgs, cur = [], ""
    for head, parts in blocks:
        piece = head + "\n"
        for ptxt in parts:
            if len(cur) + len(piece) + len(ptxt) + 2 > 3800:
                msgs.append((cur + "\n\n" + piece).strip() if cur else piece.strip())
                cur, piece = "", head + " (cont.)\n"
            piece += "\n" + ptxt
        cur = (cur + "\n\n" + piece) if cur else piece
    msgs.append(cur.strip())
    msgs[-1] += "\n\n" + legend
    ok = all(send(m) for m in msgs)
    if ok:
        for k in keys:
            state[k] = pd.Timestamp(now).isoformat()
        return len(msgs) + eobc
    return eobc
