# International Cost of Capital App

A Streamlit app that compares a project's **cost of equity, cost of debt and WACC in USD and in
the currency of any selected country**.

## Data sources (fetched live, with refresh button)

| Input | Source | Notes |
|---|---|---|
| U.S. industry **unlevered beta** | Damodaran, [`betas.xls`](https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls) | "Unlevered beta" column; HTML version used as backup |
| **BBB (Baa2) USD spread** | Damodaran, [ratings.html](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html) | Large non-financial service firms |
| **Corporate tax rates** | OECD Corporate Tax Statistics ([report page](https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/full-report/statutory-corporate-income-tax-rates_440f9286.html)) | Combined statutory CIT rate, latest year, via the OECD SDMX API (`OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT`) |
| **10-year government bond yields** | [Trading Economics /bonds](https://tradingeconomics.com/bonds) | Scraped from the public page (no API key) |

Data are cached for 12 hours; **🔄 Refresh data** in the sidebar clears the cache and re-fetches.
The *Data & sources* tab shows each value used, its data date, fetch time, source link and whether
it is live, a fallback or a manual override. If a live fetch fails, the app shows a warning and uses
a clearly-labelled built-in fallback or asks for manual input.

## Features

- Select **industry** and **target country**; adjust **equity risk premium** and **target debt share**.
- **Project tax rate switch**: U.S. or selected-country rate (the same rate is used in both currencies).
- **Manual overrides** (toggles) for unlevered beta, U.S. and foreign tax rate, USD BBB spread,
  U.S. and foreign 10Y yields, and the foreign-currency BBB spread (when off, the auto-calculated
  spread is shown).
- Side-by-side USD vs foreign-currency results, chart, **all-industry comparison**.
- Downloads: results CSV, all-industry CSV, and an **Excel workbook with an "Excel check" sheet**
  that recomputes every number with spreadsheet formulas.
- **Verification tab**: step-by-step calculation and automated checks
  (equal yields → equal results; zero debt → WACC = cost of equity;
  foreign spread override → USD results unchanged).

**Defaults:** Software (System & Application), United States, U.S. tax rate, ERP 6.5%, debt 30%.

## Formulas

```
D/E          = d / (1 - d)
beta_L       = beta_U * [1 + (1 - t) * D/E]
k_E(USD)     = y_US + beta_L * ERP
k_D(USD)     = y_US + s_BBB
k(FC)        = (1 + k(USD)) * (1 + y_FC) / (1 + y_US) - 1        (default, exact)
             = k(USD) + (y_FC - y_US)                              (optional approximation)
s_FC (auto)  = k_D(FC converted) - y_FC ;  k_D(FC) = y_FC + s_FC
WACC         = (1 - d) * k_E + d * k_D * (1 - t)
```

## Run locally (Python 3.12)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Create a GitHub repository and upload `app.py`, `requirements.txt`, `README.md`.
2. Go to https://share.streamlit.io → **Create app** → choose the repo, branch `main`, file `app.py`.
3. Under *Advanced settings* choose Python 3.12 and click **Deploy**.

## Caveats

- Government yields are treated as risk-free; for countries with default risk the yield
  differential includes a default premium.
- U.S. industry betas are applied to the foreign project.
- Trading Economics may change its page layout or block automated requests; use the yield
  overrides in that case.
