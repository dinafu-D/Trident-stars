"""
Dhan Data API candles for NIFTY / SENSEX (index), with automatic daily access token.

Needs 3 GitHub secrets:  DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET
  - the access token (valid 24 h) is made by the bot itself from PIN + TOTP and re-used until ~1 h before expiry
  - it is kept ENCRYPTED in the bot's memory folder (key = your TOTP secret), never in plain text

Candle data used:
  5m   : last ~59 days   (POST /v2/charts/intraday, interval 5)
  60m  : last ~1 year     (interval 60, in 90-day pieces)  -> 1h / 4h charts
  daily: full history     (POST /v2/charts/historical)
"""

import base64
import hashlib
import json
import os
import time
from datetime import time as dtime

import pandas as pd
import requests

IST = "Asia/Kolkata"
API = "https://api.dhan.co/v2"
AUTH = "https://auth.dhan.co/app/generateAccessToken"

CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
PIN = os.getenv("DHAN_PIN", "").strip()
TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "").replace(" ", "").strip()
TOKEN_FILE = os.path.join(os.path.dirname(os.getenv("STATE_FILE", "state/sent_stars.json")) or ".", "dhan_token.json")

# Dhan security IDs (index segment). SENSEX is looked up in Dhan's scrip list if 51 does not work.
IDS = {"NIFTY": "13", "SENSEX": "51"}
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"


def enabled():
    return bool(CLIENT_ID and PIN and TOTP_SECRET)


# ───────────────────────── token ─────────────────────────
def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(("trident|" + TOTP_SECRET + "|" + PIN).encode()).digest())
    return Fernet(key)


def _load_token():
    try:
        with open(TOKEN_FILE) as f:
            d = json.load(f)
        tok = _fernet().decrypt(d["t"].encode()).decode()
        return tok, float(d["exp"])
    except Exception:
        return None, 0.0


def _save_token(tok, exp):
    os.makedirs(os.path.dirname(TOKEN_FILE) or ".", exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({"t": _fernet().encrypt(tok.encode()).decode(), "exp": exp}, f)


def _new_token():
    import pyotp
    last = None
    for attempt in range(2):
        code = pyotp.TOTP(TOTP_SECRET).now()
        try:
            r = requests.post(AUTH, params={"dhanClientId": CLIENT_ID, "pin": PIN, "totp": code}, timeout=30)
        except Exception as e:
            # never print the error itself: it can contain the request address, which includes the PIN
            raise RuntimeError(f"Dhan login request failed ({type(e).__name__})") from None
        try:
            j = r.json()
        except Exception:
            j = {"http": r.status_code}
        tok = j.get("accessToken") if isinstance(j, dict) else None
        if isinstance(j, dict):                                   # keep only harmless fields in logs
            j = {k: v for k, v in j.items() if k in ("errorType", "errorCode", "errorMessage", "status", "message", "http")}
        if tok:
            _save_token(tok, time.time() + 23 * 3600)     # valid 24 h; renew 1 h early
            print("Dhan: new access token generated")
            return tok
        last = j
        time.sleep(31)                                   # wait for the next TOTP code and retry once
    raise RuntimeError(f"Dhan token failed: {last}")


_token = None


def token(force_new=False):
    global _token
    if _token and not force_new:
        return _token
    if not force_new:
        tok, exp = _load_token()
        if tok and exp - time.time() > 3600:
            _token = tok
            return tok
    _token = _new_token()
    return _token


# ───────────────────────── requests ─────────────────────────
def _post(path, body):
    status = None
    for attempt in range(3):
        hdr = {"access-token": token(force_new=(status in (401, 403))), "client-id": CLIENT_ID,
               "Content-Type": "application/json", "Accept": "application/json"}
        r = requests.post(f"{API}{path}", json=body, headers=hdr, timeout=30)
        status = r.status_code
        if r.ok:
            return r.json()
        txt = r.text[:300]
        if status in (401, 403) or "DH-901" in txt or "DH-906" in txt:   # token invalid / expired
            status = 401
            continue
        if status == 429 or "DH-904" in txt:                            # rate limit
            time.sleep(2)
            continue
        raise RuntimeError(f"Dhan {path} HTTP {status}: {txt}")
    raise RuntimeError(f"Dhan {path} failed after retries (HTTP {status})")


def _frame(j):
    """Dhan arrays -> DataFrame (IST index, High/Low/Close)."""
    ts = j.get("timestamp") or []
    if not ts:
        return pd.DataFrame(columns=["High", "Low", "Close"])
    idx = pd.to_datetime(pd.Series(ts, dtype="int64"), unit="s", utc=True).dt.tz_convert(IST)
    df = pd.DataFrame({"High": j["high"], "Low": j["low"], "Close": j["close"]}, index=pd.DatetimeIndex(idx))
    return df


def _intraday(sec_id, interval, start, end):
    j = _post("/charts/intraday", {
        "securityId": sec_id, "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": str(interval),
        "oi": False, "fromDate": start.strftime("%Y-%m-%d 09:00:00"), "toDate": end.strftime("%Y-%m-%d 16:00:00")})
    return _frame(j)


def _session(df):
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna().astype(float).round(2)
    return df[(df.index.time >= dtime(9, 15)) & (df.index.time < dtime(15, 30))]


IDS_FILE = os.path.join(os.path.dirname(TOKEN_FILE) or ".", "dhan_ids.json")
try:
    with open(IDS_FILE) as _f:
        IDS.update(json.load(_f))                      # ids found by an earlier lookup
except Exception:
    pass


def _lookup_id(name):
    """Find an index security id in Dhan's public scrip list (used only if the default id returns nothing)."""
    df = pd.read_csv(SCRIP_MASTER, usecols=lambda c: c in (
        "SEM_SEGMENT", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID", "SM_SYMBOL_NAME"),
        low_memory=False)
    m = df[(df["SEM_INSTRUMENT_NAME"].astype(str) == "INDEX") &
           (df["SEM_TRADING_SYMBOL"].astype(str).str.upper() == name.upper())]
    if m.empty:
        raise RuntimeError(f"{name} not found in Dhan scrip list")
    sid = str(int(m.iloc[0]["SEM_SMST_SECURITY_ID"]))
    print(f"Dhan: {name} security id = {sid}")
    try:
        os.makedirs(os.path.dirname(IDS_FILE) or ".", exist_ok=True)
        with open(IDS_FILE, "w") as f:
            json.dump({**IDS, name: sid}, f)               # remember it, so the big list is read only once
    except Exception:
        pass
    return sid


def fetch(name, now, want_hourly):
    """Returns (m5, d1, h1 or None) in the same shape as the Yahoo version."""
    sid = IDS[name]
    today = now.normalize().tz_localize(None)
    start5, end5 = today - pd.Timedelta(days=59), today + pd.Timedelta(days=1)
    try:
        m5 = _session(_intraday(sid, 5, start5, end5))
    except RuntimeError as e:
        if "HTTP 4" not in str(e):
            raise
        m5 = pd.DataFrame()
    if m5.empty:                                   # wrong id? look it up in Dhan's scrip list once
        sid = IDS[name] = _lookup_id(name)
        m5 = _session(_intraday(sid, 5, start5, end5))
    if m5.empty:
        raise RuntimeError("no 5m candles from Dhan")

    j = _post("/charts/historical", {
        "securityId": sid, "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False,
        "fromDate": "2000-01-01", "toDate": (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")})
    d = _frame(j)
    d1 = d[["High", "Low", "Close"]].astype(float).round(2)
    d1.index = pd.to_datetime(d1.index.date)                 # plain dates, like the Yahoo version
    d1 = d1[~d1.index.duplicated(keep="last")]
    if not d1.empty and d1.index[-1].date() == today.date() and now.time() < dtime(15, 30):
        d1 = d1.iloc[:-1]                                      # today's daily candle is not closed yet

    h1 = None
    if want_hourly:
        parts, end = [], today + pd.Timedelta(days=1)
        for _ in range(5):                                     # 5 x 85 days ~ 1 year of 60m candles
            start = end - pd.Timedelta(days=85)
            parts.append(_intraday(sid, 60, start, end))
            end = start
            time.sleep(0.3)
        h1 = _session(pd.concat(parts))
    return m5, d1, h1
