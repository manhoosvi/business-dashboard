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
GEMINI_MODEL = str(get_secret("GEMINI_MODEL", "gemini-2.5-flash"))


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
.block-container {padding-top:1rem; padding-bottom:1.1rem; max-width:1600px;}
[data-testid="stSidebar"] {background:#25232b; border-right:1px solid #393640;}
[data-testid="stSidebar"] * {color:#f5f3f5 !important;}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {color:#c9c5cc !important;}
[data-testid="stSidebar"] [data-baseweb="select"] div, [data-testid="stSidebar"] [data-baseweb="input"] input {color:#202124 !important;}
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
[data-testid="stPlotlyChart"] {border:1px solid #e5e7eb;border-radius:10px;padding:4px;background:#fff;}
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
    if df is None:
        return {}
    normalized = {norm(c): str(c) for c in df.columns}
    out = {}
    for role, aliases in ROLE_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                out[role] = normalized[alias]
                break
        if role not in out:
            for n, original in normalized.items():
                if any(a in n for a in aliases):
                    out[role] = original
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


@st.cache_data(ttl=FETCH_TTL, show_spinner=False, max_entries=200)
def fetch_source(location: str, visit_token: str = "") -> dict:
    """Download the raw bytes of a source. Errors are not cached, so failures retry next cycle."""
    if os.path.exists(location):  # local path (useful for testing)
        with open(location, "rb") as fh:
            data = fh.read()
    else:
        r = requests.get(to_fetch_url(location), timeout=30)
        r.raise_for_status()
        if "text/html" in r.headers.get("content-type", "").lower():
            raise ValueError("The link returned a web page instead of data. Share the Sheet/file as "
                             "'Anyone with the link – Viewer', or use File → Share → Publish to web → CSV.")
        data = r.content
    return {"bytes": data, "hash": hashlib.md5(data).hexdigest(),
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


def parse_date(df, col):
    if not col or col not in df.columns: return None
    try:
        parsed = pd.to_datetime(df[col], errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(df[col], errors="coerce")
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
    fig.update_layout(
        height=height, margin=dict(l=12, r=12, t=38, b=12),
        paper_bgcolor="white", plot_bgcolor="white",
        font=dict(family="Arial", size=11, color="#514a50"),
        title=dict(font=dict(size=13, color="#3d353b"), x=0.02),
        legend=dict(orientation="h", y=1.12, x=0),
    )
    fig.update_xaxes(showgrid=False, linecolor="#eee8eb")
    fig.update_yaxes(gridcolor="#f1edef", zeroline=False)
    return fig


@st.cache_data(ttl=86400, show_spinner=False)
def gemini_insights(summary_json: str, model: str) -> str:
    from google import genai  # imported lazily so the app runs without the package
    key = str(get_secret("GEMINI_API_KEY")).strip()
    # AI Studio keys usually start with "AIza"; Vertex AI (express mode) keys start with "AQ.".
    use_vertex = str(get_secret("GEMINI_USE_VERTEX", "")).lower() in ("1", "true", "yes") or key.startswith("AQ.")
    prompt = (
        "You are a business analyst writing for a team dashboard. Using ONLY the aggregated figures in "
        "the JSON below, write concise markdown with three sections: **Key observations** (3-5 bullets "
        "citing numbers), **Watch-outs** (1-3 bullets), **Recommended actions** (3 bullets). Do not invent "
        "figures, do not claim causes the data cannot show, and say so if the data is too thin.\n\n"
        f"```json\n{summary_json}\n```"
    )
    try:
        client = genai.Client(vertexai=True, api_key=key) if use_vertex else genai.Client(api_key=key)
        resp = client.models.generate_content(model=model, contents=prompt)
    except Exception:
        # wrong guess about the key type: try the other Gemini endpoint once
        client = genai.Client(api_key=key) if use_vertex else genai.Client(vertexai=True, api_key=key)
        resp = client.models.generate_content(model=model, contents=prompt)
    return (resp.text or "").strip()


# ----------------------------- Run the ingestion pipeline -----------------------------
# A new visit (new browser session) gets a new token, so its first load always pulls fresh data.
# Within that visit, filters/clicks reuse the same data until "Refresh now" is pressed.
if "visit_token" not in st.session_state:
    st.session_state["visit_token"] = datetime.now().isoformat()
VISIT = "" if AUTO_REFRESH else st.session_state["visit_token"]
sources = configured_sources()
datasets, pipeline_log, signature, cleaning_sql = {}, [], [], {}
for name, loc in sources.items():
    try:
        raw = fetch_source(loc, VISIT)
        clean, rep, clean_sql = process_bytes(raw["hash"], raw["bytes"], loc)
        cleaning_sql[name] = clean_sql
        signature.append(raw["hash"])
        if clean.empty:
            raise ValueError("Source contains no data rows")
        datasets[name] = clean
        pipeline_log.append({"Source": name, "Type": "Connected", "Status": "✅ OK", "Last fetched": raw["fetched_at"], **rep})
    except Exception as e:
        signature.append(f"error:{name}")
        pipeline_log.append({"Source": name, "Type": "Connected", "Status": f"⚠️ {str(e)[:140]}", "Last fetched": "—"})
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
    if sources:
        for row in pipeline_log:
            icon = "🟢" if row["Status"].startswith("✅") else "🟠"
            st.markdown(f"<div class='src-row'>{icon} {row['Source']}</div>", unsafe_allow_html=True)
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
        st.caption("No connected sources yet. Add Google Sheet / Drive links under [data_sources] in the app's secrets.")

    uploads = st.file_uploader("Add CSV / Excel (this session only)", type=["csv", "xlsx", "xls"], accept_multiple_files=True)
    for f in uploads or []:
        try:
            data = f.getvalue()
            clean, rep, clean_sql = process_bytes(hashlib.md5(data).hexdigest(), data, f.name)
            cleaning_sql[f"{f.name} (upload)"] = clean_sql
            if not clean.empty:
                datasets[f"{f.name} (upload)"] = clean
            pipeline_log.append({"Source": f.name, "Type": "Upload", "Status": "✅ OK",
                                 "Last fetched": datetime.now(TZ).strftime("%d %b %Y, %H:%M"), **rep})
        except Exception as e:
            st.error(f"{f.name}: {e}")
    st.divider()

    st.markdown("<div style='font-size:.68rem;font-weight:800;letter-spacing:.1em;color:#c9c5cc;margin:.35rem 0 .45rem'>DATA SOURCES</div>", unsafe_allow_html=True)
    view = st.radio("Choose a workspace", ["Overview", "X analytics", "Discord analytics"], label_visibility="collapsed")
    x_ready = bool(get_secret("X_BEARER_TOKEN", ""))
    discord_ready = bool(get_secret("DISCORD_BOT_TOKEN", ""))
    st.caption(f"X API: {'Credentials detected' if x_ready else 'Setup required'}")
    st.caption(f"Discord bot: {'Credentials detected' if discord_ready else 'Setup required'}")
    st.divider()

    st.markdown("<div style='font-size:.68rem;font-weight:800;letter-spacing:.1em;color:#c9c5cc;margin:.35rem 0 .45rem'>ANALYSIS</div>", unsafe_allow_html=True)
    # Offer a combined view when several sources share the same columns (e.g. monthly files)
    if len(datasets) >= 2:
        col_sets = {frozenset(map(norm, d.columns)) for d in datasets.values()}
        if len(col_sets) == 1:
            combined = pd.concat([d.assign(Source=n) for n, d in datasets.items()], ignore_index=True)
            datasets = {"All sources (combined)": combined, **datasets}

    active_name, raw_df, mapping = None, None, {}
    if datasets:
        active_name = st.selectbox("Dataset", list(datasets.keys()))
        raw_df = datasets[active_name]
        auto = suggest_mapping(raw_df)
        with st.expander("Review column mapping", expanded=False):
            for role in ROLE_ALIASES:
                opts = ["Not mapped"] + list(map(str, raw_df.columns))
                default = auto.get(role, "Not mapped")
                idx = opts.index(default) if default in opts else 0
                val = st.selectbox(role.replace("_", " ").title(), opts, index=idx, key=f"map_{active_name}_{role}")
                if val != "Not mapped":
                    mapping[role] = val
        st.caption(f"{len(raw_df):,} rows · {len(raw_df.columns)} columns")
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
        st.write("Add Google Sheet or Drive links to the app's secrets under `[data_sources]`. "
                 "The dashboard re-reads them automatically, so when someone updates the sheet, everyone sees the new numbers.")
    with right:
        st.markdown("**One-off uploads**")
        st.write("Upload CSV / Excel files in the sidebar for a quick look. Uploads are only visible in your own session.")
    if any(not r["Status"].startswith("✅") for r in pipeline_log):
        st.warning("Some connected sources could not be read:")
        st.dataframe(pd.DataFrame(pipeline_log)[["Source", "Status"]], hide_index=True, width="stretch")
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
        st.plotly_chart(chart_specs[0][1], width="stretch", config={"displayModeBar": False})
        rest = chart_specs[1:]
    else:
        rest = chart_specs
    for i in range(0, len(rest), 2):
        cols = st.columns(2)
        for j, item in enumerate(rest[i:i + 2]):
            with cols[j]:
                st.plotly_chart(item[1], width="stretch", config={"displayModeBar": False})
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
                    st.session_state[f"ai_text_{active_name}"] = f"⚠️ AI analysis failed: {e}"
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
