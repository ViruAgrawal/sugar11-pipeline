# === Sugar #11: monthly quarter price forward curve (one row per month) ===

from datetime import date, timedelta
from calendar import monthrange
from typing import Optional
import pandas as pd
import yfinance as yf

# ---------------- Parameters ----------------
HISTORY_DAYS = 1800
MAX_CONSECUTIVE_MISSES = 4

start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
end = date.today().isoformat()

# ---------------- Helpers ----------------
MONTH_MAP = {"H": 3, "K": 5, "N": 7, "V": 10}
CYCLE = ["H", "K", "N", "V"]

def last_business_day(year: int, month: int) -> pd.Timestamp:
    last = monthrange(year, month)[1]
    d = pd.Timestamp(year=year, month=month, day=last)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d

def expiry_from_symbol(symbol: str) -> Optional[pd.Timestamp]:
    try:
        code = symbol[2]
        year = 2000 + int(symbol[3:5])
        delivery = MONTH_MAP[code]
        exp_month = delivery - 1 or 12
        exp_year = year if delivery > 1 else year - 1
        return last_business_day(exp_year, exp_month)
    except Exception:
        return None

def to_yf_symbol(code: str, year: int) -> str:
    return f"SB{code}{year % 100:02d}.NYB"

def fetch_hist(symbol: str) -> pd.DataFrame:
    try:
        df = yf.download(symbol, start=start, end=end, progress=False)
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()

    def norm(c):
        if isinstance(c, tuple):
            c = c[0]
        return str(c).lower()

    df.columns = [norm(c) for c in df.columns]

    for c in ["close", "high", "low"]:
        if c not in df.columns:
            return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = pd.NA

    return df[["date", "close", "high", "low", "volume"]]

# ---------------- Auto-detect contracts ----------------
today = date.today()
year = today.year
start_code = next(c for c in CYCLE if MONTH_MAP[c] >= today.month)
idx = CYCLE.index(start_code)

frames = {}
misses = 0

while misses < MAX_CONSECUTIVE_MISSES:
    code = CYCLE[idx]
    sym = to_yf_symbol(code, year)
    df = fetch_hist(sym)

    if df.empty:
        misses += 1
    else:
        df["symbol"] = sym
        frames[sym] = df
        misses = 0

    idx = (idx + 1) % len(CYCLE)
    if idx == 0:
        year += 1

if not frames:
    raise RuntimeError("No usable Sugar contracts found.")

contracts_df = pd.concat(frames.values(), ignore_index=True)
contracts_df["date"] = pd.to_datetime(contracts_df["date"])
contracts_df["expiry"] = contracts_df["symbol"].apply(expiry_from_symbol)

# ---------------- As-of snapshot ----------------
wide = contracts_df.pivot_table(index="date", columns="symbol", values="close")
as_of = wide.notna().sum(axis=1).ge(2)
as_of_ts = as_of[as_of].index.max()

last_px = (
    contracts_df[contracts_df["date"] <= as_of_ts]
    .sort_values(["symbol", "date"])
    .groupby("symbol")
    .tail(1)
)

# ---------------- Monthly forward grid ----------------
rows = []

for _, r in last_px.iterrows():
    dates = pd.date_range(as_of_ts, r["expiry"], freq="ME")
    code = r["symbol"][2]

    for d in dates:
        rows.append({
            "Date": d,
            "MonthCode": code,
            "Close": r["close"],
        })

curve = pd.DataFrame(rows)

# ---------------- Pivot & quarter math ----------------
px = curve.pivot_table(index="Date", columns="MonthCode", values="Close")

px["Q1_Price"] = (2/3) * px["H"] + (1/3) * px["K"]
px["Q2_Price"] = (1/3) * px["K"] + (2/3) * px["N"]
px["Q3_Price"] = px["V"]
px["Q4_Price"] = (2/3) * px["V"] + (1/3) * px["H"].shift(-1)

px = px.reset_index()

def quarter_from_month(m):
    return (
        "Q1" if m <= 3 else
        "Q2" if m <= 6 else
        "Q3" if m <= 9 else
        "Q4"
    )

px["Quarter"] = px["Date"].dt.month.map(quarter_from_month)

px["QuarterPrice"] = px.apply(
    lambda r: r[f"{r['Quarter']}_Price"], axis=1
)

pb_forward = px[["Date", "Quarter", "QuarterPrice"]].dropna()

# ---------------- Export ----------------
pb_forward.to_csv("sb_forward.csv", index=False)

pd.DataFrame({"AsOfUTC": [pd.Timestamp.utcnow()]}).to_csv(
    "sb_meta.csv", index=False
)

print("✅ Monthly quarter forward curve exported")
