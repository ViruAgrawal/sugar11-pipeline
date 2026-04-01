# === CAD/USD pipeline: history (Yahoo) + forwards (Investing.com / FXEmpire) ===

from datetime import date, timedelta
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
PIP_FACTOR   = 10000.0
TIMEOUT      = 25

URL_INVESTINGCA = "https://ca.investing.com/currencies/usd-cad-forward-rates"
URL_FXEMPIRE    = "https://www.fxempire.com/currencies/usd-cad/forward-rates"

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
        if re.search(fr"\b{w}\s+WEEK", s):  return f"{n}W"
        if re.search(fr"\b{w}\s+MONTH", s): return f"{n}M"
        if re.search(fr"\b{w}\s+YEAR", s):  return f"{n}Y"

    return None

def tenor_to_reldelta(t: str) -> relativedelta:
    if t == "ON": return relativedelta(days=1)
    if t == "TN": return relativedelta(days=2)
    if t == "SN": return relativedelta(days=3)
    if t.endswith("W"): return relativedelta(weeks=int(t[:-1]))
    if t.endswith("M"): return relativedelta(months=int(t[:-1]))
    if t.endswith("Y"): return relativedelta(years=int(t[:-1]))
    return relativedelta(days=0)

# ---------------- Spot history (Yahoo) ----------------
spot = yf.download(
    PAIR_YF,
    start=start,
    end=end,
    progress=False,
)

if spot.empty:
    raise RuntimeError("No CAD/USD spot data returned from Yahoo.")

close = spot["Close"]
if isinstance(close, pd.DataFrame):
    close = close.iloc[:, 0]

cadusd_hist = close.rename("CADUSD")
cadusd_hist.index.name = "Date"
cadusd_hist = cadusd_hist.reset_index()

as_of = cadusd_hist["Date"].max()

# Spot in USDCAD terms for forward conversion
S_usdcad = 1.0 / cadusd_hist.loc[cadusd_hist["Date"] == as_of, "CADUSD"].iloc[0]

# ---------------- HTML helpers (Option 1: strong headers) ----------------
def get_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }

    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def read_tables(html: str) -> list[pd.DataFrame]:
    try:
        return pd.read_html(html)
    except Exception:
        return []

# ---------------- Investing.com scrape (POINTS → OUTRIGHT) ----------------
def fetch_investing(as_of_date: pd.Timestamp, spot_usdcad: float) -> Optional[pd.DataFrame]:
    tables = read_tables(get_html(URL_INVESTINGCA))
    frames = []

    for t in tables:
        df = t.copy()
        df.columns = [str(c).lower() for c in df.columns]

        label = next((c for c in df.columns if c in ["name","tenor","instrument","forward","description"]), None)
        bid   = next((c for c in df.columns if "bid" in c), None)
        ask   = next((c for c in df.columns if "ask" in c or "offer" in c), None)

        if not (label and bid and ask):
            continue

        df["tenor"] = df[label].map(parse_tenor)
        df = df.dropna(subset=["tenor"])
        if df.empty:
            continue

        bid_vals = pd.to_numeric(df[bid], errors="coerce")
        ask_vals = pd.to_numeric(df[ask], errors="coerce")
        mid_pts  = (bid_vals + ask_vals) / 2.0

        df = df[mid_pts.notna()]
        if df.empty:
            continue

        df["USD_CAD_Forward"] = spot_usdcad + (mid_pts / PIP_FACTOR)
        df["Date"] = df["tenor"].map(lambda t: as_of_date + tenor_to_reldelta(t))
        df["Source"] = "Investing.com"

        frames.append(df[["tenor","Date","USD_CAD_Forward","Source"]])

    if not frames:
        return None

    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("tenor")
        .sort_values("Date")
    )

# ---------------- FXEmpire scrape (OUTRIGHT) ----------------
def fetch_fxempire(as_of_date: pd.Timestamp) -> Optional[pd.DataFrame]:
    tables = read_tables(get_html(URL_FXEMPIRE))
    frames = []

    for t in tables:
        df = t.copy()
        df.columns = [str(c).lower() for c in df.columns]

        label = next((c for c in df.columns if c in ["tenor","expiration","name"]), None)
        mid   = next((c for c in df.columns if "mid" in c), None)
        bid   = next((c for c in df.columns if "bid" in c), None)
        ask   = next((c for c in df.columns if "ask" in c), None)

        if not label or not (mid or (bid and ask)):
            continue

        df["tenor"] = df[label].map(parse_tenor)
        df = df.dropna(subset=["tenor"])
        if df.empty:
            continue

        if mid and mid in df.columns:
            df["USD_CAD_Forward"] = pd.to_numeric(df[mid], errors="coerce")
        else:
            df["USD_CAD_Forward"] = (
                pd.to_numeric(df[bid], errors="coerce")
                + pd.to_numeric(df[ask], errors="coerce")
            ) / 2.0

        df = df.dropna(subset=["USD_CAD_Forward"])
        df["Date"] = df["tenor"].map(lambda t: as_of_date + tenor_to_reldelta(t))
        df["Source"] = "FXEmpire"

        frames.append(df[["tenor","Date","USD_CAD_Forward","Source"]])

    if not frames:
        return None

    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("tenor")
        .sort_values("Date")
    )

# ---------------- Fetch forwards (Option 2: Investing → FXEmpire) ----------------
df_fwd = fetch_investing(as_of, S_usdcad)

if df_fwd is None or df_fwd.empty:
    print("Investing.com unavailable — trying FXEmpire")
    df_fwd = fetch_fxempire(as_of)

if df_fwd is None or df_fwd.empty:
    print("⚠️ No forward curve available today")
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
