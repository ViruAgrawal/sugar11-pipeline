# === Sugar #11 timeline: historical SB=F + future-dated forward points ===

from datetime import date, timedelta
from calendar import monthrange
from typing import Optional

import pandas as pd
import yfinance as yf

# ---------------- Parameters ----------------
N_CONTRACTS = 12
HISTORY_DAYS = 1800

start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
end = date.today().isoformat()

# ---------------- Helpers ----------------
MONTH_MAP = {"H": 3, "K": 5, "N": 7, "V": 10}
CYCLE = [("H", 3), ("K", 5), ("N", 7), ("V", 10)]

def last_business_day(year: int, month: int) -> pd.Timestamp:
    last_day = monthrange(year, month)[1]
    dt = pd.Timestamp(year=year, month=month, day=last_day)
    while dt.weekday() >= 5:
        dt -= pd.Timedelta(days=1)
    return dt

def expiry_from_symbol(symbol: str) -> Optional[pd.Timestamp]:
    try:
        code = symbol[2]
        yy = int(symbol[3:5])
        year_full = 2000 + yy
        delivery_month = MONTH_MAP[code]
        exp_year, exp_month = year_full, delivery_month - 1
        if exp_month == 0:
            exp_month, exp_year = 12, year_full - 1
        return last_business_day(exp_year, exp_month)
    except Exception:
        return None

def next_contract_pairs(n: int, today: Optional[date] = None):
    today = today or date.today()
    out, yr = [], today.year
    start_idx = next(i for i, (_, m) in enumerate(CYCLE) if m >= today.month)
    i = start_idx
    while len(out) < n:
        code, m = CYCLE[i]
        if yr == today.year and m < today.month:
            yr += 1
        out.append((code, yr))
        i = (i + 1) % len(CYCLE)
        if i == 0:
            yr += 1
    return out

def to_yf_symbol(code: str, year_full: int) -> str:
    return f"SB{code}{year_full % 100:02d}.NYB"

# ---------------- Download ----------------
pairs = next_contract_pairs(N_CONTRACTS)
symbols_contracts = [to_yf_symbol(code, yr) for code, yr in pairs]
symbols_all = ["SB=F"] + symbols_contracts

def fetch_hist(symbol: str) -> pd.DataFrame:
    try:
        df = yf.download(
            symbol,
            start=start,
            end=end,
            progress=False,
            auto_adjust=False,
        )
    except Exception as e:
        print(f"[Skip] {symbol}: {type(e).__name__}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # Reset index first
    df = df.reset_index()

    # ✅ SAFE column normalization (this fixes the tuple issue permanently)
    def normalize_col(c):
        if isinstance(c, tuple):
            return c[0].lower()
        return str(c).lower()

    df.columns = [normalize_col(c) for c in df.columns]

    if "date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "date"})

    required = {"date", "close", "high", "low", "volume"}
    if not required.issubset(df.columns):
        print(f"[Skip] {symbol}: missing columns {required - set(df.columns)}")
        return pd.DataFrame()

    return (
        df[["date", "close", "high", "low", "volume"]]
        .assign(symbol=symbol)
    )

frames = [fetch_hist(sym) for sym in symbols_all]
frames = [f for f in frames if not f.empty]

if not frames:
    raise RuntimeError("No data returned for any symbol.")

all_df = pd.concat(frames, ignore_index=True)

# ---------------- Continuous SB=F ----------------
cont_df = (
    all_df.query("symbol == 'SB=F'")
    .assign(date=lambda d: pd.to_datetime(d["date"]))
    .set_index("date")[["close"]]
    .rename(columns={"close": "close_cont"})
    .sort_index()
)

# ---------------- Contracts ----------------
contracts_df = all_df.query("symbol != 'SB=F'").copy()
contracts_df["date"] = pd.to_datetime(contracts_df["date"], errors="coerce")
contracts_df = contracts_df.dropna(subset=["date"])
contracts_df["expiry"] = contracts_df["symbol"].apply(expiry_from_symbol)
contracts_df = contracts_df.dropna(subset=["expiry"])

# ---------------- Determine as_of ----------------
contracts_wide = contracts_df.pivot_table(
    index="date", columns="symbol", values="close"
).sort_index()

counts = contracts_wide.notna().sum(axis=1)
as_of = counts[counts >= 4].index.max() if (counts >= 4).any() else contracts_wide.index.max()
as_of_ts = pd.to_datetime(as_of)

# ---------------- Forward points ----------------
last_px_by_contract = (
    contracts_df[contracts_df["date"] <= as_of_ts]
    .sort_values(["symbol", "date"])
    .groupby("symbol")
    .apply(lambda g: g.assign(prev_close=g["close"].shift(1)).tail(1))
    .reset_index(drop=True)
    .set_index("symbol")
)

last_px_by_contract["change"] = (
    last_px_by_contract["close"] - last_px_by_contract["prev_close"]
)

forward_points = last_px_by_contract[["close", "expiry"]].copy()
forward_points = forward_points[
    forward_points["expiry"] >= as_of_ts.normalize()
].sort_values("expiry")

# ---------------- Export tables ----------------
pb_continuous = (
    cont_df.reset_index()
    .rename(columns={"date": "Date", "close_cont": "Close"})
)

pb_forward = (
    forward_points
    .join(last_px_by_contract[["high", "low", "volume", "change"]])
    .reset_index()
    .rename(columns={
        "symbol": "Symbol",
        "expiry": "Expiry",
        "close": "Close",
        "change": "Change",
        "high": "High",
        "low": "Low",
        "volume": "Volume",
    })
)

pb_meta = pd.DataFrame({"AsOfDate": [as_of_ts]})

pb_continuous.to_csv("sb_continuous.csv", index=False)
pb_forward.to_csv("sb_forward.csv", index=False)
pb_meta.to_csv("sb_meta.csv", index=False)

print("✅ CSV export complete")
