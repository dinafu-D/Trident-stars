"""
TRIDENT COMMODITY BOT  (own Telegram bot: COM_TELEGRAM_BOT_TOKEN / COM_TELEGRAM_CHAT_ID)

Assets   : MCX CRUDEOIL, CRUDEOILM, NATURALGAS, NATGASMINI, GOLD, GOLDM, SILVER, SILVERM
           (+ NSE crude oil / natural gas options, if Dhan gives them)
Source   : every option takes its direction from an MCX future —
           crude oil (MCX + NSE, normal + mini) -> MCX CRUDEOIL future | natural gas -> MCX NATURALGAS future |
           GOLD -> GOLD future | GOLDM -> GOLDM future | SILVER -> SILVER future | SILVERM -> SILVERM future
Strikes  : GOLD / GOLDM ATM +-1 on round 1000 strikes | SILVER / SILVERM ATM +-2 on round 1000 strikes |
           crude / natural gas ATM +-2 (their own strike gap) — recalculated every 5 min
Shown    : ONE commodity at a time = the nearest expiry; several on that expiry -> the bot asks (no answer =
           normal before mini); 'prio SILVERM' any time -> only that one for the day, 'prio auto' -> back
Normal day : futures board (edited) + options boards / alerts / EOB table of that commodity
Expiry day : its own futures status message (new message on change, old one deleted) + all futures
             alerts (con box, 1h box, trend, HL / LH, 1h EOB) + its options
Sound    : before 16:00 every message is silent
Hours    : Mon-Fri 09:00-23:55 IST
Typed    : eob | GOLDM 76000 | CRUDEOIL 6300CE | prio SILVERM | prio auto | 2 (answer to the question)
"""
import json
import os
import re
import time
from datetime import time as dtime

import pandas as pd

import conbox_alert as CB
import dhan_data as D
import eob_alert as EOB
import options_alert as O
import trident_star_alert as T

IST = "Asia/Kolkata"
CB.SESS_START = (9, 0)
CB.SESS_END = (23, 55)
EOB.SESS_END = (23, 55)

# the main script's Telegram helpers, pointed at the commodity bot and its own memory file
T.TG_TOKEN = os.getenv("COM_TELEGRAM_BOT_TOKEN", "")
T.TG_CHAT = os.getenv("COM_TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
T.STATE_FILE = os.getenv("COM_STATE_FILE", "state/commodity_state.json")

SYMS = ["CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI", "GOLD", "GOLDM", "SILVER", "SILVERM"]
MINI = {"CRUDEOILM", "NATGASMINI", "GOLDM", "SILVERM"}
SOURCE = {"CRUDEOIL": "CRUDEOIL", "CRUDEOILM": "CRUDEOIL", "NATURALGAS": "NATURALGAS", "NATGASMINI": "NATURALGAS",
          "GOLD": "GOLD", "GOLDM": "GOLDM", "SILVER": "SILVER", "SILVERM": "SILVERM"}
ROUND = {"GOLD": (1000, 1), "GOLDM": (1000, 1), "SILVER": (1000, 2), "SILVERM": (1000, 2)}   # (round step, each side)
OTHER_SIDE = int(os.getenv("COM_STRIKES_EACH_SIDE", "2"))          # crude / natural gas
NSE_ON = os.getenv("COM_NSE", "1") == "1"
NSE_SEG = os.getenv("NSE_COMM_SEG", "NSE_COMM")
SOUND_FROM = dtime(16, 0)
CACHE = os.path.join(os.path.dirname(T.STATE_FILE) or ".", "com_contracts.json")


# ───────────────────────── Telegram (commodity bot) ─────────────────────────
NOW = None


def send(text, reply_to=None, silent=False):
    """Silent before 16:00."""
    return T.send(text, reply_to, silent or NOW.time() < SOUND_FROM)


def edit(mid, text):
    return T.edit_msg(mid, text)


def delete(mid):
    return bool(T.TG_TOKEN and mid and mid > 0 and T._tg_del(T.TG_TOKEN, T.TG_CHAT, mid))


def asks(state):
    """Texts typed to the commodity bot since the last run (your chat only)."""
    if not T.TG_TOKEN:
        return []
    try:
        r = T.requests.get(f"https://api.telegram.org/bot{T.TG_TOKEN}/getUpdates", timeout=20,
                           params={"offset": int(state.get("_updid", 0)) + 1, "timeout": 0})
        res = r.json().get("result", []) if r.ok else []
    except Exception as e:
        print("Telegram getUpdates error:", str(e).replace(T.TG_TOKEN, "***"))
        return []
    out = []
    for u in res:
        state["_updid"] = max(int(state.get("_updid", 0)), int(u["update_id"]))
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) == str(T.TG_CHAT) and m.get("text"):
            out.append(m["text"].strip())
    if out:
        print(f"requests: {out}")
    return out


# ───────────────────────── contracts (Dhan scrip master) ─────────────────────────
def load_contracts(now):
    """{"fut": {SYM: {expiry, sid}}, "opt": {KEY: {sym, exch, seg, expiry, map}}} — nearest expiries."""
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    try:
        with open(CACHE) as f:
            c = json.load(f)
        if c.get("date") == today:
            return c["data"]
    except Exception:
        pass
    cols = ("SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID",
            "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE")
    df = pd.read_csv(O.SCRIP_MASTER, usecols=lambda x: x in cols, low_memory=False)
    df = df[df["SEM_INSTRUMENT_NAME"].astype(str).isin(["FUTCOM", "OPTFUT"])].copy()
    df["sym"] = df["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.split("-").str[0]
    df = df[df["sym"].isin(SYMS)]
    df["exp"] = pd.to_datetime(df["SEM_EXPIRY_DATE"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df[df["exp"] >= today]
    fut, opt = {}, {}
    for sym in SYMS:
        f = df[(df["SEM_EXM_EXCH_ID"].astype(str) == "MCX") & (df["SEM_INSTRUMENT_NAME"] == "FUTCOM") & (df["sym"] == sym)]
        if not f.empty:
            r = f.sort_values("exp").iloc[0]
            fut[sym] = dict(expiry=r["exp"], sid=str(int(r["SEM_SMST_SECURITY_ID"])))
        for exch in ("MCX", "NSE"):
            if exch == "NSE" and (not NSE_ON or sym not in ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI")):
                continue
            o = df[(df["SEM_EXM_EXCH_ID"].astype(str) == exch) & (df["SEM_INSTRUMENT_NAME"] == "OPTFUT") & (df["sym"] == sym)]
            if o.empty:
                continue
            exp = o["exp"].min()
            o = o[o["exp"] == exp]
            m = {}
            for _, r in o.iterrows():
                typ = str(r.get("SEM_OPTION_TYPE", "")).upper()
                s = str(r["SEM_TRADING_SYMBOL"]).upper()
                typ = typ if typ in ("CE", "PE") else ("CE" if s.endswith("CE") else "PE" if s.endswith("PE") else "")
                if typ:
                    m[f"{int(round(float(r['SEM_STRIKE_PRICE'])))}{typ}"] = str(int(r["SEM_SMST_SECURITY_ID"]))
            key = sym if exch == "MCX" else f"NSE {sym}"
            opt[key] = dict(sym=sym, exch=exch, seg="MCX_COMM" if exch == "MCX" else NSE_SEG, expiry=exp, map=m)
    data = dict(fut=fut, opt=opt)
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump({"date": today, "data": data}, f)
    print("contracts: futures " + ", ".join(f"{k} {v['expiry']}" for k, v in fut.items()))
    print("contracts: options " + ", ".join(f"{k} {v['expiry']} ({len(v['map'])})" for k, v in opt.items()))
    if NSE_ON and not any(k.startswith("NSE ") for k in opt):
        print("contracts: no NSE crude oil / natural gas options found in Dhan's list")
    return data


def fetch5(seg, sid, instrument, now, days):
    """5m candles (IST) of one MCX / NSE commodity contract."""
    today = pd.Timestamp(now).normalize().tz_localize(None)
    j = D._post("/charts/intraday", {
        "securityId": str(sid), "exchangeSegment": seg, "instrument": instrument, "interval": "5", "oi": False,
        "fromDate": (today - pd.Timedelta(days=days)).strftime("%Y-%m-%d 09:00:00"),
        "toDate": (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")})
    df = D._frame(j)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    time.sleep(0.22)                                            # Dhan data rate limit
    return df.dropna().astype(float).round(2)


# ───────────────────────── order + priority ─────────────────────────
def asset_exp(sym, c):
    e = [c["fut"][sym]["expiry"]] if sym in c["fut"] else []
    if sym in c["opt"]:
        e.append(c["opt"][sym]["expiry"])
    return min(e) if e else "9999-12-31"


def nearest_group(syms, c):
    """Assets sharing the nearest expiry, normal before mini."""
    if not syms:
        return None, []
    e = min(asset_exp(s, c) for s in syms)
    return e, sorted([s for s in syms if asset_exp(s, c) == e], key=lambda s: ((s in MINI), SYMS.index(s)))


def priority(state, c, today, replies, syms):
    """ONE commodity at a time. Default: the nearest expiry; if several share it, the bot asks
    (no answer = normal before mini). 'prio SILVERM' any time -> only that one for the rest of the day;
    'prio auto' -> back to nearest expiry. Returns the chosen symbol."""
    pr = state.get("_prio", {})
    if pr.get("date") != today:
        pr = dict(date=today, pick=None, asked={}, last=None)
    e, grp = nearest_group(syms, c)
    for q in replies:
        u = q.upper().strip()
        m = re.fullmatch(r"PRIO\s+(\w+)", u)
        if m:
            w = m.group(1)
            if w == "AUTO":
                pr["pick"] = None
                send("✅ Back to nearest expiry")
            elif w in syms:
                pr["pick"] = w
                send(f"✅ Showing only <b>{w}</b> today (type <code>prio auto</code> to go back)")
            else:
                send(f"❓ {w} is not in the list: " + ", ".join(syms))
            continue
        if re.fullmatch(r"[\d\s,]+", u) and pr.get("last") == e and len(grp) > 1:
            n = int(re.findall(r"\d", u)[0])
            if 1 <= n <= len(grp):
                pr["pick"] = grp[n - 1]
                send(f"✅ Showing only <b>{grp[n - 1]}</b> today")
    if len(grp) > 1 and e not in pr["asked"] and not pr["pick"]:
        lines = [f"{i + 1}  {s}" for i, s in enumerate(grp)]
        mid = send(f"❓ <b>Same expiry {pd.Timestamp(e):%d %b}</b> — which one?\n<pre>" + "\n".join(lines) + "</pre>\n"
                   f"Reply the number (no answer = {grp[0]})")
        pr["asked"][e] = mid or 0
        pr["last"] = e
    state["_prio"] = pr
    return pr["pick"] if pr["pick"] in syms else (grp[0] if grp else None)


# ───────────────────────── helpers ─────────────────────────
def pick_strikes(sym, ser, spot):
    strikes = sorted({int(k[:-2]) for k in ser["map"]})
    if sym in ROUND:
        step, n = ROUND[sym]
        cand = [s for s in strikes if s % step == 0]
    else:
        n, cand = OTHER_SIDE, strikes
    if not cand:
        return []
    i = min(range(len(cand)), key=lambda j: abs(cand[j] - spot))
    return cand[max(0, i - n): i + n + 1]


def h1_txt(h):
    if not h:
        return "—"
    s5 = CB.h1_side(h)
    if s5 == "In":
        return "In"
    conf = (not h["live"]) and h["side"] == (1 if s5 == "U" else -1)
    return f"{s5} 1h✅" if conf else f"{s5} 5m"


def dmy(e):
    return pd.Timestamp(e).strftime("%d%b")


# ───────────────────────── futures ─────────────────────────
def fut_board(syms, c, fdat, now, state, moved):
    rows = [f"{'Fut':<11}{'Exp':<6}{'Box':<7}{'Time':<5}{'St':<4}1h"]
    for s in syms:
        st = O.strike_state(fdat[s], now) if s in fdat else None
        exp = dmy(c["fut"][s]["expiry"]) if s in c["fut"] else "-"
        if not st:
            rows.append(f"{s:<11}{exp:<6}—")
            continue
        rows.append(f"{s:<11}{exp:<6}{st['tag']:<7}{st['dur']:<5}{st['st']:<4}{h1_txt(st['h'])}")
    txt = f"📊 <b>COMMODITY FUTURES · {O._upd(now)}</b>\n<pre>" + "\n".join(rows) + "</pre>"
    return O._post_board(state, "_FUT", txt, now, send, edit, delete, moved=moved)


def expiry_status(sym, df, now, state):
    """⏳ SILVERM · EXPIRY TODAY — new message on change (old one deleted), otherwise edited."""
    st = O.strike_state(df, now)
    CB.replay(df[[(x + pd.Timedelta(minutes=5)) <= now for x in df.index]], 0.05)
    tr = (CB.LAST.get("tr") or {}).get("st", 0)
    rows = [f"{'Box':<8}{'Time':<6}{'Status':<8}{'Trend':<6}1h"]
    sig = "none"
    if st:
        rows.append(f"{st['tag']:<8}{st['dur']:<6}{st['st']:<8}{CB._tr_word(tr):<6}{h1_txt(st['h'])}")
        sig = f"{st['firstT']}|{st['st']}|{st['tag']}|{h1_txt(st['h'])}|{tr}"
        recs = [r for r in CB.LAST["recs"] if r["firstT"] == st["firstT"]]
        if st["st"] in ("U", "D") and recs:
            r = recs[0]
            rows += [f"{f'{m:g}x':<8}{CB._p(lv)}" for m, lv in CB._targets(r["lo"], r["hi"], 1 if st["st"] == "U" else -1)]
    else:
        rows.append("—")
    txt = f"⏳ <b>{sym} · EXPIRY TODAY · {O._upd(now)}</b>\n<pre>" + "\n".join(rows) + "</pre>"
    es = state.setdefault("_expst", {})
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    cur = es.get(sym) if (es.get(sym) or {}).get("day") == today else None
    if cur and cur.get("sig") == sig:
        if cur.get("text") != txt and cur.get("mid") and edit(cur["mid"], txt):
            cur["text"] = txt
        return 0
    if cur and cur.get("mid"):
        delete(cur["mid"])
    mid = send(txt)
    es[sym] = dict(day=today, sig=sig, mid=mid if isinstance(mid, int) and mid > 0 else None, text=txt)
    return 1 if mid else 0


# ───────────────────────── options (one series) ─────────────────────────
def series(key, ser, src, now, state, found, idxinfo, boards, alerts, changed, cache):
    """Board rows + NEW / U / D / RE-IN alerts + 1h box alerts for one option series (like the index options)."""
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    closed = src[[(x + pd.Timedelta(minutes=5)) <= now for x in src.index]]
    if closed.empty:
        return
    spot = float(closed["Close"].iloc[-1])
    d, _ = O._index_dir(src, now)
    try:
        idxinfo[key] = (d, O._idx_eobs(src, now))
    except Exception as e:
        print(f"{key} source EOB: {e}")
    strikes = pick_strikes(ser["sym"], ser, spot)
    buy = ("PE",) if d == -1 else ("CE",) if d == 1 else ("CE", "PE")
    last_st = state.setdefault("_optst", {})
    h1st = state.setdefault("_opt1h", {})
    want = {int(r["k"]) for v in state.get("_eobc", {}).values() for r in v.get("rows", []) if r["name"] == key}
    board, al, h1rows = [], [], []
    for k in sorted(set(strikes) | want):
        for typ in ("CE", "PE"):
            sid = ser["map"].get(f"{k}{typ}")
            if not sid:
                continue
            try:
                df = fetch5(ser["seg"], sid, "OPTFUT", now, 7)
                cache[(key, k, typ)] = df
                slot = found.setdefault(key, {}).setdefault(k, {"new": k in strikes})
                slot[typ] = O._eobs(df, now)
                dc = O._today_closed(df, now)
                slot[typ + "px"] = float(dc["Close"].iloc[-1]) if len(dc) else None
                if k not in strikes or typ not in buy:
                    continue
                st = O.strike_state(df, now)
            except Exception as e:
                print(f"{key} {k}{typ}: {e}")
                continue
            board.append(O._brow(f"{k}{typ[0]}" if len(buy) == 2 else str(k), st))
            sk = f"{key}|{k}{typ}"
            prev = last_st.get(sk)
            if st and prev and prev.get("day") == today:
                row = dict(key=sk, label=f"{k}{typ}", firstT=st["firstT"], tag=st["tag"], dur=st["dur"])
                if st["today"] and st["firstT"] != prev.get("firstT"):
                    al.append(dict(row, lab="NEW", active=st["st"] == "In"))
                elif st["firstT"] == prev.get("firstT") and st["st"] in ("U", "D") and st["st"] != prev.get("st"):
                    al.append(dict(row, lab=st["st"], active=False))
                elif st["firstT"] == prev.get("firstT") and st["st"] == "In" and prev.get("st") in ("U", "D"):
                    al.append(dict(row, lab="RE-IN " + prev.get("st"), active=True))
            if st:
                last_st[sk] = dict(day=today, firstT=st["firstT"], st=st["st"])
                state.setdefault("_cur", {})[sk] = dict(firstT=st["firstT"], st=st["st"], tag=st["tag"], dur=st["dur"])
            if st and st.get("h") and O.H1_ALERTS:
                evs, hcur = CB.h1_transitions(h1st.get(sk), st["h"])
                for e in evs:
                    lab = f"{e[2]} 5m" if e.startswith("5m") else f"{e[2]} 1h ✅" if e.startswith("1h") else e
                    h1rows.append(dict(key=sk, label=f"{k}{typ}", dur=st["h"]["dur"], lab=lab,
                                       reply=hcur.get("mid") if e.startswith("1h") else None, ev=e))
                h1st[sk] = hcur
    arrow = "⬇ PE" if d == -1 else "⬆ CE" if d == 1 else "CE + PE"
    hdr = f"{'Strike':<9}{'Box':<8}{'Time':<6}Status"
    boards[key] = (f"📊 <b>{key} OPTIONS · {O._upd(now)} · exp {dmy(ser['expiry'])} · {arrow}</b>\n<pre>"
                   + "\n".join([hdr] + (board or ["—"])) + "</pre>")
    if al:
        alerts.append((f"🔔 <b>{key} OPTIONS · {O._upd(now)}</b>", al))
        changed.add(key)
    if h1rows:
        changed.add(key)
        body = [f"{'Strike':<9}{'Box':<5}{'Time':<6}Status"] + \
               [f"{r['label']:<9}{'1h':<5}{r['dur']:<6}{r['lab']}" for r in h1rows]
        rto = next((r["reply"] for r in h1rows if r["reply"]), None)
        mid = send(f"🔔 <b>{key} OPTIONS · 1h BOX · {O._upd(now)}</b>\n<pre>" + "\n".join(body) + "</pre>", reply_to=rto)
        if mid and isinstance(mid, int) and mid > 0:
            for r in h1rows:
                if r["ev"].startswith("5m"):
                    h1st[r["key"]]["mid"] = mid
    print(f"{key}: spot {spot:.2f}, strikes {strikes}, {len(al)} alerts")


def send_alerts(alerts, state, now):
    """NEW / U / D / RE-IN alerts; rows still inside their box are edited every 5 min."""
    today = pd.Timestamp(now).strftime("%Y-%m-%d")
    alst = state.setdefault("_optal", {})
    for mid in [m for m, v in alst.items() if v.get("day") != today]:
        alst.pop(mid)
    sent, just = 0, set()
    for head, rows in alerts:
        mid = send(O._al_text(head, rows))
        if mid:
            sent += 1
            if isinstance(mid, int) and mid > 0 and any(r["active"] for r in rows):
                alst[str(mid)] = dict(day=today, head=head, rows=rows)
                just.add(str(mid))
    cur = state.get("_cur", {})
    for mid, v in alst.items():
        if mid in just:
            continue
        for r in v["rows"]:
            if not r["active"] or r["key"] not in cur:
                continue
            c = cur[r["key"]]
            if c["firstT"] == r["firstT"] and c["st"] == "In":
                r.update(tag=c["tag"], dur=c["dur"])
            else:
                r["active"] = False
        edit(int(mid), O._al_text(v["head"], v["rows"], upd=O._upd(now)))
    for mid in [m for m, v in alst.items() if not any(r["active"] for r in v["rows"])]:
        alst.pop(mid)
    return sent


# ───────────────────────── typed requests ─────────────────────────
def answer(q, c, found, idxinfo, state, now, cache):
    u = re.sub(r"\s+", " ", q.upper()).strip()
    if u in ("EOB", "/EOB"):
        rows = []
        for v in state.get("_eobc", {}).values():
            if v.get("day") != pd.Timestamp(now).strftime("%Y-%m-%d"):
                continue
            for r in v["rows"]:
                sides = found.get(r["name"], {}).get(int(r["k"]))
                vv = O._row(r, sides, idxinfo.get(r["name"], (0, []))[1]) if sides else None
                if vv:
                    rows.append(dict(r, v=vv))
        send(O._eobc_message(rows, now) if rows else "🔱 <b>COMMODITY · EOB</b>\nnone today")
        return
    m = re.fullmatch(r"/?((?:NSE )?[A-Z]+) (\d+)(CE|PE)?", u)
    if not m or m.group(1) not in c["opt"]:
        if u in ("HELP", "/HELP", "/START"):
            send("Type: <code>eob</code> · <code>GOLDM 76000</code> · <code>CRUDEOIL 6300CE</code> · "
                 "<code>prio SILVERM</code> · <code>prio auto</code>")
        return
    key, k = m.group(1), int(m.group(2))
    ser = c["opt"][key]
    rows, erows = [], []
    for typ in ((m.group(3),) if m.group(3) else ("CE", "PE")):
        sid = ser["map"].get(f"{k}{typ}")
        if not sid:
            rows.append(f"{typ:<9}no contract")
            continue
        df = cache.get((key, k, typ))
        if df is None:
            df = fetch5(ser["seg"], sid, "OPTFUT", now, 7)
        rows.append(O._brow(typ, O.strike_state(df, now)))
        eobs = O._eobs(df, now)
        dc = O._today_closed(df, now)
        px = f"{float(dc['Close'].iloc[-1]):.2f}" if len(dc) else "–"
        if eobs:
            e = max(eobs, key=lambda x: x["swT"])
            cell = "❌ invalid" if e["invalid"] else f"{O._col(e)[0]} {'bear' if e['bear'] else 'bull'}"
            erows.append(f"{typ:<6}{cell:<11}{O._hh(e['swT']):<7}{px}")
        else:
            erows.append(f"{typ:<6}{'—':<12}{'':<7}{px}")
    body = [f"{'Type':<9}{'Box':<8}{'Time':<6}Status"] + rows + ["", f"{'EOB':<6}{'Colour':<12}{'Time':<7}Price"] + erows
    send(f"🔎 <b>{key} {k} · {pd.Timestamp(now):%H:%M}</b>\n<pre>" + "\n".join(body) + "</pre>")


# ───────────────────────── main ─────────────────────────
def main():
    global NOW
    now = T.now_ist()
    NOW = now
    if os.getenv("FORCE", "0") != "1" and (now.weekday() >= 5 or not (dtime(9, 0) <= now.time() <= dtime(23, 59))):
        print("commodity: outside MCX hours, exit")
        return 0
    if not T.TG_TOKEN:
        print("commodity: COM_TELEGRAM_BOT_TOKEN not set -> messages only printed here")
    state = T.load_state()
    today = now.strftime("%Y-%m-%d")
    replies = asks(state)
    c = load_contracts(now)
    syms = [s for s in SYMS if s in c["fut"] or s in c["opt"]]
    pick = priority(state, c, today, [q for q in replies if re.fullmatch(r"(?i)prio\s+\w+|[\d\s,]+", q.strip())], syms)
    active = [pick] if pick else []
    expiring = [s for s in active if asset_exp(s, c) == today]
    print(f"commodity: showing {active} ({'EXPIRY DAY' if expiring else 'normal day'})")

    # futures (sources + board)
    need = set(active) | {SOURCE[s] for s in active}
    fdat = {}
    for s in sorted(need):
        if s in c["fut"]:
            try:
                fdat[s] = fetch5("MCX_COMM", c["fut"][s]["sid"], "FUTCOM", now, 20)
            except Exception as e:
                print(f"{s} future: {e}")
    sent = 0
    if expiring:
        for s in active:
            if s not in fdat:
                continue
            try:
                sent += CB.run(s, fdat[s], state, now, send, edit)
                sent += EOB.run(s, CB.hourly(fdat[s], now), state, now, send, edit, T.remember_msg, T.delete_taken)
                sent += expiry_status(s, fdat[s], now, state)
            except Exception as e:
                print(f"{s} expiry futures: {e}")
    fut_changed = sent > 0

    # options
    found, idxinfo, boards, alerts, changed, cache = {}, {}, {}, [], set(), {}
    state["_cur"] = {}
    keys = [k for s in active for k in (s, f"NSE {s}") if k in c["opt"]]
    for key in keys:
        src = fdat.get(SOURCE[c["opt"][key]["sym"]])
        if src is None or src.empty:
            print(f"{key}: no source future data")
            continue
        try:
            series(key, c["opt"][key], src, now, state, found, idxinfo, boards, alerts, changed, cache)
        except Exception as e:
            print(f"{key} options: {e}")
    sent += send_alerts(alerts, state, now)
    try:
        n_eob = O.run_eob_confirm(found, idxinfo, state, now, send, edit, keys)
        sent += n_eob
        if n_eob:
            changed.update(keys)
    except Exception as e:
        print("commodity EOB table error:", e)

    # boards last (moved to the bottom when something was sent)
    if not expiring:
        try:
            fut_board(active, c, fdat, now, state, moved=bool(alerts) or fut_changed or bool(changed))
        except Exception as e:
            print(f"futures board: {e}")
    for key in keys:
        if key in boards:
            try:
                O._post_board(state, key, boards[key], now, send, edit, delete, moved=key in changed)
            except Exception as e:
                print(f"{key} board: {e}")
    bd = state.get("_optboard", {})
    for name in [n for n in bd if n != "_FUT" and n not in keys]:   # switched commodity: remove its old board
        if bd[name].get("mid"):
            delete(bd[name]["mid"])
        bd.pop(name)
    for q in replies:
        if re.fullmatch(r"(?i)prio\s+\w+|[\d\s,]+", q.strip()):
            continue
        try:
            answer(q, c, found, idxinfo, state, now, cache)
        except Exception as e:
            print(f"reply '{q}': {e}")
    T.save_state(state)
    print(f"commodity: sent {sent} alert(s)")
    return 0


if __name__ == "__main__":
    main()
