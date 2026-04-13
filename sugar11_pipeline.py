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

def parse_symbol(symbol: str):
    # SBH26.NYB → ("H", 2026)
    code = symbol[2]
    year = 2000 + int(symbol[3:5])
    return code, year

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
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()
    df.columns = [str(c).lower() for c in df.columns]

    if "date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "date"})

    return df[["date", "close", "high", "low", "volume"]].assign(symbol=symbol)

frames = [fetch_hist(sym) for sym in symbols_all]
frames = [f for f in frames if not f.empty]

if not frames:
    raise RuntimeError("No usable Yahoo Finance data returned.")

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

if contracts_df.empty:
    pb_forward_final = pd.DataFrame()
else:
    contracts_wide = contracts_df.pivot_table(
        index="date", columns="symbol", values="close"
    ).sort_index()

    counts = contracts_wide.notna().sum(axis=1)
    as_of = counts[counts >= 2].index.max()
    as_of_ts = pd.to_datetime(as_of)

    last_px_by_contract = (
        contracts_df[contracts_df["date"] <= as_of_ts]
        .sort_values(["symbol", "date"])
        .groupby("symbol")
        .tail(1)
        .set_index("symbol")
        .reset_index()
        .rename(columns={
            "symbol": "Symbol",
            "expiry": "Expiry",
            "close": "Close",
            "high": "High",
            "low": "Low",
            "volume": "Volume",
        })
    )

    # -------- Expand backwards to monthly rows --------
    def expand_backwards(row: pd.Series) -> pd.DataFrame:
        dates = pd.date_range(as_of_ts, row["Expiry"], freq="M")
        code, year = parse_symbol(row["Symbol"])
        return pd.DataFrame({
            "Date": dates,
            "Symbol": row["Symbol"],
            "Expiry": row["Expiry"],
            "Close": row["Close"],
            "High": row["High"],
            "Low": row["Low"],
            "Volume": row["Volume"],
            "MonthCode": code,
            "CropYear": year,
        })

    monthly_frames = [
        expand_backwards(row)
        for _, row in last_px_by_contract.iterrows()
    ]

    pb_forward_monthly = (
        pd.concat(monthly_frames, ignore_index=True)
        .sort_values(["Date", "Symbol"])
    )

    # -------- Quarter pricing --------
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
    pivot = pivot.drop(columns=["H_next"])

    pb_forward_final = pb_forward_monthly.merge(
        pivot[["Date", "Q1_Price", "Q2_Price", "Q3_Price", "Q4_Price"]],
        on="Date",
        how="left"
    )

# ---------------- Export ----------------
pb_continuous = cont_df.reset_index().rename(
    columns={"date": "Date", "close_cont": "Close"}
)

pb_meta = pd.DataFrame(
    {"AsOfUTC": [pd.Timestamp.utcnow()]}
)

pb_continuous.to_csv("sb_continuous.csv", index=False)
pb_forward_final.to_csv("sb_forward.csv", index=False)
pb_meta.to_csv("sb_meta.csv", index=False)

print("✅ CSV export complete")
