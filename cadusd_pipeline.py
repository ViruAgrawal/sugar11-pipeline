# === CAD/USD pipeline (DF-based, CI-safe, no scraping) ===
# Spot: Yahoo Finance
# Discount factors: FRED (USD) + Bank of Canada (CAD proxy)
# Forwards: Covered Interest Parity using DFs

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

import pandas as pd
import yfinance as yf
from pandas_datareader import data as pdr

# ---------------- Settings ----------------
HISTORY_DAYS = 1800
PAIR_YF      = "CADUSD=X"

# FRED series (robust, public)
USD_RATE_SERIES = "SOFR"     # Overnight secured USD
CAD_RATE_SERIES = "CORRA"    # Overnight CAD proxy (via BoC mirror)

TENORS = [
    "ON", "1W", "1M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "5Y", "10Y"
]

start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
end   = date.today().isoformat()

# ---------------- Tenor helpers ----------------
def tenor_to_reldelta(t: str) -> relativedelta:
    if t == "ON":
        return relativedelta(days=1)
    if t.endswith("W"):
        return relativedelta(weeks=int(t[:-1]))
    if t.endswith("M"):
        return relativedelta(months=int(t[:-1]))
    if t.endswith("Y"):
        return relativedelta(years=int(t[:-1]))
    raise ValueError(f"Unknown tenor: {t}")

def year_fraction(start_dt, end_dt) -> float:
    return (end_dt - start_dt).days / 365.0

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

# Spot USD/CAD for CIP
spot_usdcad = 1.0 / cadusd_hist.loc[cadusd_hist["Date"] == as_of, "CADUSD"].iloc[0]

# ---------------- Fetch rates (USD & CAD) ----------------
rates = pd.DataFrame()

usd = pdr.DataReader(USD_RATE_SERIES, "fred", start, end) / 100.0
cad = pdr.DataReader(CAD_RATE_SERIES, "fred", start, end) / 100.0

rates["USD"] = usd.iloc[:, 0]
rates["CAD"] = cad.iloc[:, 0]
rates = rates.dropna()
rates_asof = rates.loc[:as_of].iloc[-1]

r_usd = float(rates_asof["USD"])
r_cad = float(rates_asof["CAD"])

# ---------------- Build discount factors ----------------
rows = []

for ten in TENORS:
    mat_date = pd.Timestamp(as_of) + tenor_to_reldelta(ten)
    yf = year_fraction(pd.Timestamp(as_of), mat_date)

    df_usd = 1.0 / (1.0 + r_usd * yf)
    df_cad = 1.0 / (1.0 + r_cad * yf)

    fwd_usdcad = spot_usdcad * (df_usd / df_cad)
    fwd_cadusd = 1.0 / fwd_usdcad

    rows.append({
        "Tenor": ten,
        "Date": mat_date,
        "Forward_CADUSD": fwd_cadusd,
        "Forward_USDCAD": fwd_usdcad,
        "DF_USD": df_usd,
        "DF_CAD": df_cad,
        "Source": "CIP_DF"
    })

cadusd_forwards = pd.DataFrame(rows).sort_values("Date")

# ---------------- Export for Power BI ----------------
cadusd_spot = cadusd_hist[["Date", "CADUSD"]]

cadusd_meta = pd.DataFrame(
    [pd.Timestamp.now("UTC")]
)

cadusd_spot.to_csv("cadusd_spot.csv", index=False)
cadusd_forwards.to_csv("cadusd_forwards.csv", index=False)
cadusd_meta.to_csv("cadusd_meta.csv", index=False, header=False)

print("✅ CAD/USD DF-based forwards exported successfully")
