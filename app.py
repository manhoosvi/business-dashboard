"""
Business Performance Dashboard — cloud edition.

Workflow (runs automatically on every visit / refresh cycle):
  connected sources (Google Sheets / Drive / CSV URLs) + optional uploads
    -> fetch -> validate & clean -> map columns -> KPIs -> charts -> AI insights

Configuration lives in Streamlit secrets (see .streamlit/secrets.toml.example).
"""
import hashlib
import json
import os
import re
from datetime import datetime
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

import ai
from sources_store import SourceStore
from pipeline import breakdown_with_sql, clean_with_sql, kpis_with_sql, q

try:  # optional, only useful when running locally
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

st.set_page_config(page_title="Business Performance Dashboard", page_icon="📊", layout="wide")


# ----------------------------- Configuration -----------------------------
def get_secret(key: str, default: Any = None) -> Any:
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


try:
    TZ = ZoneInfo(str(get_secret("TIMEZONE", "Asia/Kolkata")))
except Exception:
    TZ = ZoneInfo("UTC")
# REFRESH_MODE = "on_open" (default): fresh data each time someone opens the dashboard, nothing runs in the background.
# REFRESH_MODE = "auto": additionally re-checks sources every REFRESH_MINUTES while the page is open.
REFRESH_MODE = str(get_secret("REFRESH_MODE", "on_open")).strip().lower()
AUTO_REFRESH = REFRESH_MODE == "auto"
REFRESH_MINUTES = max(1, int(get_secret("REFRESH_MINUTES", 5)))
REFRESH_SECONDS = REFRESH_MINUTES * 60
# on_open: keep data steady while someone is clicking around (max 1 h); auto: expire just before each check
FETCH_TTL = max(30, REFRESH_SECONDS - 15) if AUTO_REFRESH else 3600
GEMINI_MODEL = str(get_secret("GEMINI_MODEL", "gemini-flash-latest"))


def configured_sources() -> dict:
    """[data_sources] table in secrets, or DATA_SOURCES env var as JSON {"name": "url"}."""
    raw = get_secret("data_sources", None) or get_secret("DATA_SOURCES", None)
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    try:
        return {str(k): str(v) for k, v in dict(raw).items() if str(v).strip()}
    except Exception:
        return {}


# ----------------------------- Design system -----------------------------
st.markdown("""
<style>
:root { --ink:#202124; --muted:#555b65; --line:#e5e7eb; --rose:#a64f68; --paper:#ffffff; }
.stApp {background:#f7f6f8;}
[data-testid="stHeader"] {background:rgba(247,246,248,.92);}
[data-testid="stMain"] [data-testid="stMarkdownContainer"] p, [data-testid="stMain"] label p,
[data-testid="stMain"] [data-testid="stWidgetLabel"] p {color:#202124;}
/* readable in any browser theme */
[data-testid="stMain"] h1, [data-testid="stMain"] h2, [data-testid="stMain"] h3, [data-testid="stMain"] h4,
[data-testid="stMain"] h1 *, [data-testid="stMain"] h2 *, [data-testid="stMain"] h3 * {color:#202124 !important;}
[data-testid="stMain"] [data-testid="stAlert"] > div {background:#fff7e6 !important; border:1px solid #f3dcb0 !important;}
[data-testid="stMain"] [data-testid="stAlert"] p, [data-testid="stMain"] [data-testid="stAlert"] * {color:#3d2f12 !important;}
[data-testid="stMain"] [data-testid="stAlertContentInfo"], [data-testid="stMain"] [data-testid="stAlertContentInfo"] * {color:#1d3557 !important;}
[data-testid="stMain"] [data-testid="stCaptionContainer"] p {color:#626873 !important;}
[data-testid="stMain"] [data-testid="stTab"] p {color:#202124 !important;}
[data-testid="stMain"] [data-testid="stExpander"] summary p {color:#202124 !important;}
[data-testid="stHeader"] * {color:#555b65;}
.ai-banner {background:#fff;border:1px solid #ead6dd;border-left:4px solid #a64f68;border-radius:10px;padding:10px 14px;font-size:.86rem;color:#202124;margin:.2rem 0 .9rem;}
.alert-chip {display:inline-block;font-size:.78rem;padding:5px 11px;border-radius:999px;margin:0 6px 6px 0;border:1px solid;}
.alert-chip.up {background:#eaf6ef;color:#1e6b3f;border-color:#c5e6d2;} .alert-chip.down {background:#fdecea;color:#9a2a20;border-color:#f5c9c4;}
[data-testid="stSidebar"] [data-testid="stMultiSelect"] [role="group"] {max-height:none !important;}
[data-testid="stSidebar"] [data-testid="stMultiSelectTagsContainer"] {overflow:visible !important; max-height:none !important;}
[data-testid="stSidebar"] [data-testid="stMultiSelectTagsContainer"] > span {max-width:100% !important; height:auto !important;}
[data-testid="stSidebar"] [data-testid="stMultiSelectTagsContainer"] > span span {white-space:normal !important; overflow:visible !important; text-overflow:clip !important; max-width:none !important;}
.src-err {font-size:.72rem;color:#f2b8a0 !important;margin:-.05rem 0 .4rem 1.35rem;line-height:1.3;}
.err-table {width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:10px;font-size:.85rem;}
.err-table td, .err-table th {padding:8px 12px;border-bottom:1px solid #eee;color:#202124;text-align:left;}
.block-container {padding-top:3.6rem; padding-bottom:1.1rem; max-width:1600px;}
[data-testid="stSidebar"] {background:#25232b; border-right:1px solid #393640;}
[data-testid="stSidebar"] * {color:#f5f3f5 !important;}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {color:#c9c5cc !important;}
[data-testid="stSidebar"] [data-baseweb="select"] > div {background:#34313b !important; border-color:#77717f !important;}
[data-testid="stSidebar"] [data-baseweb="select"] * {color:#f5f3f5 !important;}
[data-testid="stSidebar"] [data-baseweb="select"] svg {fill:#f5f3f5 !important;}
[data-testid="stSidebar"] div[data-testid="stExpander"] {background:#34313b !important; border-color:#514c58 !important;}
[data-testid="stSidebar"] div[data-testid="stExpander"] summary:hover {background:#3d3a45;}
[data-testid="stSidebar"] [data-testid="stFileUploader"] section {background:#34313b;border:1px dashed #77717f;}
[data-testid="stSidebar"] [data-testid="stFileUploader"] section * {color:#f5f3f5 !important;}
[data-testid="stSidebar"] hr {border-color:#514c58;}
[data-testid="stSidebar"] button {background:#34313b;border:1px solid #77717f;}
h1 {font-size:1.65rem !important; letter-spacing:-.035em; margin-bottom:0 !important; color:#202124;}
h2 {font-size:1.05rem !important; letter-spacing:-.02em; color:#202124;}
h3 {font-size:.94rem !important; color:#202124;}
.dashboard-subtitle {color:#555b65;font-size:.84rem;margin:.2rem 0 .9rem 0;}
.section-label {font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:#626873;font-weight:750;margin:.25rem 0 .55rem;}
.kpi-card {background:#fff;border:1px solid #e5e7eb;border-radius:11px;padding:13px 15px;min-height:93px;}
.kpi-label {font-size:.75rem;color:#555b65;margin-bottom:8px;}
.kpi-value {font-size:1.38rem;font-weight:700;color:#202124;line-height:1.1;}
.kpi-note {font-size:.69rem;color:#626873;margin-top:7px;}
.kpi-up {color:#1e7a46;font-weight:700;} .kpi-down {color:#b3261e;font-weight:700;}
.fresh-pill {display:inline-block;font-size:.72rem;padding:3px 10px;border-radius:999px;background:#f6eef1;color:#7a3a4d;border:1px solid #ead6dd;margin-right:6px;}
.fresh-dot {display:inline-block;width:7px;height:7px;border-radius:50%;background:#1e9e5a;margin-right:6px;vertical-align:middle;}
.src-row {font-size:.78rem;margin:.15rem 0;}
[data-testid="stMetric"] {background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;}
[data-testid="stMetricLabel"] p {font-size:.75rem;color:#555b65 !important;}
[data-testid="stMetricValue"] {font-size:1.35rem;color:#202124 !important;}
[data-testid="stPlotlyChart"] {border:1px solid #e5e7eb;border-radius:10px;padding:4px;background:#fff;overflow:hidden !important;}
[data-testid="stPlotlyChart"] > div {overflow:hidden !important;}
.stMultiSelect [data-baseweb="tag"] {background:#a64f68;}
div[data-testid="stExpander"] {border:1px solid #e5e7eb;border-radius:10px;background:#fff;}
div[data-testid="stAlert"] p {color:#202124;}
footer {visibility:hidden;}
</style>
""", unsafe_allow_html=True)

ROLE_ALIASES = {
    "revenue": ["revenue", "sales", "sale_amount", "sales_amount", "net_sales", "gmv", "turnover", "amount"],
    "cost": ["cost", "cogs", "cost_of_goods", "product_cost", "total_cost"],
    "quantity": ["quantity", "qty", "units", "units_sold", "items"],
    "order_id": ["order_id", "transaction_id", "invoice_id", "order_number"],
    "customer_id": ["customer_id", "client_id", "user_id", "buyer_id"],
    "date": ["date", "order_date", "transaction_date", "created_at", "timestamp"],
    "marketing_spend": ["marketing_spend", "ad_spend", "spend", "budget_spent", "campaign_cost"],
    "impressions": ["impressions", "views", "ad_impressions"],
    "clicks": ["clicks", "link_clicks"],
    "conversions": ["conversions", "purchases", "orders_converted"],
    "leads": ["leads", "lead_count", "enquiries", "inquiries"],
    "product": ["product", "product_name", "sku", "item"],
    "region": ["region", "city", "state", "country", "market"],
    "channel": ["channel", "source", "campaign", "marketing_channel"],
}
OBJECTIVES = {
    "Sales performance": ["Total revenue", "Average order value", "Revenue trend", "Revenue by product / region"],
    "Profitability": ["Gross profit", "Gross margin", "Revenue vs cost", "Profit by category"],
    "Customer analysis": ["Unique customers", "Revenue per customer", "Customer contribution"],
    "Marketing performance": ["Marketing spend", "ROAS proxy", "Cost per conversion", "Spend by channel"],
    "Campaign funnel": ["Impressions", "Click-through rate", "Click-to-conversion rate"],
    "Product / regional analysis": ["Revenue by product", "Units sold", "Regional contribution"],
}


# ----------------------------- Helpers -----------------------------
def norm(x: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(x).strip().lower()).strip("_")


def suggest_mapping(df) -> dict:
    """Rule-based column matching: exact names first, then whole-word matches; a column is used once."""
    if df is None:
        return {}
    normalized = {norm(c): str(c) for c in df.columns}
    out, used = {}, set()
    for role, aliases in ROLE_ALIASES.items():  # pass 1: exact name
        for alias in aliases:
            col = normalized.get(alias)
            if col and col not in used:
                out[role] = col
                used.add(col)
                break

    def has_words(name: str, alias: str) -> bool:
        tokens, words = name.split("_"), alias.split("_")
        return any(tokens[i:i + len(words)] == words for i in range(len(tokens) - len(words) + 1))

    for role, aliases in ROLE_ALIASES.items():  # pass 2: alias appears as whole word(s), e.g. "Net Sales (INR)"
        if role in out:
            continue
        for alias in aliases:
            col = next((orig for n, orig in normalized.items() if orig not in used and has_words(n, alias)), None)
            if col:
                out[role] = col
                used.add(col)
                break
    return out


# ----------------------------- Ingestion -----------------------------
SHEET_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)")
DRIVE_RE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([a-zA-Z0-9_-]+)")


def to_fetch_url(url: str) -> str:
    """Turn normal Google Sheets / Drive share links into direct download links."""
    url = url.strip()
    gid = re.search(r"[#&?]gid=(\d+)", url)
    if "/spreadsheets/d/e/" in url:  # "Publish to web" link
        if "output=csv" in url:
            return url
        base = re.sub(r"/pub(html)?.*$", "/pub", url)
        return f"{base}?output=csv" + (f"&gid={gid.group(1)}" if gid else "")
    m = SHEET_RE.search(url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")
    m = DRIVE_RE.search(url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


def source_title(disposition: str) -> str:
    """Google sends the file name, e.g. 'sample - Sheet1.csv' -> 'sample · Sheet1'."""
    from urllib.parse import unquote
    m = re.search(r"filename\*=UTF-8''([^;]+)", disposition) or re.search(r'filename="?([^";]+)"?', disposition)
    if not m:
        return ""
    name = os.path.splitext(unquote(m.group(1)).strip())[0]
    return name.replace(" - ", " · ")


@st.cache_data(ttl=FETCH_TTL, show_spinner=False, max_entries=200)
def fetch_source(location: str, visit_token: str = "") -> dict:
    """Download the raw bytes of a source. Errors are not cached, so failures retry next cycle."""
    title = ""
    if os.path.exists(location):  # local path (useful for testing)
        with open(location, "rb") as fh:
            data = fh.read()
        title = os.path.splitext(os.path.basename(location))[0]
    else:
        r = requests.get(to_fetch_url(location), timeout=30)
        r.raise_for_status()
        if "text/html" in r.headers.get("content-type", "").lower():
            raise ValueError("The link returned a web page instead of data. Share the Sheet/file as "
                             "'Anyone with the link – Viewer', or use File → Share → Publish to web → CSV.")
        data = r.content
        title = source_title(r.headers.get("content-disposition", ""))
    return {"bytes": data, "hash": hashlib.md5(data).hexdigest(), "title": title,
            "fetched_at": datetime.now(TZ).strftime("%d %b %Y, %H:%M")}


def parse_bytes(data: bytes, name: str = "") -> pd.DataFrame:
    is_excel = data[:2] == b"PK" or data[:4] == b"\xd0\xcf\x11\xe0" or name.lower().endswith((".xlsx", ".xls"))
    if is_excel:
        return pd.read_excel(BytesIO(data))
    try:
        return pd.read_csv(BytesIO(data))
    except UnicodeDecodeError:
        return pd.read_csv(BytesIO(data), encoding="latin-1")


@st.cache_data(show_spinner=False, max_entries=64)
def process_bytes(data_hash: str, _data: bytes, name: str) -> tuple:
    """Parse a file, then run the SQL cleaning stage. Returns (clean_df, report, sql)."""
    return clean_with_sql(parse_bytes(_data, name))


def number_col(df, mapping, role):
    col = mapping.get(role)
    if col and col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return None


def fmt_money(x):
    if abs(x) >= 1_000_000: return f"{x/1_000_000:,.2f}M"
    if abs(x) >= 100_000: return f"{x/100_000:,.2f}L"
    return f"{x:,.0f}"


def build_metrics(df, m):
    """Format the KPI values computed by the SQL engine (pipeline.kpis_with_sql)."""
    k, sql = kpis_with_sql(df, m)
    out = {}
    g = lambda key: k.get(key)
    if g("total_revenue") is not None:
        out["Total revenue"] = (fmt_money(g("total_revenue")), "Across selected records")
        if g("avg_order_value") is not None:
            out["Average order value"] = (fmt_money(g("avg_order_value")), f"{int(g('orders') or 0):,} orders")
    if g("gross_profit") is not None:
        out["Gross profit"] = (fmt_money(g("gross_profit")), "Revenue less mapped cost")
        out["Gross margin"] = (f"{g('gross_margin_pct') or 0:.1f}%", "Gross profit / revenue")
    if g("units_sold") is not None: out["Units sold"] = (f"{g('units_sold'):,.0f}", "Total mapped quantity")
    if g("unique_customers") is not None:
        out["Unique customers"] = (f"{int(g('unique_customers')):,}", "Distinct customer IDs")
        if g("revenue_per_customer") is not None:
            out["Revenue per customer"] = (fmt_money(g("revenue_per_customer")), "Revenue / unique customers")
    if g("marketing_spend") is not None:
        out["Marketing spend"] = (fmt_money(g("marketing_spend")), "Total mapped spend")
        if g("roas") is not None: out["ROAS proxy"] = (f"{g('roas'):.2f}x", "Revenue / spend; not attributed ROAS")
        if g("cost_per_conversion") is not None:
            out["Cost per conversion"] = (fmt_money(g("cost_per_conversion")), "Spend / conversions")
    if g("ctr_pct") is not None: out["Click-through rate"] = (f"{g('ctr_pct'):.2f}%", "Clicks / impressions")
    if g("click_to_conversion_pct") is not None:
        out["Click-to-conversion"] = (f"{g('click_to_conversion_pct'):.2f}%", "Conversions / clicks")
    return out, sql


DATE_DAYFIRST_DEFAULT = str(get_secret("DATE_DAYFIRST", "true")).lower() in ("1", "true", "yes")


def detect_dayfirst(values: pd.Series) -> bool:
    """For dates like 03/04/2026: day-first if any first part > 12, month-first if any second part > 12."""
    parts = values.dropna().astype(str).str.extract(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")
    parts = parts.dropna().astype(int)
    if parts.empty:
        return False  # ISO style (2026-03-04) or text months: dayfirst not needed
    if (parts[0] > 12).any():
        return True
    if (parts[1] > 12).any():
        return False
    return DATE_DAYFIRST_DEFAULT


def parse_date(df, col):
    if not col or col not in df.columns: return None
    series = df[col]
    if pd.api.types.is_datetime64_any_dtype(series):
        return series if series.notna().any() else None
    dayfirst = detect_dayfirst(series)
    try:
        parsed = pd.to_datetime(series, errors="coerce", format="mixed", dayfirst=dayfirst)
    except (TypeError, ValueError):
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=dayfirst)
    return parsed if parsed.notna().any() else None


def monthly_revenue(df, m):
    """Revenue by month from the SQL engine. Returns (Series indexed by Period, sql) or (None, None)."""
    if not m.get("revenue") or "_date" not in df.columns or df["_date"].isna().all():
        return None, None
    out, sql = breakdown_with_sql(df, "date_trunc('month', _date)",
                                  {"revenue": f"SUM(TRY_CAST({q(m['revenue'])} AS DOUBLE))"})
    if out.empty:
        return None, sql
    s = pd.Series(out["revenue"].values, index=pd.to_datetime(out["grp"]).dt.to_period("M"))
    return s, sql


def plot_theme(fig, height=260):
    """One consistent chart style: title on top, legend on its own row under it (side legend for pies)."""
    is_pie = any(getattr(t, "type", "") == "pie" for t in fig.data)
    legend_items = sum(1 for t in fig.data if getattr(t, "showlegend", None) is not False and getattr(t, "name", None))
    has_legend = is_pie or legend_items > 1
    fig.update_layout(
        height=height, margin=dict(l=12, r=12, t=(78 if has_legend and not is_pie else 48), b=12),
        template="plotly_white", paper_bgcolor="white", plot_bgcolor="white",
        font=dict(family="Arial", size=12, color="#3a3438"),
        title=dict(font=dict(size=13, color="#3d353b"), x=0.02, xanchor="left", y=0.97, yanchor="top"),
        showlegend=has_legend,
        legend=(dict(orientation="v", x=1.0, xanchor="left", y=0.5, yanchor="middle", title_text="", font=dict(size=11))
                if is_pie else
                dict(orientation="h", x=0, xanchor="left", y=1.02, yanchor="bottom", title_text="", font=dict(size=11))),
    )
    if is_pie:
        fig.update_layout(margin=dict(l=12, r=110, t=48, b=12))
    fig.update_xaxes(showgrid=False, linecolor="#d9d2d6", tickfont=dict(color="#3a3438"), title_font=dict(color="#3a3438"),
                     automargin=True)
    fig.update_yaxes(gridcolor="#efe8ec", zeroline=False, tickfont=dict(color="#3a3438"), title_font=dict(color="#3a3438"),
                     automargin=True)
    return fig


@st.cache_data(ttl=86400, show_spinner=False)
def gemini_insights(summary_json: str, model: str) -> str:
    prompt = (
        "You are a business analyst writing for a team dashboard. Using ONLY the aggregated figures in "
        "the JSON below, write concise markdown with three sections: **Key observations** (3-5 bullets "
        "citing numbers), **Watch-outs** (1-3 bullets), **Recommended actions** (3 bullets). Do not invent "
        "figures, do not claim causes the data cannot show, and say so if the data is too thin.\n\n"
        f"```json\n{summary_json}\n```"
    )
    return ai.generate(prompt, AI_KEY, model)


# ----------------------------- AI helpers (cached so each question / dataset costs one call) -----------------------------
AI_KEY = str(get_secret("GEMINI_API_KEY", "") or "").strip().strip('"').strip("'")
ai.USE_VERTEX = str(get_secret("GEMINI_USE_VERTEX", "")).lower() in ("1", "true", "yes")
AI_ON = bool(AI_KEY)


@st.cache_data(ttl=3600, max_entries=32, show_spinner=False)
def cached_profile(df: pd.DataFrame) -> str:
    return json.dumps(ai.schema_profile(df), sort_keys=True)


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def cached_ai_mapping(profile_json: str, missing: tuple, used: tuple) -> dict:
    return ai.ai_column_mapping(json.loads(profile_json), list(missing), list(used), AI_KEY, GEMINI_MODEL)


@st.cache_data(ttl=86400, show_spinner=False)
def cached_text_to_sql(question: str, profile_json: str, previous_sql: str = None, error: str = None) -> dict:
    return ai.text_to_sql(question, json.loads(profile_json), AI_KEY, GEMINI_MODEL, previous_sql, error)


@st.cache_data(ttl=86400, show_spinner=False)
def cached_answer(question: str, result_csv: str) -> str:
    return ai.answer_from_result(question, pd.read_csv(BytesIO(result_csv.encode())), AI_KEY, GEMINI_MODEL)


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def cached_suggestions(profile_json: str) -> dict:
    return ai.suggest_analyses(json.loads(profile_json), AI_KEY, GEMINI_MODEL)


def ask_with_retry(question: str, profile_json: str, data: pd.DataFrame) -> dict:
    """Question -> SQL -> run. If the SQL fails, give the error back to the AI once to fix it."""
    plan = cached_text_to_sql(question, profile_json)
    if not plan.get("sql"):
        return {"sql": None, "explanation": plan.get("explanation", "The data can't answer that question.")}
    try:
        return {**plan, "result": ai.run_sql(data, plan["sql"])}
    except Exception as e:
        fixed = cached_text_to_sql(question, profile_json, plan["sql"], str(e)[:500])
        if not fixed.get("sql"):
            raise
        return {**fixed, "result": ai.run_sql(data, fixed["sql"])}


def result_chart(res: pd.DataFrame, kind: str = "auto", title: str = ""):
    """Pick a simple chart for a query result: first column = label/date, first numeric column = value."""
    if res is None or res.empty or res.shape[1] < 2 or kind == "table":
        return None
    x = res.columns[0]
    nums = [c for c in res.columns[1:] if pd.api.types.is_numeric_dtype(res[c])]
    if not nums:
        return None
    y = nums[0]
    is_time = pd.api.types.is_datetime64_any_dtype(res[x])
    if kind == "line" or (kind == "auto" and is_time):
        fig = px.line(res, x=x, y=y, markers=True, title=title)
        fig.update_traces(line=dict(color="#a64f68", width=2.5))
    else:
        data = res.head(25).copy()
        data[x] = data[x].astype(str)
        fig = px.bar(data, x=x, y=y, title=title, text_auto=".3s")
        fig.update_traces(marker_color="#b8667d")
    return plot_theme(fig, 300)


def friendly_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "no columns to parse" in low or "no data rows" in low or "empty" in low:
        return "The sheet is empty. Put column names in row 1 and your data below, then press Refresh now."
    if "web page instead of data" in low or "401" in low or "403" in low:
        return "Google won't share this sheet. Open it → Share → General access → Anyone with the link → Viewer."
    if "404" in low:
        return "Sheet not found. Check the link in Secrets, it may have a typo or the sheet was deleted."
    if "timed out" in low or "connection" in low:
        return "Couldn't reach Google just now. Press Refresh now in a minute."
    return msg[:160]


# ----------------------------- Run the ingestion pipeline -----------------------------
# A new visit (new browser session) gets a new token, so its first load always pulls fresh data.
# Within that visit, filters/clicks reuse the same data until "Refresh now" is pressed.
if "visit_token" not in st.session_state:
    st.session_state["visit_token"] = datetime.now().isoformat()
VISIT = "" if AUTO_REFRESH else st.session_state["visit_token"]
STORE = SourceStore(str(get_secret("GITHUB_TOKEN", "") or ""), str(get_secret("GITHUB_REPO", "") or ""),
                    str(get_secret("GITHUB_BRANCH", "main") or "main"))
ADMIN_PASSWORD = str(get_secret("ADMIN_PASSWORD", "") or "")


@st.cache_data(ttl=60, show_spinner=False)
def load_saved_sources() -> tuple:
    try:
        return STORE.load()[0], ""
    except Exception as e:
        return {}, str(e)[:160]


secret_sources = configured_sources()
saved_sources, saved_error = load_saved_sources()
session_sources = st.session_state.setdefault("session_sources", {})
sources = {**saved_sources, **session_sources, **secret_sources}
source_kind = {**{n: "saved" for n in saved_sources}, **{n: "session" for n in session_sources},
               **{n: "secrets" for n in secret_sources}}
datasets, pipeline_log, signature, cleaning_sql = {}, [], [], {}
for name, loc in sources.items():
    try:
        raw = fetch_source(loc, VISIT)
        clean, rep, clean_sql = process_bytes(raw["hash"], raw["bytes"], loc)
        label = raw.get("title") or name
        if label in datasets:
            label = f"{label} ({name})"
        cleaning_sql[label] = clean_sql
        signature.append(raw["hash"])
        if clean.empty:
            raise ValueError("Source contains no data rows")
        datasets[label] = clean
        pipeline_log.append({"Source": label, "Type": "Connected", "Status": "✅ OK", "Last fetched": raw["fetched_at"], **rep})
    except Exception as e:
        signature.append(f"error:{name}")
        pipeline_log.append({"Source": name, "Type": "Connected", "Status": f"⚠️ {friendly_error(e)}", "Last fetched": "—"})
st.session_state["data_sig"] = tuple(signature)


@st.fragment(run_every=REFRESH_SECONDS)
def freshness_watcher():
    """Runs in the background while the page is open; reruns the app when any source changes."""
    if not sources:
        return
    sig = []
    for name, loc in sources.items():
        try:
            sig.append(fetch_source(loc, VISIT)["hash"])
        except Exception:
            sig.append(f"error:{name}")
    if tuple(sig) != st.session_state.get("data_sig"):
        st.session_state["data_sig"] = tuple(sig)
        st.rerun()
    st.caption(f"Auto-checks every {REFRESH_MINUTES} min · last check {datetime.now(TZ):%H:%M}")


# ----------------------------- Sidebar: ingestion + navigation -----------------------------
with st.sidebar:
    st.markdown("## 📊 Workspace")
    st.caption("Business performance analytics")

    st.markdown("<div style='font-size:.68rem;font-weight:800;letter-spacing:.1em;color:#c9c5cc;margin:1rem 0 .45rem'>CONNECTED DATA</div>", unsafe_allow_html=True)
    with st.expander("➕ Add or manage sources", expanded=not sources):
        new_name = st.text_input("Name", placeholder="e.g. Sales 2026", key="new_src_name")
        new_link = st.text_input("Google Sheet / Drive / CSV link", placeholder="https://docs.google.com/spreadsheets/d/…",
                                 key="new_src_link")
        pw = st.text_input("Admin password", type="password", key="admin_pw") if (ADMIN_PASSWORD and STORE.can_save) else ""
        if st.button("Test & add", width="stretch", key="add_src"):
            name, link = new_name.strip(), new_link.strip()
            if not name or not link:
                st.error("Enter a name and a link.")
            elif name in sources:
                st.error(f"'{name}' already exists. Pick another name.")
            else:
                try:
                    raw = fetch_source(link, datetime.now().isoformat())
                    test_df, _, _ = process_bytes(raw["hash"], raw["bytes"], link)
                    if test_df.empty:
                        raise ValueError("no data rows")
                except Exception as e:
                    st.error(f"Couldn't read that link: {friendly_error(e)}")
                else:
                    if STORE.can_save:
                        if ADMIN_PASSWORD and pw != ADMIN_PASSWORD:
                            st.error("Wrong admin password.")
                        else:
                            try:
                                STORE.save({**saved_sources, name: link}, f"Add data source: {name}")
                                load_saved_sources.clear()
                                st.success(f"Saved '{name}' for everyone ({len(test_df):,} rows).")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                    else:
                        session_sources[name] = link
                        st.success(f"Added '{name}' for this session ({len(test_df):,} rows). "
                                   "To save sources for everyone, add GITHUB_TOKEN and GITHUB_REPO to Secrets.")
                        st.rerun()
        if sources:
            st.markdown("<div style='font-size:.72rem;margin-top:.6rem;color:#c9c5cc'>Current sources</div>", unsafe_allow_html=True)
        for n in list(sources):
            kind = source_kind.get(n)
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"<div class='src-row'>{n}<br><span style='font-size:.66rem;color:#a9a3ad'>"
                        f"{ {'secrets': '🔒 set in Secrets', 'saved': 'saved for everyone', 'session': 'this session only'}[kind] }"
                        f"</span></div>", unsafe_allow_html=True)
            if kind in ("saved", "session") and c2.button("✕", key=f"rm_{n}", help=f"Remove {n}"):
                if kind == "session":
                    session_sources.pop(n, None)
                    st.rerun()
                elif ADMIN_PASSWORD and pw != ADMIN_PASSWORD:
                    st.error("Enter the admin password above to remove saved sources.")
                else:
                    try:
                        STORE.save({k: v for k, v in saved_sources.items() if k != n}, f"Remove data source: {n}")
                        load_saved_sources.clear()
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
        if saved_error:
            st.caption(f"Couldn't load saved sources: {saved_error}")
        if not STORE.can_save:
            st.caption("Sources added here last for this session. Add GITHUB_TOKEN + GITHUB_REPO in Secrets to save them for everyone.")
    # ---- pick the active dataset with radio circles: connected sources here, uploads below ----
    def pick_from(src_key: str, other_key: str):
        st.session_state["active_ds"] = st.session_state.get(src_key)
        st.session_state[other_key] = None

    connected_ok = list(datasets.keys())
    if len(connected_ok) >= 2:  # combined view when connected sources share the same columns (e.g. monthly sheets)
        if len({frozenset(map(norm, datasets[n].columns)) for n in connected_ok}) == 1:
            datasets["All sources (combined)"] = pd.concat([datasets[n].assign(Source=n) for n in connected_ok],
                                                            ignore_index=True)
            connected_ok = connected_ok + ["All sources (combined)"]

    # uploads are read first (shown further down) so a sensible default can be chosen
    upload_files = st.session_state.get("uploads") or []
    upload_names = []
    for f in upload_files:
        try:
            data = f.getvalue()
            clean, rep, clean_sql = process_bytes(hashlib.md5(data).hexdigest(), data, f.name)
            label, n_dup = f"{f.name} (upload)", 2
            while label in datasets or label in upload_names:   # same file name uploaded twice -> keep both, distinct
                label = f"{f.name} #{n_dup} (upload)"
                n_dup += 1
            cleaning_sql[label] = clean_sql
            if not clean.empty:
                datasets[label] = clean
                upload_names.append(label)
            pipeline_log.append({"Source": f.name, "Type": "Upload", "Status": "✅ OK" if not clean.empty else "⚠️ The file has no data rows.",
                                 "Last fetched": datetime.now(TZ).strftime("%d %b %Y, %H:%M"), **rep})
        except Exception as e:
            pipeline_log.append({"Source": f.name, "Type": "Upload", "Status": f"⚠️ {friendly_error(e)}", "Last fetched": "—"})

    seen = st.session_state.setdefault("seen_uploads", set())
    new_uploads = [u for u in upload_names if u not in seen]
    seen.update(upload_names)
    active = st.session_state.get("active_ds")
    if new_uploads:                       # a freshly uploaded file becomes the active dataset
        active = new_uploads[-1]
    elif active not in datasets:
        active = (connected_ok or upload_names or [None])[0]
    st.session_state["active_ds"] = active
    st.session_state["pick_conn"] = active if active in connected_ok else None
    st.session_state["pick_up"] = active if active in upload_names else None

    if sources:
        if connected_ok:
            st.radio("Connected data", connected_ok, key="pick_conn", label_visibility="collapsed",
                     format_func=lambda n: f"🟢 {n}", on_change=pick_from, args=("pick_conn", "pick_up"))
        for row in pipeline_log:
            if row["Type"] == "Connected" and not row["Status"].startswith("✅"):
                st.markdown(f"<div class='src-row'>🟠 {row['Source']}</div>", unsafe_allow_html=True)
                st.markdown(f"<div class='src-err'>{row['Status'].lstrip('⚠️ ')}</div>", unsafe_allow_html=True)
        if st.button("↻ Refresh now", width="stretch"):
            st.session_state["visit_token"] = datetime.now().isoformat()
            if AUTO_REFRESH:
                fetch_source.clear()
            st.rerun()
        if AUTO_REFRESH:
            freshness_watcher()
        else:
            st.caption("Data refreshes each time the dashboard is opened.")
    else:
        st.caption("No connected sources yet. Use ➕ Add or manage sources above to connect a Google Sheet.")

    st.file_uploader("Add CSV / Excel (this session only)", type=["csv", "xlsx", "xls"],
                     accept_multiple_files=True, key="uploads")
    if upload_names:
        st.radio("Uploaded files", upload_names, key="pick_up", label_visibility="collapsed",
                 format_func=lambda n: f"📄 {n.replace(' (upload)', '')}", on_change=pick_from, args=("pick_up", "pick_conn"))
    for row in pipeline_log:
        if row["Type"] == "Upload" and not row["Status"].startswith("✅"):
            st.markdown(f"<div class='src-err'>{row['Source']}: {row['Status'].lstrip('⚠️ ')}</div>", unsafe_allow_html=True)
    st.divider()

    st.markdown("<div style='font-size:.68rem;font-weight:800;letter-spacing:.1em;color:#c9c5cc;margin:.35rem 0 .45rem'>DATA SOURCES</div>", unsafe_allow_html=True)
    view = st.radio("Choose a workspace", ["Overview", "X analytics", "Discord analytics"], label_visibility="collapsed")
    x_ready = bool(get_secret("X_BEARER_TOKEN", ""))
    discord_ready = bool(get_secret("DISCORD_BOT_TOKEN", ""))
    st.caption(f"X API: {'Credentials detected' if x_ready else 'Setup required'}")
    st.caption(f"Discord bot: {'Credentials detected' if discord_ready else 'Setup required'}")
    st.divider()

    st.markdown("<div style='font-size:.68rem;font-weight:800;letter-spacing:.1em;color:#c9c5cc;margin:.35rem 0 .45rem'>ANALYSIS</div>", unsafe_allow_html=True)
    active_name, raw_df, mapping = None, None, {}
    if datasets and active:
        active_name = active
        raw_df = datasets[active_name]
        auto = suggest_mapping(raw_df)
        ai_roles = set()
        # AI fallback: if name rules left roles empty and there are unclaimed columns, ask Gemini once
        missing = [r for r in ROLE_ALIASES if r not in auto]
        unclaimed = [c for c in map(str, raw_df.columns) if c not in auto.values()]
        ai_view = st.session_state.get("dash_mode", "🤖 AI-designed") == "🤖 AI-designed"
        if AI_ON and missing and unclaimed and (not ai_view or "date" not in auto):
            try:
                prof = cached_profile(raw_df)
                with st.spinner("AI is reading your columns…"):
                    extra = cached_ai_mapping(prof, tuple(missing), tuple(sorted(auto.values())))
                auto.update(extra)
                ai_roles = set(extra)
            except Exception as e:
                st.caption(f"AI column mapping unavailable: {str(e)[:80]}")
        if ai_roles:
            st.caption(f"🤖 AI matched {len(ai_roles)} column(s) the name rules missed. Check them below.")
        # Column roles as removable chips ("Revenue → Sales"); one column per role.
        role_label = lambda r: r.replace("_", " ").title()
        pair = lambda r, c: f"{role_label(r)} → {c}"
        all_pairs = [pair(r, c) for r in ROLE_ALIASES for c in map(str, raw_df.columns)]
        lookup = {pair(r, c): (r, c) for r in ROLE_ALIASES for c in map(str, raw_df.columns)}
        default_pairs = [pair(r, auto[r]) for r in ROLE_ALIASES if r in auto and auto[r] in raw_df.columns]
        chip_key = f"roles_{active_name}_{hashlib.md5(json.dumps(auto, sort_keys=True).encode()).hexdigest()[:8]}"

        def one_column_per_role(key=chip_key):
            kept = {}
            for p in st.session_state.get(key, []):   # newest choice for a role wins
                kept[lookup[p][0]] = p
            st.session_state[key] = list(kept.values())

        with st.expander("Column roles", expanded=False):
            st.caption("How columns are used by the Standard view. ✕ removes a role. To add or change one, pick a "
                       "Role and a Column and press Add. Removed one by mistake? Press Reset."
                       + (f" 🤖 AI matched: {', '.join(role_label(r) for r in sorted(ai_roles))}." if ai_roles else ""))
            if chip_key not in st.session_state:
                st.session_state[chip_key] = default_pairs

            def add_role(key=chip_key):
                r, c = st.session_state.get(f"{key}_role"), st.session_state.get(f"{key}_col")
                if r and c:
                    st.session_state[key] = [p for p in st.session_state.get(key, []) if lookup[p][0] != r] + [pair(r, c)]

            def reset_roles(key=chip_key):
                st.session_state[key] = list(default_pairs)

            # 1) pick a role + column and add it ...
            mapped_roles = {lookup[p][0] for p in st.session_state.get(chip_key, []) if p in lookup}
            role_opts = sorted(ROLE_ALIASES, key=lambda r: (r in mapped_roles, list(ROLE_ALIASES).index(r)))
            st.selectbox("Role", role_opts, key=f"{chip_key}_role",
                         format_func=lambda r: role_label(r) + ("  (set)" if r in mapped_roles else ""))
            st.selectbox("Column", list(map(str, raw_df.columns)), key=f"{chip_key}_col")
            b1, b2 = st.columns(2)
            b1.button("➕ Add", key=f"{chip_key}_add", on_click=add_role, width="stretch")
            b2.button("↺ Reset", key=f"{chip_key}_reset", on_click=reset_roles, width="stretch",
                      help="Restore the automatically detected roles")
            # 2) ... and it lands in the box below, where ✕ removes it
            chosen_pairs = st.multiselect("Selected roles", all_pairs, key=chip_key,
                                          on_change=one_column_per_role,
                                          placeholder="No roles yet. Add one above.")
        for p in chosen_pairs:
            r, c = lookup[p]
            mapping[r] = c
        st.caption(f"Using **{active_name.replace(' (upload)', '')}** · {len(raw_df):,} rows · {len(raw_df.columns)} columns")
    else:
        st.caption("Connect a source or upload a file to activate business KPIs and charts.")

# ----------------------------- Source workspace states -----------------------------
if view == "X analytics":
    st.markdown("# X analytics")
    st.markdown('<div class="dashboard-subtitle">Account-level and post-level performance from an authenticated X API connection.</div>', unsafe_allow_html=True)
    st.info("X is not connected yet. This build includes the source workspace and secure configuration checks, but it does not yet fetch live X data or display fabricated metrics.")
    st.markdown("**Required setup**")
    st.markdown("1. Create an X developer app and confirm that its API tier exposes the account and post metrics you need.\n2. Add `X_BEARER_TOKEN` to the app's secrets (never commit it).\n3. Implement OAuth/user-context access for account-specific metrics where bearer-token access is insufficient.")
    st.markdown("**Planned dashboard sections**")
    a, b, c = st.columns(3)
    a.metric("Account performance", "Awaiting connection")
    b.metric("Post-wise analysis", "Awaiting connection")
    c.metric("Performance trends", "Awaiting connection")
    st.caption("Metrics such as impressions, clicks and profile activity depend on the API access level and permissions actually granted.")
    st.stop()

if view == "Discord analytics":
    st.markdown("# Discord analytics")
    st.markdown('<div class="dashboard-subtitle">Server growth, member activity and channel-level engagement.</div>', unsafe_allow_html=True)
    st.info("Discord is not connected yet. This build includes the source workspace and secure configuration checks, but it does not yet fetch live server data or display fabricated metrics.")
    st.markdown("**Required setup**")
    st.markdown("1. Create a Discord application and bot, then invite it to the target server with only the permissions required.\n2. Enable the necessary privileged intents (including Server Members and Message Content if the planned analysis requires them).\n3. Add `DISCORD_BOT_TOKEN` to the app's secrets (never commit it).")
    st.markdown("**Planned dashboard sections**")
    a, b, c = st.columns(3)
    a.metric("Server growth", "Awaiting connection")
    b.metric("Channel-wise activity", "Awaiting connection")
    c.metric("Member participation", "Awaiting connection")
    st.caption("Member retention and participation require suitable event history; a bot cannot reconstruct historical data it did not collect.")
    st.stop()

if raw_df is None:
    st.markdown("# Business Performance Dashboard")
    st.markdown('<div class="dashboard-subtitle">One shared, self-updating view of business performance.</div>', unsafe_allow_html=True)
    st.markdown("### Connect your data")
    left, right = st.columns(2)
    with left:
        st.markdown("**Connected sources (recommended)**")
        st.write("Paste a Google Sheet link into **➕ Add or manage sources** in the sidebar. "
                 "The dashboard re-reads it automatically, so when someone updates the sheet, everyone sees the new numbers.")
    with right:
        st.markdown("**One-off uploads**")
        st.write("Upload CSV / Excel files in the sidebar for a quick look. Uploads are only visible in your own session.")
    if any(not r["Status"].startswith("✅") for r in pipeline_log):
        st.warning("Some connected sources could not be read. Fix the issue below, then press Refresh now.")
        rows = "".join(f"<tr><td><b>{r['Source']}</b></td><td>{r['Status']}</td></tr>"
                       for r in pipeline_log if not r["Status"].startswith("✅"))
        st.markdown(f"<table class='err-table'><tr><th>Source</th><th>Problem</th></tr>{rows}</table>", unsafe_allow_html=True)
    st.info("No sample figures are shown. This keeps the dashboard free of misleading demo metrics.")
    st.stop()

# ----------------------------- Main dashboard -----------------------------
st.markdown("# Business Performance Dashboard")
date_all = parse_date(raw_df, mapping.get("date"))
fetched_times = [r["Last fetched"] for r in pipeline_log if r["Type"] == "Connected" and r["Last fetched"] != "—"]
pills = []
if date_all is not None:
    pills.append(f"<span class='fresh-pill'>Data through {date_all.max():%d %b %Y}</span>")
if fetched_times:
    pills.append(f"<span class='fresh-pill'><span class='fresh-dot'></span>Synced {max(fetched_times)}</span>")
    pills.append(f"<span class='fresh-pill'>Auto-refresh every {REFRESH_MINUTES} min</span>" if AUTO_REFRESH
                 else "<span class='fresh-pill'>Updates on open</span>")
st.markdown(f'<div class="dashboard-subtitle">{"".join(pills) or "A live view of the selected dataset."}</div>', unsafe_allow_html=True)


def ai_error_message(e: Exception) -> str:
    low = str(e).lower()
    if "api key not valid" in low or "api_key_invalid" in low or "permission_denied" in low or "unauthenticated" in low:
        return ("Gemini rejected the API key. Check GEMINI_API_KEY in the app's Secrets: copy the whole key from "
                "aistudio.google.com → Get API key (it may start with 'AQ.' or 'AIza'), inside quotes, with no spaces. "
                f"Google's message: {str(e)[:120]}")
    if "429" in low or "quota" in low or "resource_exhausted" in low:
        return "Gemini's usage limit was reached. It will work again shortly (free limits reset every minute/day)."
    if "not found" in low and "model" in low:
        return "The Gemini model name isn't available for this key. Set GEMINI_MODEL in Secrets (e.g. gemini-flash-latest)."
    return str(e)[:160]


# ============================= AI-designed dashboard =============================
def fmt_value(v, fmt: str) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if fmt == "money": return fmt_money(v)
    if fmt == "percent": return f"{v:.1f}%"
    if fmt == "ratio": return f"{v:.2f}x"
    if fmt == "days": return f"{v:,.1f} days"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"


def sql_scalar(data: pd.DataFrame, sql: str):
    if data is None or data.empty:
        return None
    res = ai.run_sql(data, sql, limit=1)
    if res.empty:
        return None
    v = res.iloc[0, 0]
    return None if pd.isna(v) else v


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def cached_plan(schema_sig: str, _profile_json: str, _df: pd.DataFrame) -> dict:
    """Gemini designs the dashboard once per data *structure*; every query is test-run and repaired if broken."""
    prof = json.loads(_profile_json)
    plan = ai.plan_dashboard(prof, AI_KEY, GEMINI_MODEL)
    kpis = [k for k in (plan.get("kpis") or [])[:6] if isinstance(k, dict) and k.get("sql")]
    charts = [c for c in (plan.get("charts") or [])[:6] if isinstance(c, dict) and c.get("sql")]
    items = {f"kpi{i}": k for i, k in enumerate(kpis)} | {f"chart{i}": c for i, c in enumerate(charts)}
    failures = []
    for id_, item in items.items():
        try:
            ai.run_sql(_df, item["sql"], limit=5)
        except Exception as e:
            failures.append({"id": id_, "title": item.get("title", ""), "sql": item["sql"], "error": str(e)[:300]})
    repaired = 0
    if failures:
        try:
            fixes = {f["id"]: f["sql"] for f in ai.repair_queries(prof, failures, AI_KEY, GEMINI_MODEL).get("fixed", [])
                     if isinstance(f, dict) and f.get("sql")}
        except Exception:
            fixes = {}
        for f in failures:
            item, new = items[f["id"]], fixes.get(f["id"])
            try:
                if not new:
                    raise ValueError("not fixed")
                ai.run_sql(_df, new, limit=5)
                item["sql"], item["repaired"] = new, True
                repaired += 1
            except Exception:
                item["drop"] = True
    plan["kpis"] = [k for k in kpis if not k.get("drop")]
    plan["charts"] = [c for c in charts if not c.get("drop")]
    pm = plan.get("primary_metric") or {}
    try:
        ai.run_sql(_df, f"SELECT {pm['expr']} AS value FROM data", limit=1)
    except Exception:
        plan["primary_metric"] = None
    cols = set(map(str, _df.columns))
    ok_col = lambda c: c in cols and c != "_date" and 1 < _df[c].nunique(dropna=True) <= 50
    plan["filters"] = [c for c in (plan.get("filters") or []) if isinstance(c, str) and ok_col(c)][:4]
    plan["breakdowns"] = [c for c in (plan.get("breakdowns") or []) if isinstance(c, str) and ok_col(c)][:3]
    plan["_stats"] = {"queries": len(items), "repaired": repaired, "dropped": len(failures) - repaired}
    return plan


@st.cache_data(ttl=86400, show_spinner=False)
def cached_insights(data_key: str, context_json: str) -> str:
    return ai.write_insights(json.loads(context_json), AI_KEY, GEMINI_MODEL)


def compare_periods(data: pd.DataFrame):
    """Latest month vs previous month. If the latest month is partial, compare the same number of days."""
    if "_date" not in data.columns or data["_date"].isna().all():
        return None
    d = data["_date"]
    last = d.max().normalize()
    cs = last.replace(day=1)
    ps = cs - pd.offsets.MonthBegin(1)
    partial = last < (cs + pd.offsets.MonthEnd(0)).normalize()
    pe = min(ps + pd.Timedelta(days=(last - cs).days), cs - pd.Timedelta(days=1)) if partial else cs - pd.Timedelta(days=1)
    cur = data[(d >= cs) & (d < last + pd.Timedelta(days=1))]
    prev = data[(d >= ps) & (d < pe + pd.Timedelta(days=1))]
    if cur.empty or prev.empty:
        return None
    label = (f"{cs:%b} 1–{last.day} vs {ps:%b} 1–{pe.day}" if partial else f"{cs:%b %Y} vs {ps:%b %Y}")
    return cur, prev, label, partial


def delta_html(cur, prev, fmt, better="up"):
    if cur is None or prev is None:
        return ""
    cur, prev = float(cur), float(prev)
    if fmt == "percent":
        diff = cur - prev
        good = (diff >= 0) == (better != "down")
        return f"<span class='{'kpi-up' if good else 'kpi-down'}'>{'▲' if diff >= 0 else '▼'} {abs(diff):.1f} pts</span>"
    if not prev:
        return ""
    pct = (cur - prev) / abs(prev) * 100
    good = (pct >= 0) == (better != "down")
    return f"<span class='{'kpi-up' if good else 'kpi-down'}'>{'▲' if pct >= 0 else '▼'} {abs(pct):.1f}%</span>"


def find_alerts(plan, periods, threshold=25.0):
    """SQL-computed: which breakdown groups moved most on the primary metric between the two periods."""
    pm = plan.get("primary_metric")
    if not pm or not periods:
        return []
    cur, prev, label, _ = periods
    alerts = []
    total_cur = sql_scalar(cur, f"SELECT {pm['expr']} AS value FROM data")
    total_prev = sql_scalar(prev, f"SELECT {pm['expr']} AS value FROM data")
    if total_cur is not None and total_prev:
        pct = (float(total_cur) - float(total_prev)) / abs(float(total_prev)) * 100
        if abs(pct) >= threshold * 0.6:
            alerts.append({"what": f"Total {pm['name']}", "change_pct": round(pct, 1),
                           "now": float(total_cur), "before": float(total_prev)})
    for col in plan.get("breakdowns", []):
        sql = f"SELECT {q(col)} AS grp, {pm['expr']} AS v FROM data WHERE {q(col)} IS NOT NULL GROUP BY 1"
        try:
            a, b = ai.run_sql(cur, sql), ai.run_sql(prev, sql)
        except Exception:
            continue
        m = a.merge(b, on="grp", how="outer", suffixes=("_now", "_before")).fillna(0)
        scale = max(float(m["v_before"].abs().sum()), 1e-9)
        for _, r in m.iterrows():
            if r["v_before"] and abs(r["v_before"]) / scale >= 0.05:
                pct = (r["v_now"] - r["v_before"]) / abs(r["v_before"]) * 100
                if abs(pct) >= threshold:
                    alerts.append({"what": f"{pm['name']} in {col} = {r['grp']}", "change_pct": round(float(pct), 1),
                                   "now": float(r["v_now"]), "before": float(r["v_before"])})
    alerts.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return alerts[:5]


@st.cache_data(ttl=3600, max_entries=64, show_spinner=False)
def compute_ai_view(plan_key: str, data_key: str, _plan: dict, _data: pd.DataFrame) -> dict:
    """All SQL + chart building for the AI dashboard, cached per (plan, data/filter state)."""
    plan, data = _plan, _data
    periods = compare_periods(data)
    cards, kpi_rows = [], []
    for k in plan.get("kpis", []):
        fmt = k.get("format", "number")
        try:
            v = sql_scalar(data, k["sql"])
            c_v = sql_scalar(periods[0], k["sql"]) if periods else None
            p_v = sql_scalar(periods[1], k["sql"]) if periods else None
        except Exception:
            v = c_v = p_v = None
        d_html = delta_html(c_v, p_v, fmt, str(k.get("better", "up")).lower())
        note = (f"{d_html} {periods[2]}" if d_html else k.get("note", ""))
        kpi_rows.append({"kpi": k.get("title"), "value": v, "latest_period": c_v, "previous_period": p_v, "format": fmt})
        cards.append(f'<div class="kpi-card"><div class="kpi-label">{k.get("title", "")}</div>'
                     f'<div class="kpi-value">{fmt_value(v, fmt)}</div><div class="kpi-note">{note}</div></div>')
    alerts = find_alerts(plan, periods)
    pm_fmt = (plan.get("primary_metric") or {}).get("format", "number")
    chips = "".join(
        f"<span class='alert-chip {'up' if a['change_pct'] >= 0 else 'down'}'>"
        f"{'▲' if a['change_pct'] >= 0 else '▼'} {a['what']}: {abs(a['change_pct']):.0f}% "
        f"({fmt_value(a['before'], pm_fmt)} → {fmt_value(a['now'], pm_fmt)})</span>" for a in alerts)
    figs, chart_results = [], []
    for c in plan.get("charts", []):
        try:
            res = ai.run_sql(data, c["sql"])
        except Exception:
            continue
        chart_results.append({"chart": c.get("title"), "rows": res.head(12).to_dict("records")})
        kind = c.get("chart", "auto")
        if kind == "pie" and res.shape[1] >= 2 and len(res) <= 8:
            fig = px.pie(res, names=res.columns[0], values=res.columns[1], hole=.55, title=c.get("title", ""))
            fig.update_traces(marker=dict(colors=["#98445f", "#bd7186", "#d99baa", "#e8c6ce", "#b9a2a9", "#7d3a50"]))
            fig = plot_theme(fig, 300)
        else:
            fig = result_chart(res, kind if kind in ("line", "bar", "table") else "auto", c.get("title", ""))
        figs.append((c, res, fig))
    max_date = f"{data['_date'].max():%d %b}" if "_date" in data.columns and data["_date"].notna().any() else ""
    return {"cards": cards, "kpi_rows": kpi_rows, "alerts": alerts, "chips": chips, "figs": figs,
            "chart_results": chart_results, "periods_label": periods[2] if periods else None,
            "partial": bool(periods[3]) if periods else False, "max_date": max_date}


def render_ai_dashboard(base: pd.DataFrame, data_name: str) -> None:
    prof_json = cached_profile(base)
    schema_sig = data_name + "|" + "|".join(f"{c}:{base[c].dtype}" for c in base.columns)
    with st.spinner("🤖 AI is studying the data and designing the dashboard…"):
        plan = cached_plan(schema_sig, prof_json, base)
    if not plan.get("kpis") and not plan.get("charts"):
        raise ValueError("the AI plan had no usable KPIs or charts")

    st.markdown(f"<div class='ai-banner'>🤖 <b>{plan.get('dataset_type', 'Dataset')}</b> · "
                f"{plan.get('summary', '')}</div>", unsafe_allow_html=True)

    # ---- filters chosen by the AI ----
    data = base
    fcols_names = plan.get("filters", [])
    has_date = "_date" in base.columns and base["_date"].notna().any()
    widgets = st.columns(max(1, len(fcols_names) + (1 if has_date else 0)))
    wi, active_filters = 0, {}
    if has_date:
        dmin, dmax = base["_date"].min().date(), base["_date"].max().date()
        rng = widgets[wi].date_input("Date range", value=(dmin, dmax), min_value=dmin, max_value=dmax,
                                     key=f"ai_date_{data_name}_{dmin}_{dmax}")
        wi += 1
        if isinstance(rng, tuple) and len(rng) == 2 and (rng[0], rng[1]) != (dmin, dmax):
            data = data[data["_date"].dt.date.between(rng[0], rng[1])]
            active_filters["date"] = f"{rng[0]} to {rng[1]}"
    for col in fcols_names:
        vals = sorted(base[col].dropna().astype(str).unique().tolist())
        picked = widgets[wi].multiselect(col, vals, default=[], placeholder="All", key=f"ai_f_{data_name}_{col}")
        wi += 1
        if picked:
            data = data[data[col].astype(str).isin(picked)]
            active_filters[col] = picked
    if data.empty:
        st.info("No rows match these filters.")
        return
    data_key = hashlib.md5(pd.util.hash_pandas_object(data, index=False).values.tobytes()).hexdigest()
    plan_key = hashlib.md5(json.dumps(plan, sort_keys=True, default=str).encode()).hexdigest()
    view = compute_ai_view(plan_key, data_key, plan, data)   # cached: clicks don't recompute SQL or charts
    periods_label, partial = view["periods_label"], view["partial"]

    # ---- KPI cards ----
    st.markdown('<div class="section-label">Performance at a glance</div>', unsafe_allow_html=True)
    if view["cards"]:
        for col, html in zip(st.columns(len(view["cards"])), view["cards"]):
            col.markdown(html, unsafe_allow_html=True)

    # ---- alerts ----
    if view["chips"]:
        st.markdown(f"<div class='section-label' style='margin-top:.9rem'>Changes worth a look · {periods_label}</div>"
                    f"<div>{view['chips']}</div>", unsafe_allow_html=True)

    # ---- charts ----
    st.markdown('<div class="section-label" style="margin-top:1rem">Performance analysis</div>', unsafe_allow_html=True)
    figs = view["figs"]

    def draw(c, res, fig, key):
        if fig is not None:
            st.plotly_chart(fig, width="stretch", theme=None, config={"displayModeBar": False}, key=key)
        else:
            st.markdown(f"**{c.get('title', '')}**")
            st.dataframe(res, width="stretch", hide_index=True, height=280)
        if c.get("why"):
            st.caption(c["why"])

    if partial:
        st.caption(f"Note: the latest month is incomplete (data through {view['max_date']}), "
                   "so monthly totals for it will look lower. KPI changes compare the same days of each month.")
    rest = figs
    if figs and figs[0][0].get("chart") == "line" and figs[0][2] is not None:  # main trend full width
        draw(*figs[0], key="ai_chart_0")
        rest = figs[1:]
    for i in range(0, len(rest), 2):
        cols = st.columns(2)
        for j, item in enumerate(rest[i:i + 2]):
            with cols[j]:
                draw(*item, key=f"ai_chart_{i + j + 1}")
    kpi_rows, alerts, chart_results = view["kpi_rows"], view["alerts"], view["chart_results"]
    periods = (None, None, periods_label, partial) if periods_label else None

    # ---- automatic AI insights (once per data version; on demand when filtered) ----
    st.markdown('<div class="section-label" style="margin-top:1rem">AI insights</div>', unsafe_allow_html=True)
    context = {"dataset": plan.get("dataset_type"), "rows": int(len(data)), "filters": active_filters,
               "comparison_period": periods[2] if periods else None,
               "latest_period_is_partial": bool(periods[3]) if periods else None,
               "kpis": kpi_rows, "alerts": alerts, "charts": chart_results}
    if has_date:
        context["date_range"] = f"{data['_date'].min():%Y-%m-%d} to {data['_date'].max():%Y-%m-%d}"
    auto = not active_filters
    if auto or st.button("🤖 Analyse this filtered view", key="ai_filtered_insights"):
        with st.spinner("Writing insights…"):
            try:
                with st.container(border=True):
                    st.markdown(cached_insights(data_key, json.dumps(context, default=str, sort_keys=True)))
            except Exception as e:
                st.warning(f"AI insights unavailable right now: {ai_error_message(e)}")
    else:
        st.caption("Filters are on. Click the button for insights on this filtered view.")

    # ---- transparency ----
    with st.expander("How the AI built this dashboard"):
        stats = plan.get("_stats", {})
        st.caption(f"Gemini designed this dashboard from the column names and a few example values. "
                   f"It wrote {stats.get('queries', 0)} SQL queries; {stats.get('repaired', 0)} were auto-repaired and "
                   f"{stats.get('dropped', 0)} dropped because they didn't run. Every number above comes from SQL. "
                   f"The design is reused until the columns change; numbers and insights refresh with the data.")
        for k in plan.get("kpis", []):
            st.markdown(f"**KPI: {k.get('title')}**" + (" (auto-repaired)" if k.get("repaired") else ""))
            st.code(k["sql"], language="sql")
        for c in plan.get("charts", []):
            st.markdown(f"**Chart: {c.get('title')}**" + (" (auto-repaired)" if c.get("repaired") else ""))
            st.code(c["sql"], language="sql")
        if st.button("Redesign dashboard with AI", key="ai_redesign"):
            cached_plan.clear()
            st.rerun()
    with st.expander("Explore underlying data"):
        st.dataframe(data.drop(columns=["_date"], errors="ignore").head(200), width="stretch", height=300, hide_index=True)
    with st.expander("Pipeline status"):
        log = pd.DataFrame(pipeline_log)
        for c in ("numeric_converted", "empty_columns_dropped"):
            if c in log:
                log[c] = log[c].apply(lambda v: ", ".join(v) if isinstance(v, list) else "")
        st.dataframe(log, width="stretch", hide_index=True)
        if active_name in cleaning_sql:
            st.markdown("**Cleaning SQL**")
            st.code(cleaning_sql[active_name], language="sql")
    st.caption(f"Showing {len(data):,} of {len(base):,} rows · AI-designed dashboard · numbers calculated with SQL.")


tab_dash, tab_ask, tab_suggest = st.tabs(["📊 Dashboard", "💬 Ask your data", "✨ AI-suggested analyses"])

# The AI tabs work on the full dataset (with a typed "_date" column for time questions).
ai_df = raw_df.assign(_date=date_all) if date_all is not None else raw_df
AI_PRIVACY = ("The AI sees column names, types and up to 3 example values per column, never the full dataset. "
              "It writes SQL; the numbers come from running that SQL on your data.")


def ai_off_message():
    st.info("Add `GEMINI_API_KEY` to the app's Secrets to switch on the AI features.")


with tab_ask:
    st.markdown("### Ask a question about your data")
    st.caption(AI_PRIVACY)
    if not AI_ON:
        ai_off_message()
    else:
        examples = ["Which region had the highest revenue?", "Show monthly revenue for the last 6 months",
                    "Top 5 products by profit margin", "How many orders per channel?"]
        ex_cols = st.columns(len(examples))
        for i, ex in enumerate(examples):
            if ex_cols[i].button(ex, key=f"ex_{i}", width="stretch"):
                st.session_state["ask_q"] = ex
        with st.form("ask_form", clear_on_submit=False, border=False):
            question = st.text_input("Your question", key="ask_q", placeholder="e.g. Which product sold the most units in March?")
            asked = st.form_submit_button("Ask", type="primary")
        if asked and question.strip():
            prof = cached_profile(ai_df)
            with st.spinner("Writing and running SQL…"):
                try:
                    out = ask_with_retry(question.strip(), prof, ai_df)
                    if out.get("sql") and out.get("result") is not None:
                        try:
                            out["answer"] = cached_answer(question.strip(), out["result"].head(25).to_csv(index=False))
                        except Exception:
                            out["answer"] = ""
                    st.session_state["ask_out"] = {"q": question.strip(), **out}
                except Exception as e:
                    st.session_state["ask_out"] = {"q": question.strip(), "error": ai_error_message(e)}
        out = st.session_state.get("ask_out")
        if out:
            st.markdown(f"**Q: {out['q']}**")
            if out.get("error"):
                st.error(f"Couldn't answer that: {out['error'][:300]}")
            elif not out.get("sql"):
                st.warning(out.get("explanation", "The data can't answer that question."))
            else:
                if out.get("answer"):
                    st.success(out["answer"])
                res = out["result"]
                fig = result_chart(res)
                left, right = st.columns([1.3, 1]) if fig is not None else (st.container(), None)
                if fig is not None:
                    with left:
                        st.plotly_chart(fig, width="stretch", theme=None, config={"displayModeBar": False})
                    with right:
                        st.dataframe(res, width="stretch", hide_index=True, height=300)
                else:
                    st.dataframe(res, width="stretch", hide_index=True)
                with st.expander("SQL used"):
                    st.caption(out.get("explanation", ""))
                    st.code(out["sql"], language="sql")

with tab_suggest:
    st.markdown("### Analyses the AI recommends for this dataset")
    st.caption(AI_PRIVACY)
    if not AI_ON:
        ai_off_message()
    else:
        prof = cached_profile(ai_df)
        sugg_key = f"sugg_{hashlib.md5(prof.encode()).hexdigest()}"
        if st.button("Generate suggestions" if sugg_key not in st.session_state else "Regenerate", type="primary"):
            if sugg_key in st.session_state:
                cached_suggestions.clear()
            with st.spinner("Studying the dataset…"):
                try:
                    st.session_state[sugg_key] = cached_suggestions(prof)
                except Exception as e:
                    st.session_state[sugg_key] = {"error": ai_error_message(e)}
        plan = st.session_state.get(sugg_key)
        if plan:
            if plan.get("error"):
                st.error(f"AI suggestions failed: {plan['error'][:300]}")
            else:
                if plan.get("summary"):
                    st.info(plan["summary"])
                items = plan.get("analyses", [])[:6]
                for i in range(0, len(items), 2):
                    cols = st.columns(2)
                    for j, item in enumerate(items[i:i + 2]):
                        with cols[j]:
                            st.markdown(f"**{item.get('title', 'Analysis')}**")
                            st.caption(item.get("why", ""))
                            try:
                                res = ai.run_sql(ai_df, item.get("sql", ""))
                                fig = result_chart(res, item.get("chart", "auto"))
                                if fig is not None:
                                    st.plotly_chart(fig, width="stretch", theme=None, config={"displayModeBar": False},
                                                    key=f"sugg_chart_{i + j}")
                                else:
                                    st.dataframe(res, width="stretch", hide_index=True, height=260)
                            except Exception as e:
                                st.warning(f"This analysis couldn't run on the data: {str(e)[:120]}")
                            with st.expander("SQL"):
                                st.code(item.get("sql", ""), language="sql")
        else:
            st.write("Click **Generate suggestions** and the AI will propose the most useful analyses for this "
                     "dataset, each backed by a SQL query that runs on your real data.")

with tab_dash:
    if AI_ON:
        mode = st.radio("Dashboard mode", ["🤖 AI-designed", "Standard"], horizontal=True,
                        label_visibility="collapsed", key="dash_mode")
        if mode == "🤖 AI-designed":
            try:
                render_ai_dashboard(ai_df, active_name)
                st.stop()
            except Exception as e:
                if type(e).__name__ in ("StopException", "RerunException"):
                    raise
                st.warning(f"The AI dashboard couldn't be built: {ai_error_message(e)} Showing the standard dashboard instead.")
    available_objectives = []
    for name in OBJECTIVES:
        if name == "Sales performance" and mapping.get("revenue"): available_objectives.append(name)
        elif name == "Profitability" and mapping.get("revenue") and mapping.get("cost"): available_objectives.append(name)
        elif name == "Customer analysis" and mapping.get("customer_id"): available_objectives.append(name)
        elif name == "Marketing performance" and mapping.get("marketing_spend"): available_objectives.append(name)
        elif name == "Campaign funnel" and mapping.get("impressions") and mapping.get("clicks"): available_objectives.append(name)
        elif name == "Product / regional analysis" and (mapping.get("product") or mapping.get("region")): available_objectives.append(name)

    if not available_objectives:
        st.warning("No standard objectives can be calculated yet. Open Analysis → Review column mapping in the sidebar.")
        st.stop()

    chosen = st.multiselect(
        "Choose business objectives",
        available_objectives,
        default=available_objectives[:2] if len(available_objectives) > 1 else available_objectives,
        key=f"objective_select_{active_name}",
        help="Selected objectives stay visible here. The dashboard updates to show relevant metrics and charts.",
    )
    if not chosen:
        st.info("Select at least one objective to populate the dashboard.")
        st.stop()

    # Compact filter row. Only dimensions that exist in this dataset are shown.
    df = raw_df.copy()
    filter_cols = [c for c in [mapping.get("region"), mapping.get("product"), mapping.get("channel"),
                               "Source" if "Source" in df.columns and active_name == "All sources (combined)" else None]
                   if c and c in df.columns and df[c].nunique(dropna=True) <= 80]
    filter_cols = list(dict.fromkeys(filter_cols))
    date_series = parse_date(df, mapping.get("date"))
    fcols = st.columns(1 + len(filter_cols) + (1 if date_series is not None else 0))
    ci = 0
    if date_series is not None:
        dates = date_series.dropna()
        if not dates.empty:
            min_d, max_d = dates.min().date(), dates.max().date()
            selected_range = fcols[ci].date_input("Date range", value=(min_d, max_d), min_value=min_d, max_value=max_d,
                                                  key=f"date_{active_name}_{min_d}_{max_d}")
            ci += 1
            if isinstance(selected_range, tuple) and len(selected_range) == 2:
                df = df.loc[date_series.dt.date.between(selected_range[0], selected_range[1])]
    for col in filter_cols:
        vals = sorted(df[col].dropna().astype(str).unique().tolist())
        picked = fcols[ci].multiselect(str(col), vals, default=[], placeholder="All", key=f"f_{active_name}_{col}")
        ci += 1
        if picked: df = df[df[col].astype(str).isin(picked)]

    date_filtered = parse_date(df, mapping.get("date"))
    df = df.assign(_date=date_filtered) if date_filtered is not None else df  # typed date column for SQL
    st.markdown('<div class="section-label">Performance at a glance</div>', unsafe_allow_html=True)
    sql_log = {}
    metrics, sql_log["Headline KPIs"] = build_metrics(df, mapping)

    # Latest month vs previous month (historical comparison)
    mrev, month_sql = monthly_revenue(df, mapping)
    if month_sql: sql_log["Monthly revenue"] = month_sql
    if mrev is not None and len(mrev) >= 2 and "Total revenue" in metrics:
        last, prev = float(mrev.iloc[-1]), float(mrev.iloc[-2])
        if prev:
            pct = (last - prev) / prev * 100
            cls, arrow = ("kpi-up", "▲") if pct >= 0 else ("kpi-down", "▼")
            metrics["Total revenue"] = (metrics["Total revenue"][0],
                                        f"<span class='{cls}'>{arrow} {abs(pct):.1f}%</span> {mrev.index[-1]} vs {mrev.index[-2]}")

    objective_metric_order = {
        "Sales performance": ["Total revenue", "Average order value", "Units sold"],
        "Profitability": ["Gross profit", "Gross margin"],
        "Customer analysis": ["Unique customers", "Revenue per customer"],
        "Marketing performance": ["Marketing spend", "ROAS proxy", "Cost per conversion"],
        "Campaign funnel": ["Click-through rate", "Click-to-conversion"],
        "Product / regional analysis": ["Units sold", "Total revenue"],
    }
    wanted = []
    for objective in chosen:
        for metric in objective_metric_order[objective]:
            if metric in metrics and metric not in wanted: wanted.append(metric)
    if not wanted: wanted = list(metrics)[:6]
    wanted = wanted[:6]
    if wanted:
        cards = st.columns(len(wanted))
        for col, name in zip(cards, wanted):
            value, note = metrics[name]
            col.markdown(f'<div class="kpi-card"><div class="kpi-label">{name}</div><div class="kpi-value">{value}</div><div class="kpi-note">{note}</div></div>', unsafe_allow_html=True)
    else:
        st.info("The selected objectives do not have enough mapped fields to calculate KPIs.")

    # Charts are selected automatically from the objective + available columns.
    st.markdown('<div class="section-label" style="margin-top:1rem">Performance analysis</div>', unsafe_allow_html=True)
    chart_specs = []
    rev_col, cost_col = mapping.get("revenue"), mapping.get("cost")
    dim_candidates = []
    for role in ["product", "region", "channel"]:
        c = mapping.get(role)
        if c and c in df.columns and 1 < df[c].nunique(dropna=True) <= 40 and c not in dim_candidates:
            dim_candidates.append(c)
    if mrev is not None and "Sales performance" in chosen:
        trend = pd.DataFrame({"_date": mrev.index.to_timestamp(), "_revenue": mrev.values})
        fig = px.line(trend, x="_date", y="_revenue", markers=True, title="Revenue trend",
                      labels={"_date": "Month", "_revenue": "Revenue"})
        fig.update_traces(line=dict(color="#a64f68", width=2.5), marker=dict(size=5))
        chart_specs.append(("wide", plot_theme(fig, 260)))
    if rev_col and rev_col in df.columns and dim_candidates and any(o in chosen for o in ["Sales performance", "Product / regional analysis", "Profitability"]):
        dcol = dim_candidates[0]
        tmp, sql_log[f"Revenue by {dcol}"] = breakdown_with_sql(
            df, q(dcol), {"_metric": f"SUM(TRY_CAST({q(rev_col)} AS DOUBLE))"}, order_by='"_metric" DESC', limit=10)
        tmp = tmp.rename(columns={"grp": dcol})
        tmp[dcol] = tmp[dcol].astype(str)
        fig = px.bar(tmp, x="_metric", y=dcol, orientation="h", title=f"Revenue by {dcol}", text_auto=".2s", labels={"_metric": "Revenue"})
        fig.update_traces(marker_color="#b8667d", textposition="outside", cliponaxis=False)
        fig.update_layout(yaxis=dict(categoryorder="total ascending"))
        chart_specs.append(("half", plot_theme(fig, 260)))
    if rev_col and cost_col and rev_col in df.columns and cost_col in df.columns and "Profitability" in chosen:
        tmp = df.assign(Revenue=pd.to_numeric(df[rev_col], errors="coerce").fillna(0), Cost=pd.to_numeric(df[cost_col], errors="coerce").fillna(0))
        if dim_candidates:
            tmp = tmp.groupby(dim_candidates[0], as_index=False)[["Revenue", "Cost"]].sum().sort_values("Revenue", ascending=False).head(10)
            fig = px.bar(tmp, x=dim_candidates[0], y=["Revenue", "Cost"], barmode="group", title="Revenue vs cost",
                         color_discrete_sequence=["#b8667d", "#d9b2bc"])
        else:
            fig = go.Figure(data=[go.Bar(name="Revenue", x=["Total"], y=[tmp.Revenue.sum()], marker_color="#b8667d"),
                                  go.Bar(name="Cost", x=["Total"], y=[tmp.Cost.sum()], marker_color="#d9b2bc")])
            fig.update_layout(barmode="group", title="Revenue vs cost")
        chart_specs.append(("half", plot_theme(fig, 260)))
    if mapping.get("customer_id") and mapping["customer_id"] in df.columns and "Customer analysis" in chosen:
        cust = df.groupby(mapping["customer_id"]).size().reset_index(name="Records").sort_values("Records", ascending=False).head(8)
        cust[mapping["customer_id"]] = cust[mapping["customer_id"]].astype(str)
        fig = px.bar(cust, x="Records", y=mapping["customer_id"], orientation="h", title="Top customers by records")
        fig.update_traces(marker_color="#c58a9a")
        fig.update_layout(yaxis=dict(categoryorder="total ascending", type="category"))
        chart_specs.append(("half", plot_theme(fig, 250)))
    if mapping.get("channel") and mapping["channel"] in df.columns and "Marketing performance" in chosen:
        measure = mapping.get("marketing_spend")
        if measure and measure in df.columns:
            tmp = df.assign(_spend=pd.to_numeric(df[measure], errors="coerce")).groupby(mapping["channel"], as_index=False)["_spend"].sum().sort_values("_spend", ascending=False).head(8)
            fig = px.pie(tmp, names=mapping["channel"], values="_spend", hole=.58, title="Marketing spend by channel")
            fig.update_traces(marker=dict(colors=["#98445f", "#bd7186", "#d99baa", "#e8c6ce", "#b9a2a9"]))
            chart_specs.append(("half", plot_theme(fig, 250)))
    if mapping.get("impressions") and mapping.get("clicks") and "Campaign funnel" in chosen:
        imp = float(pd.to_numeric(df[mapping["impressions"]], errors="coerce").sum())
        clk = float(pd.to_numeric(df[mapping["clicks"]], errors="coerce").sum())
        fig = go.Figure(go.Funnel(y=["Impressions", "Clicks"], x=[imp, clk], textinfo="value+percent initial", marker=dict(color=["#d9a3b0", "#a64f68"])))
        fig.update_layout(title="Campaign funnel")
        chart_specs.append(("half", plot_theme(fig, 250)))

    if chart_specs:
        if chart_specs[0][0] == "wide":
            st.plotly_chart(chart_specs[0][1], width="stretch", theme=None, config={"displayModeBar": False})
            rest = chart_specs[1:]
        else:
            rest = chart_specs
        for i in range(0, len(rest), 2):
            cols = st.columns(2)
            for j, item in enumerate(rest[i:i + 2]):
                with cols[j]:
                    st.plotly_chart(item[1], width="stretch", theme=None, config={"displayModeBar": False})
    else:
        st.info("More charts will appear when the selected objectives have suitable date, category or numeric fields.")

    # ----------------------------- AI analysis (Gemini) -----------------------------
    with st.expander("AI analysis", expanded=False):
        if not get_secret("GEMINI_API_KEY"):
            st.write("Add `GEMINI_API_KEY` to the app's secrets to enable AI-written insights. "
                     "The key stays on the server; viewers never see it.")
        else:
            summary = {
                "dataset": active_name, "rows_analysed": int(len(df)), "objectives": chosen,
                "kpis": {k: re.sub(r"<[^>]+>", "", f"{v} ({n})") for k, (v, n) in metrics.items()},
            }
            if date_filtered is not None and date_filtered.notna().any():
                summary["date_range"] = f"{date_filtered.min():%Y-%m-%d} to {date_filtered.max():%Y-%m-%d}"
            if mrev is not None:
                summary["monthly_revenue_last_12"] = {str(k): round(float(v), 2) for k, v in mrev.tail(12).items()}
            if rev_col and rev_col in df.columns:
                for dcol in dim_candidates[:3]:
                    top = df.assign(_r=pd.to_numeric(df[rev_col], errors="coerce")).groupby(dcol)["_r"].sum().sort_values(ascending=False)
                    summary[f"revenue_by_{dcol}_top5"] = {str(k): round(float(v), 2) for k, v in top.head(5).items()}
                    summary[f"revenue_by_{dcol}_bottom3"] = {str(k): round(float(v), 2) for k, v in top.tail(3).items()}
            st.caption("Only the aggregated figures shown on this page are sent to the model; no row-level records.")
            if st.button("Generate AI insights", key=f"ai_{active_name}"):
                with st.spinner("Analysing the latest numbers…"):
                    try:
                        st.session_state[f"ai_text_{active_name}"] = gemini_insights(json.dumps(summary, sort_keys=True), GEMINI_MODEL)
                    except Exception as e:
                        st.session_state[f"ai_text_{active_name}"] = f"⚠️ AI analysis failed: {ai_error_message(e)}"
            if st.session_state.get(f"ai_text_{active_name}"):
                st.markdown(st.session_state[f"ai_text_{active_name}"])

    # ----------------------------- Data, quality and pipeline status -----------------------------
    with st.expander("Explore underlying data and data quality"):
        left, right = st.columns([1.5, 1])
        with left:
            st.markdown("**Filtered records**")
            st.dataframe(df.drop(columns=["_date"], errors="ignore").head(100), width="stretch", height=300, hide_index=True)
        with right:
            qdf = df.drop(columns=["_date"], errors="ignore")
            quality = pd.DataFrame({"Column": qdf.columns.astype(str), "Type": [str(qdf[c].dtype) for c in qdf.columns],
                                    "Missing": [int(qdf[c].isna().sum()) for c in qdf.columns],
                                    "Unique": [int(qdf[c].nunique(dropna=True)) for c in qdf.columns]})
            st.markdown("**Data quality**")
            st.dataframe(quality, width="stretch", height=300, hide_index=True)

    with st.expander("SQL behind these numbers"):
        st.caption("Cleaning and KPI calculations run as SQL in an embedded DuckDB engine on every refresh. "
                   "The live queries for the current view are shown below.")
        src_names = [n for n in cleaning_sql if n == active_name] or list(cleaning_sql)[:1]
        for n in src_names:
            st.markdown(f"**Cleaning: {n}**")
            st.code(cleaning_sql[n], language="sql")
        for title, sql in sql_log.items():
            st.markdown(f"**{title}**")
            st.code(sql, language="sql")

    with st.expander("Pipeline status"):
        if pipeline_log:
            log = pd.DataFrame(pipeline_log)
            if "numeric_converted" in log:
                log["numeric_converted"] = log["numeric_converted"].apply(lambda v: ", ".join(v) if isinstance(v, list) else "")
            if "empty_columns_dropped" in log:
                log["empty_columns_dropped"] = log["empty_columns_dropped"].apply(lambda v: ", ".join(v) if isinstance(v, list) else "")
            log = log.rename(columns={"empty_columns_dropped": "Empty columns dropped", "rows_in": "Rows in", "rows_out": "Rows out", "empty_rows_removed": "Empty rows removed",
                                      "duplicates_removed": "Duplicates removed", "columns": "Columns",
                                      "numeric_converted": "Text→number columns"})
            st.dataframe(log, width="stretch", hide_index=True)
        st.caption("Each source is fetched, validated and cleaned automatically. " +
                   (f"Connected sources are re-checked every {REFRESH_MINUTES} minutes." if AUTO_REFRESH
                    else "Connected sources are re-read every time the dashboard is opened, or when Refresh now is pressed."))

    st.caption(f"Showing {len(df):,} of {len(raw_df):,} rows · {len(chosen)} objective(s) selected · Metrics are calculated from the currently filtered dataset.")
