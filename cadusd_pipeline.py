# === CAD/USD pipeline (DF-based, CI-safe, NO scraping) ===
# Spot: Yahoo Finance
# USD DF: FRED (SOFR CSV)
# CAD DF: Bank of Canada Valet (CORRA JSON)

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

import requests
import pandas as pd
import yfinance as yf

# ---------------- Settings ----------------
HISTORY_DAYS = 1800
PAIR_YF      = "CADUSD=X"

USD_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR"
CAD_BOC_JSON = "https://www.bankofcanada.ca/valet/observations/AVG.INTWO"

TENORS = ["ON", "1W", "1M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "5Y", "10Y"]

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
spot = yf.download(PAIR_YF, start=start, end=end, progress=False)

if spot.empty:
    raise RuntimeError("No CAD/USD spot data returned from Yahoo.")

close = spot["Close"]
if isinstance(close, pd.DataFrame):
    close = close.iloc[:, 0]

cadusd_spot = close.rename("CADUSD")
cadusd_spot.index.name = "Date"
cadusd_spot = cadusd_spot.reset_index()

as_of = cadusd_spot["Date"].max()

# USD/CAD spot for CIP
spot_usdcad = 1.0 / cadusd_spot.loc[
    cadusd_spot["Date"] == as_of, "CADUSD"
].iloc[0]

# ---------------- USD rates (SOFR via FRED CSV) ----------------
usd_raw = pd.read_csv(USD_FRED_CSV)
usd_raw.columns = [c.lower() for c in usd_raw.columns]

usd_date_col = next(c for c in usd_raw.columns if "date" in c)
usd_rate_col = next(c for c in usd_raw.columns if c != usd_date_col)

usd_rates = (
    usd_raw
    .assign(date=lambda d: pd.to_datetime(d[usd_date_col]))
    .set_index("date")[usd_rate_col]
    .astype(float) / 100.0
)

# ---------------- CAD rates (CORRA via BoC Valet JSON) ----------------

resp = requests.get(CAD_BOC_JSON, timeout=20)
resp.raise_for_status()

data = resp.json()["observations"]

cad_rates = pd.Series(
    {
        obs["d"]: float(obs["AVG.INTWO"]["v"]) / 100.0
        for obs in data
        if "AVG.INTWO" in obs and "v" in obs["AVG.INTWO"]
    }
)

cad_rates.index = pd.to_datetime(cad_rates.index)
cad_rates = cad_rates.sort_index()

# ---------------- Align rates ----------------
rates = pd.concat(
    [usd_rates.rename("USD"), cad_rates.rename("CAD")],
    axis=1
).dropna()

rates_asof = rates.loc[:as_of].iloc[-1]
r_usd = float(rates_asof["USD"])
r_cad = float(rates_asof["CAD"])

# ---------------- Build DF-based FX forwards ----------------
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
cadusd_spot.to_csv("cadusd_spot.csv", index=False)
cadusd_forwards.to_csv("cadusd_forwards.csv", index=False)

pd.DataFrame([pd.Timestamp.now("UTC")]).to_csv(
    "cadusd_meta.csv",
    index=False,
    header=False
)

print("✅ CAD/USD DF-based forward curve exported successfully")
