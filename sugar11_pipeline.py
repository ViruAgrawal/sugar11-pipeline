# === Sugar #11 timeline: historical SB=F + forward curve with auto-detected horizon ===

from datetime import date, timedelta
from calendar import monthrange
from typing import Optional

import pandas as pd
import yfinance as yf

# ---------------- Parameters ----------------
HISTORY_DAYS = 1800
MAX_CONSECUTIVE_MISSES = 4   # stop after Yahoo stops listing further contracts

start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
end = date.today().isoformat()

# ---------------- Helpers ----------------
MONTH_MAP = {"H": 3, "K": 5, "N": 7, "V": 10}
CYCLE = ["H", "K", "N", "V"]

def last_business_day(year: int, month: int) -> pd.Timestamp:
    last_day = monthrange(year, month)[1]
    d = pd.Timestamp(year=year, month=month, day=last_day)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d

def expiry_from_symbol(symbol: str) -> Optional[pd.Timestamp]:
    try:
        code = symbol[2]
        yy = int(symbol[3:5])
        year = 2000 + yy
        delivery_month = MONTH_MAP[code]
        exp_month = delivery_month - 1
        exp_year = year if exp_month > 0 else year - 1
        exp_month = exp_month if exp_month > 0 else 12
        return last_business_day(exp_year, exp_month)
    except Exception:
        return None

def parse_symbol(symbol: str):
    return symbol[2], 2000 + int(symbol[3:5])

def to_yf_symbol(code: str, year: int) -> str:
    return f"SB{code}{year % 100:02d}.NYB"

# ---------------- Robust Yahoo download ----------------
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

# ---------------- Detect furthest listed contract ----------------
today = date.today()
year = today.year
cycle_idx = CYCLE.index(next(c for c in CYCLE if MONTH_MAP[c] >= today.month))

contract_frames = {}
misses = 0

while misses < MAX_CONSECUTIVE_MISSES:
    code = CYCLE[cycle_idx]
    sym = to_yf_symbol(code, year)
    df = fetch_hist(sym)

    if df.empty:
        misses += 1
    else:
        df = df.assign(symbol=sym)
        contract_frames[sym] = df
        misses = 0

    cycle_idx = (cycle_idx + 1) % len(CYCLE)
    if cycle_idx == 0:
        year += 1

# ---------------- Fetch continuous SB=F ----------------
cont_df = fetch_hist("SB=F")
cont_df = cont_df.assign(symbol="SB=F")

if not contract_frames:
    raise RuntimeError("No forward Sugar contracts available from Yahoo.")

all_df = pd.concat([cont_df, *contract_frames.values()], ignore_index=True)

# ---------------- Continuous SB=F ----------------
pb_continuous = (
    all_df.query("symbol == 'SB=F'")
    .assign(Date=lambda d: pd.to_datetime(d["date"]))
    .rename(columns={"close": "Close"})
    [["Date", "Close"]]
)

# ---------------- Contracts ----------------
contracts_df = all_df.query("symbol != 'SB=F'").copy()
contracts_df["date"] = pd.to_datetime(contracts_df["date"])
contracts_df["expiry"] = contracts_df["symbol"].apply(expiry_from_symbol)
contracts_df = contracts_df.dropna(subset=["expiry"])

contracts_wide = contracts_df.pivot_table(
    index="date", columns="symbol", values="close"
).sort_index()

counts = contracts_wide.notna().sum(axis=1)
as_of_ts = counts[counts >= 2].index.max()

last_px = (
    contracts_df[contracts_df["date"] <= as_of_ts]
    .sort_values(["symbol", "date"])
    .groupby("symbol")
    .tail(1)
)

# ---------------- Monthly backward fill ----------------
rows = []

for _, r in last_px.iterrows():
    if r["expiry"] < as_of_ts:
        continue

    dates = pd.date_range(
        start=as_of_ts.normalize(),
        end=r["expiry"],
        freq="ME"
    )

    code, crop_year = parse_symbol(r["symbol"])

    for d in dates:
        rows.append({
            "Date": d,
            "Symbol": r["symbol"],
            "Expiry": r["expiry"],
            "Close": r["close"],
            "High": r["high"],
            "Low": r["low"],
            "Volume": r["volume"],
            "MonthCode": code,
            "CropYear": crop_year,
        })

pb_forward_monthly = pd.DataFrame(rows)

# ---------------- Quarter pricing ----------------
pivot = pb_forward_monthly.pivot_table(
    index=["Date", "CropYear"],
    columns="MonthCode",
    values="Close"
).reset_index()

pivot["Q1_Price"] = (2/3) * pivot["H"] + (1/3) * pivot["K"]
pivot["Q2_Price"] = (1/3) * pivot["K"] + (2/3) * pivot["N"]
pivot["Q3_Price"] = pivot["V"]

pivot["H_next"] = pivot.groupby("Date")["H"].shift(-1)
pivot["Q4_Price"] = (2/3) * pivot["V"] + (1/3) * pivot["H_next"]

pb_forward = pb_forward_monthly.merge(
    pivot[["Date", "Q1_Price", "Q2_Price", "Q3_Price", "Q4_Price"]],
    on="Date",
    how="left"
)

# ---------------- Export ----------------
pb_continuous.to_csv("sb_continuous.csv", index=False)
pb_forward.to_csv("sb_forward.csv", index=False)
pd.DataFrame({"AsOfUTC": [pd.Timestamp.utcnow()]}).to_csv(
    "sb_meta.csv", index=False
)

print("✅ CSV export complete")
