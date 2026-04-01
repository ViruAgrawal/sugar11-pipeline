# === CAD/USD pipeline: history (Yahoo) + forwards (FXEmpire / Investing) ===

from datetime import date, timedelta
from calendar import monthrange
from dateutil.relativedelta import relativedelta
from typing import Optional

import warnings
import re
import requests
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ---------------- Settings ----------------
HISTORY_DAYS = 1800
PAIR_YF      = "CADUSD=X"
PIP_FACTOR  = 10000.0
TIMEOUT     = 25

URL_FXEMPIRE    = "https://www.fxempire.com/currencies/usd-cad/forward-rates"
URL_INVESTINGCA = "https://ca.investing.com/currencies/usd-cad-forward-rates"

start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
end   = date.today().isoformat()

# ---------------- Tenor parsing ----------------
_WORDS = {
    "ONE":1,"TWO":2,"THREE":3,"FOUR":4,"FIVE":5,"SIX":6,
    "SEVEN":7,"EIGHT":8,"NINE":9,"TEN":10,"ELEVEN":11,
    "TWELVE":12,"FIFTEEN":15,"TWENTY ONE":21,"TWENTY-ONE":21,
    "THIRTY":30
}

def parse_tenor(label: str) -> Optional[str]:
    s = str(label).upper().strip()
    s = s.replace(" FORWARD","").replace(" FORWARDS","").replace(" FWD","")
    if re.search(r"\bON\b", s): return "ON"
    if re.search(r"\bTN\b", s): return "TN"
    if re.search(r"\bSN\b", s): return "SN"

    m = re.search(r"\b(\d+)\s*W", s)
    if m: return f"{int(m.group(1))}W"
    m = re.search(r"\b(\d+)\s*M", s)
    if m: return f"{int(m.group(1))}M"
    m = re.search(r"\b(\d+)\s*Y", s)
    if m: return f"{int(m.group(1))}Y"

    for w, n in _WORDS.items():
        if re.search(fr"\b{w}\s+WEEK", s): return f"{n}W"
        if re.search(fr"\b{w}\s+MONTH", s): return f"{n}M"
        if re.search(fr"\b{w}\s+YEAR", s): return f"{n}Y"
    return None

def tenor_to_reldelta(t: str) -> relativedelta:
    if t == "ON": return relativedelta(days=1)
    if t == "TN": return relativedelta(days=2)
    if t == "SN": return relativedelta(days=3)
    if t.endswith("W"): return relativedelta(weeks=int(t[:-1]))
    if t.endswith("M"): return relativedelta(months=int(t[:-1]))
    if t.endswith("Y"): return relativedelta(years=int(t[:-1]))
    return relativedelta(days=0)

# ---------------- Spot history ----------------
spot = yf.download(
    PAIR_YF,
    start=start,
    end=end,
    progress=False,
)

if spot.empty:
    raise RuntimeError("No CAD/USD spot data returned from Yahoo.")

close = spot["Close"]

# yfinance may return a DataFrame instead of Series
if isinstance(close, pd.DataFrame):
    close = close.iloc[:, 0]

cadusd_hist = close.rename("CADUSD")
cadusd_hist.index.name = "Date"
cadusd_hist = cadusd_hist.reset_index()

as_of = cadusd_hist["Date"].max()

# ---------------- HTML helpers ----------------
def get_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9"
    }
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def read_tables(html: str) -> list[pd.DataFrame]:
    try:
        return pd.read_html(html)
    except Exception:
        return []

# ---------------- FXEmpire scrape ----------------
def fetch_fxempire(as_of_date: pd.Timestamp) -> Optional[pd.DataFrame]:
    tables = read_tables(get_html(URL_FXEMPIRE))
    frames = []

    for t in tables:
        df = t.copy()
        df.columns = [str(c).lower() for c in df.columns]

        label = next((c for c in df.columns if c in ["tenor","expiration","name"]), None)
        bid   = next((c for c in df.columns if "bid" in c), None)
        ask   = next((c for c in df.columns if "ask" in c), None)
        mid   = next((c for c in df.columns if "mid" in c), None)

        if label is None or not (mid or (bid and ask)):
            continue

        df["tenor"] = df[label].map(parse_tenor)
        df = df.dropna(subset=["tenor"])

        if df.empty:
            continue

        if mid and mid in df:
            df["fwd_usdcad"] = pd.to_numeric(df[mid], errors="coerce")
        else:
            df["fwd_usdcad"] = (
                pd.to_numeric(df[bid], errors="coerce")
                + pd.to_numeric(df[ask], errors="coerce")
            ) / 2

        df = df.dropna(subset=["fwd_usdcad"])
        frames.append(df[["tenor","fwd_usdcad"]])

    if not frames:
        return None

    out = pd.concat(frames).drop_duplicates("tenor")
    out["Date"] = out["tenor"].map(lambda t: as_of_date + tenor_to_reldelta(t))
    out["USD_CAD_Forward"] = out["fwd_usdcad"]
    out["Source"] = "FXEmpire"
    return out[["tenor","Date","USD_CAD_Forward","Source"]]

# ---------------- Fetch forwards (with fallback) ----------------
df_fwd = fetch_fxempire(as_of)

if df_fwd is None or df_fwd.empty:
    print("⚠️ FXEmpire unavailable — no forward curve today")
    df_fwd = pd.DataFrame(
        columns=["tenor","Date","USD_CAD_Forward","Source"]
    )

# ---------------- Export for Power BI ----------------
pb_spot = cadusd_hist[["Date","CADUSD"]]

pb_meta = pd.DataFrame(
    [pd.Timestamp.now("UTC")]
)

pb_spot.to_csv("cadusd_spot.csv", index=False)
df_fwd.to_csv("cadusd_forwards.csv", index=False)
pb_meta.to_csv("cadusd_meta.csv", index=False, header=False)

print("✅ CADUSD CSV export complete")
