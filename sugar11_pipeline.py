# === Sugar #11: Monthly forward curve with stable quarterly pricing ===

from datetime import date, timedelta
from calendar import monthrange
from typing import Optional

import pandas as pd
import yfinance as yf

# ---------------- Parameters ----------------
HISTORY_DAYS = 1800
MAX_CONSECUTIVE_MISSES = 4   # how far Yahoo lists contracts

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

def quarter_from_month(m: int) -> str:
    return (
        "Q1" if m <= 3 else
        "Q2" if m <= 6 else
        "Q3" if m <= 9 else
        "Q4"
    )

def fetch_hist(symbol: str) -> pd.DataFrame:
    try:
        df = yf.download(
            symbol,
            start=start,
            end=end,
            progress=False,
            auto_adjust=False,
        )
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()

    def norm(c):
        if isinstance(c, tuple):
            c = c[0]
        return str(c).lower().replace(" ", "")

    df.columns = [norm(c) for c in df.columns]

    if not {"date", "close", "high", "low"}.issubset(df.columns):
        return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = pd.NA

    return df[["date", "close", "high", "low", "volume"]]

# ---------------- Auto-detect listed contracts ----------------
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
    raise RuntimeError("No usable Sugar #11 contracts available from Yahoo.")

contracts_df = pd.concat(frames.values(), ignore_index=True)
contracts_df["date"] = pd.to_datetime(contracts_df["date"])
contracts_df["expiry"] = contracts_df["symbol"].apply(expiry_from_symbol)

# ---------------- As-of snapshot ----------------
wide = contracts_df.pivot_table(
    index="date", columns="symbol", values="close"
).sort_index()

counts = wide.notna().sum(axis=1)
as_of_ts = counts[counts >= 2].index.max()

last_px = (
    contracts_df[contracts_df["date"] <= as_of_ts]
    .sort_values(["symbol", "date"])
    .groupby("symbol")
    .tail(1)
    .reset_index(drop=True)
)

# ---------------- Compute quarter prices ONCE ----------------
def get_px(code):
    return last_px.loc[
        last_px["symbol"].str[2] == code, "close"
    ].iloc[0]

Q_PRICES = {
    "Q1": (2/3) * get_px("H") + (1/3) * get_px("K"),
    "Q2": (1/3) * get_px("K") + (2/3) * get_px("N"),
    "Q3": get_px("V"),
    "Q4": (2/3) * get_px("V") + (1/3) * get_px("H"),
}

# ---------------- Build monthly output ----------------
rows = []

end_date = last_px["expiry"].max()
months = pd.date_range(as_of_ts.normalize(), end_date, freq="ME")

for d in months:
    # next-expiring contract as of this month
    front = (
        last_px[last_px["expiry"] >= d]
        .sort_values("expiry")
        .iloc[0]
    )

    q = quarter_from_month(d.month)

    rows.append({
        "Date": d,
        "Quarter": q,
        "QuarterPrice": Q_PRICES[q],
        "Symbol": front["symbol"],
        "Expiry": front["expiry"],
        "Close": front["close"],
        "High": front["high"],
        "Low": front["low"],
        "Volume": front["volume"],
    })

pb_forward = pd.DataFrame(rows)

# ---------------- Export ----------------
pb_forward.to_csv("sb_forward.csv", index=False)

pd.DataFrame({"AsOfUTC": [pd.Timestamp.utcnow()]}).to_csv(
    "sb_meta.csv", index=False
)

print("✅ Monthly forward curve with stable quarterly pricing exported")
