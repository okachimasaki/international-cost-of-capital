"""
International Cost of Capital App
=================================
Compares a project's cost of equity, cost of debt and WACC in USD and in the
currency of a selected country.

Data sources (fetched live, cached 12 h, refresh button in the sidebar)
  * Unlevered industry betas (U.S.) ... Damodaran, betas.xls
  * BBB (Baa2) USD default spread ..... Damodaran, ratings.html
                                        (large non-financial service firms)
  * Statutory corporate tax rates ..... OECD Corporate Tax Statistics
                                        (combined statutory CIT rate, SDMX API)
  * 10-year government bond yields .... Trading Economics, /bonds page

Formulas (all rates as decimals)
  D/E                = d / (1 - d)                      d = target debt share D/V
  beta_L             = beta_U * [1 + (1 - t) * D/E]      Hamada relevering
  k_E(USD)           = y_US + beta_L * ERP               CAPM in USD
  k_D(USD)           = y_US + s_BBB                      pre-tax
  WACC(USD)          = (1 - d) * k_E(USD) + d * k_D(USD) * (1 - t)

  Conversion to the foreign currency (FC) uses the ratio of 10-year yields
  (international Fisher / interest-parity logic):
  k(FC)              = (1 + k(USD)) * (1 + y_FC) / (1 + y_US) - 1
  k_E(FC)            = conversion of k_E(USD)
  k_D(FC)            = y_FC + s_FC
     s_FC (auto)     = conversion of k_D(USD)  -  y_FC
     s_FC (override) = user input  (does not touch any USD number)
  WACC(FC)           = (1 - d) * k_E(FC) + d * k_D(FC) * (1 - t)

  The same project tax rate t (U.S. or selected country), debt share d and
  ERP are used for both currencies.  An "additive" approximation
  k(FC) = k(USD) + (y_FC - y_US) is also available as an option.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
URL_BETAS = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
URL_BETAS_HTML = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"
URL_RATINGS = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
URL_OECD_PAGE = (
    "https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/"
    "full-report/statutory-corporate-income-tax-rates_440f9286.html"
    "#chapter-d1e2342-08e74a33fb"
)
URL_OECD_API = [
    "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,/all"
    "?startPeriod=2018&dimensionAtObservation=AllDimensions&format=csvfilewithlabels",
    "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,"
    "?startPeriod=2018&format=csvfilewithlabels",
]
URL_TE = "https://tradingeconomics.com/bonds"

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "United States"
DEFAULT_ERP = 6.5      # %
DEFAULT_DEBT = 30.0    # %
TIMEOUT = 30
CACHE_TTL = 12 * 3600

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Country name (as shown on Trading Economics) -> (ISO3, currency code)
COUNTRY_INFO: dict[str, tuple[str, str]] = {
    "United States": ("USA", "USD"), "United Kingdom": ("GBR", "GBP"),
    "Japan": ("JPN", "JPY"), "Australia": ("AUS", "AUD"),
    "Germany": ("DEU", "EUR"), "Brazil": ("BRA", "BRL"),
    "Russia": ("RUS", "RUB"), "India": ("IND", "INR"),
    "Canada": ("CAN", "CAD"), "Italy": ("ITA", "EUR"),
    "France": ("FRA", "EUR"), "South Africa": ("ZAF", "ZAR"),
    "China": ("CHN", "CNY"), "Switzerland": ("CHE", "CHF"),
    "Mexico": ("MEX", "MXN"), "Netherlands": ("NLD", "EUR"),
    "New Zealand": ("NZL", "NZD"), "Portugal": ("PRT", "EUR"),
    "South Korea": ("KOR", "KRW"), "Spain": ("ESP", "EUR"),
    "Greece": ("GRC", "EUR"), "Turkey": ("TUR", "TRY"),
    "Austria": ("AUT", "EUR"), "Belgium": ("BEL", "EUR"),
    "Bulgaria": ("BGR", "BGN"), "Croatia": ("HRV", "EUR"),
    "Cyprus": ("CYP", "EUR"), "Czech Republic": ("CZE", "CZK"),
    "Denmark": ("DNK", "DKK"), "Finland": ("FIN", "EUR"),
    "Hungary": ("HUN", "HUF"), "Iceland": ("ISL", "ISK"),
    "Ireland": ("IRL", "EUR"), "Latvia": ("LVA", "EUR"),
    "Lithuania": ("LTU", "EUR"), "Luxembourg": ("LUX", "EUR"),
    "Malta": ("MLT", "EUR"), "Norway": ("NOR", "NOK"),
    "Poland": ("POL", "PLN"), "Romania": ("ROU", "RON"),
    "Serbia": ("SRB", "RSD"), "Slovakia": ("SVK", "EUR"),
    "Slovenia": ("SVN", "EUR"), "Sweden": ("SWE", "SEK"),
    "Ukraine": ("UKR", "UAH"), "Euro Area": ("EA", "EUR"),
    "Argentina": ("ARG", "ARS"), "Chile": ("CHL", "CLP"),
    "Colombia": ("COL", "COP"), "Peru": ("PER", "PEN"),
    "Bangladesh": ("BGD", "BDT"), "Hong Kong": ("HKG", "HKD"),
    "Indonesia": ("IDN", "IDR"), "Israel": ("ISR", "ILS"),
    "Jordan": ("JOR", "JOD"), "Kazakhstan": ("KAZ", "KZT"),
    "Malaysia": ("MYS", "MYR"), "Pakistan": ("PAK", "PKR"),
    "Philippines": ("PHL", "PHP"), "Qatar": ("QAT", "QAR"),
    "Singapore": ("SGP", "SGD"), "Sri Lanka": ("LKA", "LKR"),
    "Taiwan": ("TWN", "TWD"), "Thailand": ("THA", "THB"),
    "Vietnam": ("VNM", "VND"), "Bahrain": ("BHR", "BHD"),
    "Botswana": ("BWA", "BWP"), "Egypt": ("EGY", "EGP"),
    "Kenya": ("KEN", "KES"), "Mauritius": ("MUS", "MUR"),
    "Morocco": ("MAR", "MAD"), "Namibia": ("NAM", "NAD"),
    "Nigeria": ("NGA", "NGN"), "Uganda": ("UGA", "UGX"),
    "Zambia": ("ZMB", "ZMW"), "Saudi Arabia": ("SAU", "SAR"),
    "United Arab Emirates": ("ARE", "AED"), "Costa Rica": ("CRI", "CRC"),
    "Venezuela": ("VEN", "VES"), "Uruguay": ("URY", "UYU"),
    "Bolivia": ("BOL", "BOB"), "Ecuador": ("ECU", "USD"),
    "Dominican Republic": ("DOM", "DOP"), "Jamaica": ("JAM", "JMD"),
    "Panama": ("PAN", "PAB"), "Ghana": ("GHA", "GHS"),
    "Tanzania": ("TZA", "TZS"), "Rwanda": ("RWA", "RWF"),
    "Mongolia": ("MNG", "MNT"), "Nepal": ("NPL", "NPR"),
    "Bosnia and Herzegovina": ("BIH", "BAM"), "Montenegro": ("MNE", "EUR"),
    "North Macedonia": ("MKD", "MKD"), "Albania": ("ALB", "ALL"),
}

# Built-in fallbacks, used ONLY if the live request fails (clearly flagged in the UI)
FALLBACK_TAX = {  # OECD combined statutory CIT rate, %, approx. 2025 values
    "USA": 25.6, "GBR": 25.0, "JPN": 29.7, "AUS": 30.0, "DEU": 29.9, "BRA": 34.0,
    "RUS": 25.0, "IND": 25.2, "CAN": 26.1, "ITA": 27.8, "FRA": 36.1, "ZAF": 27.0,
    "CHN": 25.0, "CHE": 19.6, "MEX": 30.0, "NLD": 25.8, "NZL": 28.0, "PRT": 30.5,
    "KOR": 26.4, "ESP": 25.0, "GRC": 22.0, "TUR": 25.0,
}
FALLBACK_YIELDS = {  # Trading Economics 10Y yield, %, snapshot Nov 12, 2026
    "United States": 5.1090, "United Kingdom": 5.2816, "Japan": 2.7930,
    "Australia": 5.0650, "Germany": 3.3573, "Brazil": 15.3750, "Russia": 16.7300,
    "India": 7.2480, "Canada": 3.7870, "Italy": 4.4001, "France": 4.8360,
    "South Africa": 8.6400, "China": 1.6078, "Switzerland": 0.5534, "Mexico": 9.2580,
}
FALLBACK_YIELD_DATE = "2026-11-12 (built-in snapshot)"
FALLBACK_SPREAD = 1.11  # % Baa2/BBB, Damodaran January 2026


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------
def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _to_float(x) -> float | None:
    """Parse '1.11%', '25.6', 0.256 etc.  Returns None if not numeric."""
    if x is None:
        return None
    if isinstance(x, (int, float, np.floating)):
        return None if pd.isna(x) else float(x)
    s = str(x).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def iso3_of(country: str) -> str | None:
    info = COUNTRY_INFO.get(country)
    return info[0] if info else None


def ccy_of(country: str) -> str:
    info = COUNTRY_INFO.get(country)
    return info[1] if info else "LCY"


# --------------------------------------------------------------------------
# 1) Damodaran industry betas
# --------------------------------------------------------------------------
def parse_betas_frame(raw: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """Find the 'Industry Name' header row and return Industry / Unlevered beta."""
    date_str = None
    header_row = None
    for i in range(min(len(raw), 40)):
        row = [str(v).strip() for v in raw.iloc[i].tolist()]
        low = [v.lower() for v in row]
        if any("date updated" in v for v in low) and date_str is None:
            j = next(k for k, v in enumerate(low) if "date updated" in v)
            # value is either in the same cell after ':' or in a cell to the right
            cell = row[j]
            after = cell.split(":", 1)[1].strip() if ":" in cell else ""
            if after:
                date_str = after
            else:
                for v in raw.iloc[i].tolist()[j + 1:]:
                    if pd.notna(v) and str(v).strip():
                        date_str = v
                        break
        if "industry name" in low:
            header_row = i
            break
    if header_row is None:
        raise ValueError("Could not find the 'Industry Name' header row")

    df = raw.iloc[header_row + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_row].tolist()]
    cols = {c.lower(): c for c in df.columns}
    ind_col = cols["industry name"]
    beta_col = cols.get("unlevered beta")
    if beta_col is None:  # be tolerant to small header changes
        beta_col = next(c for c in df.columns
                        if c.lower().startswith("unlevered beta") and "cash" not in c.lower())
    out = pd.DataFrame({
        "Industry": df[ind_col].astype(str).str.strip(),
        "Unlevered beta": pd.to_numeric(df[beta_col], errors="coerce"),
    })
    nfirms_col = cols.get("number of firms")
    if nfirms_col:
        out["Number of firms"] = pd.to_numeric(df[nfirms_col], errors="coerce").values
    out = out[(out["Industry"] != "") & (out["Industry"].str.lower() != "nan")]
    out = out.dropna(subset=["Unlevered beta"]).reset_index(drop=True)

    if isinstance(date_str, (datetime, pd.Timestamp)):
        date_str = pd.Timestamp(date_str).strftime("%Y-%m-%d")
    elif date_str is not None:
        try:  # Excel serial date
            f = float(date_str)
            date_str = (pd.Timestamp("1899-12-30") + pd.Timedelta(days=f)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            date_str = str(date_str)
    return out, date_str


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_betas() -> dict:
    errors = []
    try:
        content = _get(URL_BETAS).content
        raw = None
        for engine in ("xlrd", "openpyxl", None):
            try:
                xls = pd.ExcelFile(io.BytesIO(content), engine=engine)
                sheet = next((s for s in xls.sheet_names if "industry" in s.lower()),
                             xls.sheet_names[0])
                raw = pd.read_excel(xls, sheet_name=sheet, header=None)
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{engine}: {e}")
        if raw is None:
            raise ValueError("; ".join(errors))
        df, date_str = parse_betas_frame(raw)
        return {"ok": True, "data": df, "date": date_str or "see file",
                "source": URL_BETAS, "fetched": _now(), "note": ""}
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    # Secondary: Damodaran's HTML version of the same table
    try:
        tables = pd.read_html(io.StringIO(_get(URL_BETAS_HTML).text), header=None)
        raw = max(tables, key=len)
        df, date_str = parse_betas_frame(raw)
        return {"ok": True, "data": df, "date": date_str or "see page",
                "source": URL_BETAS_HTML, "fetched": _now(),
                "note": "betas.xls could not be read; HTML version used."}
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    return {"ok": False, "data": pd.DataFrame(columns=["Industry", "Unlevered beta"]),
            "date": None, "source": URL_BETAS, "fetched": _now(),
            "note": " | ".join(errors)[:500]}


# --------------------------------------------------------------------------
# 2) Damodaran BBB spread
# --------------------------------------------------------------------------
def parse_bbb_spread(html: str) -> tuple[float, str | None]:
    """Return the Baa2/BBB spread (in %) for large non-financial firms (first table/first match)."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m_date = re.search(r"as of\s+([A-Za-z]+\s+\d{4})", text)
    date_str = m_date.group(1) if m_date else None
    # The non-financial block comes first on the page; within one row the
    # non-financial 'Baa2/BBB' cell is followed by its spread.
    soup = BeautifulSoup(html, "html.parser")
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        # only the FIRST rating cell of each row belongs to the non-financial block
        k = next((i for i, c in enumerate(cells)
                  if re.fullmatch(r"[A-Za-z0-9]+/[A-Z+\-]+", c.replace(" ", ""))), None)
        if k is not None and cells[k].replace(" ", "").lower() == "baa2/bbb":
            for v in cells[k + 1:]:
                f = _to_float(v)
                if f is not None:
                    return (f * 100 if f < 0.5 else f), date_str
    m = re.search(r"Baa2/BBB\s*\|?\s*([0-9.]+)\s*%", text)
    if m:
        return float(m.group(1)), date_str
    raise ValueError("Baa2/BBB row not found")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_spread() -> dict:
    try:
        val, date_str = parse_bbb_spread(_get(URL_RATINGS).text)
        return {"ok": True, "value": val, "date": date_str or "see page",
                "source": URL_RATINGS, "fetched": _now(), "note": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "value": FALLBACK_SPREAD, "date": "January 2026 (built-in)",
                "source": URL_RATINGS, "fetched": _now(), "note": str(e)[:300]}


# --------------------------------------------------------------------------
# 3) OECD statutory corporate tax rates
# --------------------------------------------------------------------------
def parse_oecd_csv(text: str) -> pd.DataFrame:
    """Latest combined statutory CIT rate per ISO3 from an OECD SDMX CSV."""
    df = pd.read_csv(io.StringIO(text), low_memory=False)
    area = next(c for c in df.columns if c.upper() in ("REF_AREA", "COU"))
    area_lbl = next((c for c in df.columns if c.lower() in ("reference area", "country")), None)
    time_col = next(c for c in df.columns if c.upper() in ("TIME_PERIOD", "TIME", "YEAR"))
    val_col = next(c for c in df.columns if c.upper() in ("OBS_VALUE", "VALUE"))
    # Keep only "combined corporate income tax rate" rows, whatever the dimension is called
    mask = pd.Series(False, index=df.index)
    for c in df.columns:
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
            s = df[c].astype(str)
            mask |= s.str.upper().str.contains("COMB") & s.str.upper().str.contains("CIT")
            mask |= s.str.lower().str.contains("combined corporate income tax rate")
    # drop targeted / small-business / reduced rates, keep the standard statutory rate
    excl = pd.Series(False, index=df.index)
    for c in df.columns:
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
            excl |= df[c].astype(str).str.lower().str.contains("target|small|reduced|sbr")
    sub = df[mask & ~excl].copy()
    if sub.empty:
        raise ValueError("No combined CIT rows in OECD response")
    sub[val_col] = pd.to_numeric(sub[val_col], errors="coerce")
    sub = sub.dropna(subset=[val_col])
    sub["_year"] = pd.to_numeric(sub[time_col].astype(str).str[:4], errors="coerce")
    sub = sub.sort_values("_year").groupby(area).tail(1)
    out = pd.DataFrame({
        "ISO3": sub[area].astype(str),
        "Country (OECD)": sub[area_lbl].astype(str) if area_lbl else sub[area].astype(str),
        "Tax rate (%)": sub[val_col].astype(float),
        "Year": sub["_year"].astype(int),
    })
    return out.reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_tax() -> dict:
    errors = []
    for url in URL_OECD_API:
        try:
            r = requests.get(url, headers={**HEADERS, "Accept": "application/vnd.sdmx.data+csv"},
                             timeout=60)
            r.raise_for_status()
            df = parse_oecd_csv(r.text)
            yr = int(df["Year"].max())
            return {"ok": True, "data": df, "date": f"tax year {yr} (latest available)",
                    "source": URL_OECD_PAGE, "api": url, "fetched": _now(), "note": ""}
        except Exception as e:  # noqa: BLE001
            errors.append(str(e)[:200])
    fb = pd.DataFrame({"ISO3": list(FALLBACK_TAX), "Tax rate (%)": list(FALLBACK_TAX.values())})
    fb["Country (OECD)"] = fb["ISO3"]
    fb["Year"] = 2025
    return {"ok": False, "data": fb, "date": "2025 (built-in snapshot)",
            "source": URL_OECD_PAGE, "api": URL_OECD_API[0], "fetched": _now(),
            "note": " | ".join(errors)}


# --------------------------------------------------------------------------
# 4) Trading Economics 10-year yields
# --------------------------------------------------------------------------
def _te_date(s: str, today: datetime) -> str:
    """'Nov/12' -> '2026-11-12' (year inferred);  '14:05' -> today."""
    s = s.strip()
    try:
        d = datetime.strptime(f"{s}/{today.year}", "%b/%d/%Y")
        if d.date() > today.date():
            d = d.replace(year=today.year - 1)
        return d.strftime("%Y-%m-%d")
    except ValueError:
        pass
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)?", s, flags=re.I):
        return today.strftime("%Y-%m-%d")
    return s


def parse_te_bonds(html: str, today: datetime | None = None) -> pd.DataFrame:
    today = today or datetime.now(timezone.utc)
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        name_idx = None
        for i, td in enumerate(tds):
            a = td.find("a", href=True)
            if a and ("bond-yield" in a["href"] or "government-bond" in a["href"]):
                name_idx = i
                break
        if name_idx is None or name_idx + 1 >= len(tds):
            continue
        name = tds[name_idx].get_text(" ", strip=True)
        name = re.sub(r"\s*\d+Y$", "", name).strip()
        y = _to_float(tds[name_idx + 1].get_text(strip=True))
        if y is None:
            continue
        date_txt = tds[-1].get_text(strip=True)
        rows.append({"Country": name, "10Y yield (%)": y, "Date": _te_date(date_txt, today)})
    if not rows:
        raise ValueError("No bond rows found on Trading Economics page")
    df = pd.DataFrame(rows).drop_duplicates("Country", keep="first")
    return df.sort_values("Country").reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_yields() -> dict:
    try:
        df = parse_te_bonds(_get(URL_TE).text)
        return {"ok": True, "data": df, "source": URL_TE, "fetched": _now(), "note": ""}
    except Exception as e:  # noqa: BLE001
        df = pd.DataFrame({"Country": list(FALLBACK_YIELDS),
                           "10Y yield (%)": list(FALLBACK_YIELDS.values()),
                           "Date": FALLBACK_YIELD_DATE})
        return {"ok": False, "data": df.sort_values("Country").reset_index(drop=True),
                "source": URL_TE, "fetched": _now(), "note": str(e)[:300]}


# --------------------------------------------------------------------------
# Core calculation (pure function: easy to verify)
# --------------------------------------------------------------------------
def convert_rate(k_usd: float, y_us: float, y_fc: float, method: str) -> float:
    if method == "additive":
        return k_usd + (y_fc - y_us)
    return (1 + k_usd) * (1 + y_fc) / (1 + y_us) - 1


def cost_of_capital(beta_u: float, tax: float, debt_share: float, erp: float,
                    y_us: float, y_fc: float, spread_usd: float,
                    spread_fc_override: float | None = None,
                    method: str = "multiplicative") -> dict:
    """All inputs/outputs are decimals (0.065 = 6.5%)."""
    d = debt_share
    de = d / (1 - d)
    beta_l = beta_u * (1 + (1 - tax) * de)

    ke_usd = y_us + beta_l * erp
    kd_usd = y_us + spread_usd
    kd_usd_at = kd_usd * (1 - tax)
    wacc_usd = (1 - d) * ke_usd + d * kd_usd_at

    ke_fc = convert_rate(ke_usd, y_us, y_fc, method)
    spread_fc_auto = convert_rate(kd_usd, y_us, y_fc, method) - y_fc
    spread_fc = spread_fc_auto if spread_fc_override is None else spread_fc_override
    kd_fc = y_fc + spread_fc
    kd_fc_at = kd_fc * (1 - tax)
    wacc_fc = (1 - d) * ke_fc + d * kd_fc_at

    return {
        "D/E": de, "Levered beta": beta_l,
        "USD": {"Risk-free (10Y)": y_us, "Equity premium (beta_L x ERP)": beta_l * erp,
                "Cost of equity": ke_usd, "Debt spread": spread_usd,
                "Pre-tax cost of debt": kd_usd, "After-tax cost of debt": kd_usd_at,
                "WACC": wacc_usd},
        "FC": {"Risk-free (10Y)": y_fc, "Equity premium (beta_L x ERP)": ke_fc - y_fc,
               "Cost of equity": ke_fc, "Debt spread": spread_fc,
               "Pre-tax cost of debt": kd_fc, "After-tax cost of debt": kd_fc_at,
               "WACC": wacc_fc},
        "spread_fc_auto": spread_fc_auto,
    }


# --------------------------------------------------------------------------
# Excel export with an independent formula-based check
# --------------------------------------------------------------------------
def build_excel(inputs: pd.DataFrame, summary: pd.DataFrame, all_ind: pd.DataFrame,
                p: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.utils.dataframe import dataframe_to_rows

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    for r in dataframe_to_rows(summary, index=False, header=True):
        ws.append(r)
    ws2 = wb.create_sheet("Inputs & sources")
    for r in dataframe_to_rows(inputs, index=False, header=True):
        ws2.append(r)
    ws3 = wb.create_sheet("All industries")
    for r in dataframe_to_rows(all_ind, index=False, header=True):
        ws3.append(r)

    # Independent check with live Excel formulas
    wc = wb.create_sheet("Excel check")
    rows = [
        ("Input", "Value", "Note"),
        ("Unlevered beta", p["beta_u"], ""),
        ("Tax rate", p["tax"], ""),
        ("Debt share D/V", p["d"], ""),
        ("ERP", p["erp"], ""),
        ("US 10Y yield", p["y_us"], ""),
        ("Foreign 10Y yield", p["y_fc"], ""),
        ("USD BBB spread", p["s_usd"], ""),
        ("FC spread override (blank = auto)", p["s_fc_ovr"], ""),
        ("", "", ""),
        ("Output", "Excel formula", "App value"),
        ("D/E", "=B4/(1-B4)", p["out"]["D/E"]),
        ("Levered beta", "=B2*(1+(1-B3)*B12)", p["out"]["Levered beta"]),
        ("k_E USD", "=B6+B13*B5", p["out"]["USD"]["Cost of equity"]),
        ("k_D USD pre-tax", "=B6+B8", p["out"]["USD"]["Pre-tax cost of debt"]),
        ("WACC USD", "=(1-B4)*B14+B4*B15*(1-B3)", p["out"]["USD"]["WACC"]),
        ("k_E FC", ("=(1+B14)*(1+B7)/(1+B6)-1" if p["method"] == "multiplicative"
                    else "=B14+(B7-B6)"), p["out"]["FC"]["Cost of equity"]),
        ("FC spread", ("=IF(B9=\"\",(1+B15)*(1+B7)/(1+B6)-1-B7,B9)" if p["method"] == "multiplicative"
                       else "=IF(B9=\"\",B8,B9)"), p["out"]["FC"]["Debt spread"]),
        ("k_D FC pre-tax", "=B7+B18", p["out"]["FC"]["Pre-tax cost of debt"]),
        ("WACC FC", "=(1-B4)*B17+B4*B19*(1-B3)", p["out"]["FC"]["WACC"]),
    ]
    for r in rows:
        wc.append(list(r))
    wc.column_dimensions["A"].width = 34
    wc.column_dimensions["B"].width = 18
    wc.column_dimensions["C"].width = 18
    for ws_ in (ws, ws2, ws3):
        for col in ws_.columns:
            ws_.column_dimensions[col[0].column_letter].width = 22
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="International Cost of Capital", page_icon="🌐", layout="wide")
st.title("🌐 International Cost of Capital")
st.caption("Cost of equity, cost of debt and WACC of a project in USD and in the "
           "currency of a selected country.")

pct = lambda x: f"{x * 100:.2f}%"  # noqa: E731

# ---------------- Sidebar: data refresh ----------------
with st.sidebar:
    st.header("Data")
    if st.button("🔄 Refresh data (fetch latest)", width="stretch"):
        st.cache_data.clear()
        st.rerun()

with st.spinner("Fetching latest data from Damodaran, OECD and Trading Economics..."):
    betas = fetch_betas()
    spread = fetch_spread()
    taxes = fetch_tax()
    yields = fetch_yields()

for name, res in [("Industry betas (Damodaran)", betas), ("BBB spread (Damodaran)", spread),
                  ("Tax rates (OECD)", taxes), ("Bond yields (Trading Economics)", yields)]:
    if not res["ok"]:
        st.warning(f"**{name}**: live fetch failed — using fallback / manual input. "
                   f"Details: {res['note']}")
    elif res.get("note"):
        st.info(f"**{name}**: {res['note']}")

beta_df: pd.DataFrame = betas["data"]
tax_df: pd.DataFrame = taxes["data"]
yld_df: pd.DataFrame = yields["data"]
tax_map = dict(zip(tax_df["ISO3"], tax_df["Tax rate (%)"]))
tax_year = dict(zip(tax_df["ISO3"], tax_df["Year"]))
yld_map = dict(zip(yld_df["Country"], yld_df["10Y yield (%)"]))
yld_date = dict(zip(yld_df["Country"], yld_df["Date"]))

# ---------------- Sidebar: selections ----------------
with st.sidebar:
    st.header("Project")
    industries = beta_df["Industry"].tolist()
    if industries:
        industry = st.selectbox("Industry", industries,
                                index=industries.index(DEFAULT_INDUSTRY)
                                if DEFAULT_INDUSTRY in industries else 0)
        beta_fetched = float(beta_df.loc[beta_df["Industry"] == industry, "Unlevered beta"].iloc[0])
    else:
        industry = "Manual"
        beta_fetched = 1.0
        st.error("Industry list unavailable — enter the unlevered beta manually below.")

    countries = sorted(set(yld_df["Country"]) | {DEFAULT_COUNTRY})
    country = st.selectbox("Target country", countries, index=countries.index(DEFAULT_COUNTRY))
    ccy = ccy_of(country)
    iso = iso3_of(country)

    tax_choice = st.radio("Project tax rate", ["U.S. tax rate", f"{country} tax rate"],
                          index=0, horizontal=True,
                          help="The same tax rate is used for USD and foreign-currency WACC.")
    erp = st.slider("Equity risk premium (%)", 0.0, 15.0, DEFAULT_ERP, 0.1) / 100
    debt = st.slider("Target debt share D/(D+E) (%)", 0.0, 90.0, DEFAULT_DEBT, 1.0) / 100

    method = st.radio(
        "Currency conversion", ["multiplicative", "additive"], index=0,
        format_func=lambda m: ("Exact: (1+k$)(1+y_FC)/(1+y_US)-1" if m == "multiplicative"
                               else "Approx.: k$ + (y_FC - y_US)"),
        help="How USD rates are translated into the foreign currency.")

    # ---------------- Overrides ----------------
    st.header("Manual overrides")
    st.caption("Switch on to replace a fetched value. Values are in %.")

    def override(label: str, fetched: float | None, key: str, step: float = 0.01,
                 fmt: str = "%.4f") -> tuple[float | None, bool]:
        on = st.toggle(f"Override {label}", value=False, key=f"t_{key}")
        if on:
            v = st.number_input(label, value=float(fetched if fetched is not None else 0.0),
                                step=step, format=fmt, key=f"n_{key}")
            return v, True
        return fetched, False

    beta_u, beta_ovr = override("unlevered beta", beta_fetched, f"beta_{industry}", 0.01)

    us_tax_f = tax_map.get("USA")
    fc_tax_f = tax_map.get(iso) if iso else None
    us_tax, us_tax_ovr = override("U.S. tax rate (%)", us_tax_f, "tax_us", 0.1, "%.2f")
    if country == "United States":
        fc_tax, fc_tax_ovr = us_tax, us_tax_ovr
    else:
        fc_tax, fc_tax_ovr = override(f"{country} tax rate (%)", fc_tax_f, f"tax_{country}",
                                      0.1, "%.2f")

    s_usd, s_usd_ovr = override("USD BBB spread (%)", spread["value"], "spread_usd")
    y_us_f = yld_map.get("United States")
    y_us, y_us_ovr = override("U.S. 10Y yield (%)", y_us_f, "y_us")
    if country == "United States":
        y_fc, y_fc_ovr = override("Foreign 10Y yield (%)", y_us, "y_fc_us")
    else:
        y_fc, y_fc_ovr = override(f"{country} 10Y yield (%)", yld_map.get(country), f"y_{country}")

    s_fc_on = st.toggle(f"Override {ccy} BBB spread", value=False, key="t_sfc")
    s_fc_ovr = None
    if s_fc_on:
        s_fc_ovr = st.number_input(f"{ccy} BBB spread (%)", value=float(s_usd or 0), step=0.01,
                                   format="%.4f", key="n_sfc")

# ---------------- Validate inputs ----------------
missing = []
if us_tax is None:
    missing.append("U.S. tax rate")
if fc_tax is None and tax_choice != "U.S. tax rate":
    missing.append(f"{country} tax rate (not in OECD data — switch on the override)")
if y_us is None:
    missing.append("U.S. 10Y yield")
if y_fc is None:
    missing.append(f"{country} 10Y yield")
if missing:
    st.error("Missing inputs: " + ", ".join(missing))
    st.stop()

tax_pct = us_tax if tax_choice == "U.S. tax rate" else fc_tax
tax = tax_pct / 100
out = cost_of_capital(beta_u, tax, debt, erp, y_us / 100, y_fc / 100, s_usd / 100,
                      None if s_fc_ovr is None else s_fc_ovr / 100, method)

# ---------------- Results ----------------
tab_res, tab_all, tab_data, tab_check, tab_about = st.tabs(
    ["📊 Results", "🏭 All industries", "🗂️ Data & sources", "✅ Verification", "ℹ️ Assumptions"])

with tab_res:
    st.subheader(f"{industry} · USD vs {ccy} ({country})")
    c0, c1, c2 = st.columns([1, 1, 1])
    c0.metric("Unlevered beta", f"{beta_u:.4f}", "manual" if beta_ovr else None,
              delta_color="off")
    c1.metric("Levered beta", f"{out['Levered beta']:.4f}")
    c2.metric("Project tax rate", f"{tax_pct:.2f}%", tax_choice, delta_color="off")

    a, b = st.columns(2)
    with a:
        st.markdown("#### 🇺🇸 USD")
        m1, m2, m3 = st.columns(3)
        m1.metric("Cost of equity", pct(out["USD"]["Cost of equity"]))
        m2.metric("Cost of debt (pre-tax)", pct(out["USD"]["Pre-tax cost of debt"]))
        m3.metric("WACC", pct(out["USD"]["WACC"]))
    with b:
        st.markdown(f"#### 🌍 {ccy}")
        m1, m2, m3 = st.columns(3)
        m1.metric("Cost of equity", pct(out["FC"]["Cost of equity"]))
        m2.metric("Cost of debt (pre-tax)", pct(out["FC"]["Pre-tax cost of debt"]))
        m3.metric("WACC", pct(out["FC"]["WACC"]))

    summary = pd.DataFrame({
        "Item": list(out["USD"].keys()),
        "USD": [pct(v) for v in out["USD"].values()],
        ccy if ccy != "USD" else "USD (target country)": [pct(v) for v in out["FC"].values()],
    })
    st.dataframe(summary, hide_index=True, width="stretch")
    spread_note = ("manual override" if s_fc_ovr is not None
                   else f"auto = (1+k_D$)(1+y_FC)/(1+y_US) − 1 − y_FC = {pct(out['spread_fc_auto'])}"
                   if method == "multiplicative" else "auto = USD spread (additive method)")
    st.caption(f"{ccy} debt spread: {spread_note}. D/E = {out['D/E']:.4f}. "
               f"Debt share {debt * 100:.0f}%, ERP {erp * 100:.2f}%.")

    fig = go.Figure()
    labels = ["Cost of equity", "Pre-tax cost of debt", "After-tax cost of debt", "WACC"]
    fig.add_bar(name="USD", x=labels, y=[out["USD"][k] * 100 for k in labels],
                text=[f"{out['USD'][k] * 100:.2f}%" for k in labels], textposition="outside")
    fig.add_bar(name=ccy, x=labels, y=[out["FC"][k] * 100 for k in labels],
                text=[f"{out['FC'][k] * 100:.2f}%" for k in labels], textposition="outside")
    fig.update_layout(barmode="group", yaxis_title="%", height=400,
                      margin=dict(t=30, b=30), legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")

# ---------------- All-industry comparison ----------------
all_rows = []
for _, r in beta_df.iterrows():
    bu = beta_u if (beta_ovr and r["Industry"] == industry) else float(r["Unlevered beta"])
    o = cost_of_capital(bu, tax, debt, erp, y_us / 100, y_fc / 100, s_usd / 100,
                        None if s_fc_ovr is None else s_fc_ovr / 100, method)
    all_rows.append({
        "Industry": r["Industry"], "Unlevered beta": round(bu, 4),
        "Levered beta": round(o["Levered beta"], 4),
        "k_E USD (%)": round(o["USD"]["Cost of equity"] * 100, 3),
        f"k_E {ccy} (%)": round(o["FC"]["Cost of equity"] * 100, 3),
        "k_D USD pre-tax (%)": round(o["USD"]["Pre-tax cost of debt"] * 100, 3),
        f"k_D {ccy} pre-tax (%)": round(o["FC"]["Pre-tax cost of debt"] * 100, 3),
        "WACC USD (%)": round(o["USD"]["WACC"] * 100, 3),
        f"WACC {ccy} (%)": round(o["FC"]["WACC"] * 100, 3),
    })
all_df = pd.DataFrame(all_rows)

with tab_all:
    st.subheader(f"All industries — {country} ({ccy}), same tax / debt share / ERP")
    if all_df.empty:
        st.info("Industry table unavailable (beta fetch failed).")
    else:
        sort_by = st.selectbox("Sort by", [f"WACC {ccy} (%)", "WACC USD (%)", "Unlevered beta",
                                           "Industry"], index=0)
        view = all_df.sort_values(sort_by, ascending=(sort_by == "Industry"))
        st.dataframe(view.style.apply(
            lambda s: ["background-color: #fff3b0" if s["Industry"] == industry else ""
                       for _ in s], axis=1),
            hide_index=True, width="stretch", height=450)
        n = st.slider("Industries in chart", 10, len(all_df), min(30, len(all_df)))
        top = all_df.sort_values(f"WACC {ccy} (%)", ascending=False).head(n)
        figa = go.Figure()
        figa.add_bar(name="WACC USD", y=top["Industry"], x=top["WACC USD (%)"], orientation="h")
        figa.add_bar(name=f"WACC {ccy}", y=top["Industry"], x=top[f"WACC {ccy} (%)"],
                     orientation="h")
        figa.update_layout(barmode="group", height=max(400, 22 * n), xaxis_title="%",
                           yaxis=dict(autorange="reversed"), legend=dict(orientation="h"),
                           margin=dict(t=30))
        st.plotly_chart(figa, width="stretch")

# ---------------- Data & sources ----------------
def _src(label, fetched_ok, url, date, fetched, used, overridden):
    return {"Input": label, "Value used": used,
            "Status": "manual override" if overridden else ("live" if fetched_ok else "fallback"),
            "Data date": date, "Fetched at": fetched, "Source": url}

src_rows = [
    _src(f"Unlevered beta — {industry}", betas["ok"], betas["source"], betas["date"],
         betas["fetched"], f"{beta_u:.4f}", beta_ovr),
    _src("USD BBB (Baa2) spread, large non-financial firms", spread["ok"], spread["source"],
         spread["date"], spread["fetched"], f"{s_usd:.4f}%", s_usd_ovr),
    _src("U.S. corporate tax rate (combined statutory)", taxes["ok"], taxes["source"],
         f"tax year {tax_year.get('USA', '?')}", taxes["fetched"],
         f"{us_tax:.2f}%" if us_tax is not None else "n/a", us_tax_ovr),
    _src(f"{country} corporate tax rate (combined statutory)", taxes["ok"], taxes["source"],
         f"tax year {tax_year.get(iso, '?')}", taxes["fetched"],
         f"{fc_tax:.2f}%" if fc_tax is not None else "n/a", fc_tax_ovr),
    _src("U.S. 10Y government bond yield", yields["ok"], yields["source"],
         yld_date.get("United States", "?"), yields["fetched"], f"{y_us:.4f}%", y_us_ovr),
    _src(f"{country} 10Y government bond yield", yields["ok"], yields["source"],
         yld_date.get(country, "?"), yields["fetched"], f"{y_fc:.4f}%", y_fc_ovr),
    {"Input": f"{ccy} BBB spread", "Value used": pct(out["FC"]["Debt spread"]),
     "Status": "manual override" if s_fc_ovr is not None else "auto-calculated",
     "Data date": "-", "Fetched at": "-", "Source": "computed in app"},
    {"Input": "Equity risk premium", "Value used": f"{erp * 100:.2f}%", "Status": "user input",
     "Data date": "-", "Fetched at": "-", "Source": "user"},
    {"Input": "Target debt share D/V", "Value used": f"{debt * 100:.1f}%",
     "Status": "user input", "Data date": "-", "Fetched at": "-", "Source": "user"},
    {"Input": "Project tax rate used", "Value used": f"{tax_pct:.2f}%", "Status": tax_choice,
     "Data date": "-", "Fetched at": "-", "Source": "user choice"},
]
inputs_df = pd.DataFrame(src_rows)

with tab_data:
    st.subheader("Inputs used, sources and dates")
    st.dataframe(inputs_df, hide_index=True, width="stretch",
                 column_config={"Source": st.column_config.LinkColumn("Source")})
    st.caption(f"OECD API endpoint: {taxes.get('api', '')}")
    with st.expander("Raw: 10-year government bond yields (Trading Economics)"):
        st.dataframe(yld_df, hide_index=True, width="stretch")
    with st.expander("Raw: corporate tax rates (OECD, latest year per country)"):
        st.dataframe(tax_df.sort_values("ISO3"), hide_index=True, width="stretch")
    with st.expander("Raw: unlevered industry betas (Damodaran)"):
        st.dataframe(beta_df, hide_index=True, width="stretch")

    st.subheader("Download")
    summary_num = pd.DataFrame({
        "Item": list(out["USD"].keys()) + ["Levered beta", "D/E"],
        "USD": [round(v, 6) for v in out["USD"].values()] + [out["Levered beta"], out["D/E"]],
        ccy: [round(v, 6) for v in out["FC"].values()] + [out["Levered beta"], out["D/E"]],
    })
    params = {"beta_u": beta_u, "tax": tax, "d": debt, "erp": erp, "y_us": y_us / 100,
              "y_fc": y_fc / 100, "s_usd": s_usd / 100,
              "s_fc_ovr": None if s_fc_ovr is None else s_fc_ovr / 100,
              "method": method, "out": out}
    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇️ Results (CSV)",
                       pd.concat([summary_num, pd.DataFrame([{}]), inputs_df]).to_csv(index=False),
                       f"cost_of_capital_{iso or country}.csv", "text/csv",
                       width="stretch")
    d2.download_button("⬇️ All industries (CSV)", all_df.to_csv(index=False),
                       f"all_industries_{iso or country}.csv", "text/csv",
                       width="stretch")
    d3.download_button("⬇️ Excel workbook (with formula check)",
                       build_excel(inputs_df, summary_num, all_df, params),
                       f"cost_of_capital_{iso or country}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       width="stretch")

# ---------------- Verification ----------------
with tab_check:
    st.subheader("Step-by-step calculation (current inputs)")
    d_ = debt
    st.markdown(f"""
| Step | Formula | Value |
|---|---|---|
| D/E | d/(1−d) = {d_:.4f}/(1−{d_:.4f}) | {out['D/E']:.6f} |
| β_L | β_U·[1+(1−t)·D/E] = {beta_u:.4f}·[1+(1−{tax:.4f})·{out['D/E']:.4f}] | {out['Levered beta']:.6f} |
| k_E (USD) | y_US + β_L·ERP = {y_us/100:.6f} + {out['Levered beta']:.4f}·{erp:.4f} | {out['USD']['Cost of equity']:.6f} |
| k_D (USD) | y_US + s_BBB = {y_us/100:.6f} + {s_usd/100:.6f} | {out['USD']['Pre-tax cost of debt']:.6f} |
| WACC (USD) | (1−d)·k_E + d·k_D·(1−t) | {out['USD']['WACC']:.6f} |
| k_E ({ccy}) | {"(1+k_E$)(1+y_FC)/(1+y_US)−1" if method == "multiplicative" else "k_E$ + (y_FC−y_US)"} , y_FC = {y_fc/100:.6f} | {out['FC']['Cost of equity']:.6f} |
| s ({ccy}) | {"override" if s_fc_ovr is not None else ("(1+k_D$)(1+y_FC)/(1+y_US)−1−y_FC" if method == "multiplicative" else "= USD spread")} | {out['FC']['Debt spread']:.6f} |
| k_D ({ccy}) | y_FC + s_FC | {out['FC']['Pre-tax cost of debt']:.6f} |
| WACC ({ccy}) | (1−d)·k_E + d·k_D·(1−t) | {out['FC']['WACC']:.6f} |
""")

    st.subheader("Automated property checks")
    base = dict(beta_u=beta_u, tax=tax, debt_share=debt, erp=erp, y_us=y_us / 100,
                spread_usd=s_usd / 100, method=method)
    tol = 1e-12
    # 1) equal yields -> equal results (override off)
    eq = cost_of_capital(y_fc=y_us / 100, **base)
    ok1 = all(abs(eq["USD"][k] - eq["FC"][k]) < tol for k in eq["USD"])
    # 2) zero debt -> WACC = cost of equity
    z = cost_of_capital(**{**base, "debt_share": 0.0}, y_fc=y_fc / 100)
    ok2 = (abs(z["USD"]["WACC"] - z["USD"]["Cost of equity"]) < tol
           and abs(z["FC"]["WACC"] - z["FC"]["Cost of equity"]) < tol)
    # 3) FC spread override leaves USD results unchanged
    o_off = cost_of_capital(y_fc=y_fc / 100, **base)
    o_on = cost_of_capital(y_fc=y_fc / 100, spread_fc_override=0.05, **base)
    ok3 = all(abs(o_off["USD"][k] - o_on["USD"][k]) < tol for k in o_off["USD"])
    checks = pd.DataFrame([
        {"Check": "Equal government yields (y_FC = y_US), spread override off → "
                  "USD and FC results identical", "Pass": "✅" if ok1 else "❌",
         "Detail": f"WACC USD {pct(eq['USD']['WACC'])} vs FC {pct(eq['FC']['WACC'])}"},
        {"Check": "Zero debt → WACC = cost of equity (both currencies)",
         "Pass": "✅" if ok2 else "❌",
         "Detail": f"USD {pct(z['USD']['WACC'])} = {pct(z['USD']['Cost of equity'])}; "
                   f"FC {pct(z['FC']['WACC'])} = {pct(z['FC']['Cost of equity'])}"},
        {"Check": "Overriding the foreign-currency spread (here 5%) leaves USD results unchanged",
         "Pass": "✅" if ok3 else "❌",
         "Detail": f"WACC USD {pct(o_off['USD']['WACC'])} → {pct(o_on['USD']['WACC'])}; "
                   f"WACC FC {pct(o_off['FC']['WACC'])} → {pct(o_on['FC']['WACC'])}"},
    ])
    st.dataframe(checks, hide_index=True, width="stretch")
    st.caption("The Excel download (Data & sources tab) contains an 'Excel check' sheet that "
               "recomputes every number with spreadsheet formulas for an independent check.")

# ---------------- Assumptions ----------------
with tab_about:
    st.markdown(r"""
**Method**

1. **Unlevered beta** β_U: Damodaran U.S. industry average (*Unlevered beta* column of `betas.xls`).
2. **Relevering (Hamada):** $\beta_L = \beta_U\,[1 + (1-t)\,D/E]$ with $D/E = d/(1-d)$ and the
   chosen project tax rate $t$.
3. **USD cost of equity (CAPM):** $k_E^{\$} = y_{US} + \beta_L \times ERP$, with the U.S. 10-year
   Treasury yield as the risk-free rate.
4. **USD cost of debt:** $k_D^{\$} = y_{US} + s_{BBB}$ — the project is assumed to borrow at a
   BBB (Baa2) rating; spread from Damodaran's table for large non-financial service firms.
5. **Foreign currency:** USD rates are translated using the 10-year government yield
   differential (international Fisher effect / interest-rate parity):
   $1 + k^{FC} = (1 + k^{\$})\,\dfrac{1 + y_{FC}}{1 + y_{US}}$.
   The foreign-currency spread is $s_{FC} = k_D^{FC} - y_{FC}$ unless overridden; an override
   changes only the foreign-currency debt cost and WACC.
6. **WACC:** $WACC = (1-d)\,k_E + d\,k_D\,(1-t)$, same $d$, $t$ and ERP in both currencies.

**Key assumptions / caveats**

- Government bond yields are treated as default-free; for countries with sovereign default risk,
  the yield differential also contains a default premium, which is then embedded in the
  foreign-currency rates.
- The yield differential is used as the expected inflation / currency depreciation differential.
- U.S. industry betas are applied to the foreign project (no country-specific beta).
- Corporate tax = OECD *combined* statutory rate (central + sub-central), latest available year.
- Trading Economics yields are scraped from the public page; if a country is missing or the page
  blocks the request, enter the yield manually.
""")
