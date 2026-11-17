"""
AI layer (Gemini). Three jobs:
  1. ai_column_mapping  - work out which column is revenue / date / region ... when name rules fail
  2. text_to_sql        - turn a plain-English question into DuckDB SQL (numbers still come from SQL)
  3. suggest_analyses   - recommend analyses for a new dataset, each backed by a SQL query

AI never calculates numbers itself: it writes SQL, the SQL runs on the real data in a locked-down
DuckDB connection (read-only, no file/network access), and results are shown with the query.
"""
import json
import re

import duckdb
import pandas as pd

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|install|load|export|import|"
    r"call|set|reset|checkpoint|vacuum|use|truncate|grant|revoke)\b|\bread_\w+\s*\(|\bglob\s*\(|getenv|https?:",
    re.IGNORECASE,
)


# ----------------------------- Gemini call -----------------------------
USE_VERTEX = False  # set True only for Google Cloud Vertex AI keys (the app reads GEMINI_USE_VERTEX from Secrets)
RESOLVED_MODEL = {}  # requested model -> model that actually worked for this key (remembered while the app runs)


def _flash_candidates(client) -> list:
    """Ask Google which models this key can use; prefer the newest stable 'gemini-X.Y-flash'."""
    names = []
    try:
        for m in client.models.list():
            name = (m.name or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            names.append(name)
    except Exception:
        pass
    ver = lambda n: tuple(int(x) for x in re.findall(r"\d+", n.split("-")[1])) if len(n.split("-")) > 1 else (0,)
    stable_flash = sorted([n for n in names if re.fullmatch(r"gemini-\d+(\.\d+)?-flash", n)], key=ver, reverse=True)
    lite = sorted([n for n in names if re.fullmatch(r"gemini-\d+(\.\d+)?-flash-lite", n)], key=ver, reverse=True)
    return ["gemini-flash-latest"] + stable_flash + lite + ["gemini-flash-lite-latest"]


def _model_unavailable(err: Exception) -> bool:
    low = str(err).lower()
    return ("404" in low or "not_found" in low or "no longer available" in low or "not supported" in low
            or ("429" in low and "limit: 0" in low))


def _transient(err: Exception) -> bool:
    """Temporary Google-side problems worth retrying / switching model for: 503 overloaded, 5xx, timeouts.
    Uses the HTTP status code when the SDK gives one, so a 403/400 (permissions, billing, bad key) is never retried."""
    code = getattr(err, "code", None)
    if isinstance(code, int):
        return code in (500, 502, 503, 504)
    low = (str(err) + " " + type(err).__name__).lower()
    return bool(re.search(r"\b(500|502|503|504)\b", low)) or any(
        t in low for t in ("overloaded", "high demand", "deadline", "timeout", "timed out", "connecterror"))


TIMEOUT_MS = 30_000     # max wait for ONE request to Gemini (a hung request used to spin forever)
TOTAL_BUDGET_S = 80     # give up after this long in total so the app can fall back to the Standard dashboard
MAX_FALLBACK_MODELS = 3


def _gemini_generate(prompt: str, key: str, model: str, json_mode: bool = False) -> str:
    """Call Gemini through the standard Gemini API. Works with both AI Studio key formats
    ('AIza...' and the newer 'AQ.Ab...').
    - Every request has a timeout, so a stuck Google server can't freeze the app.
    - A temporary error (503 overloaded, 5xx, timeout) is retried once, then other current Flash models are tried.
    - A retired model (404) is replaced by a current one for as long as the app runs."""
    import time

    from google import genai
    from google.genai import types

    cfg = {"response_mime_type": "application/json"} if json_mode else None
    key = key.strip().strip('"').strip("'")
    http = types.HttpOptions(timeout=TIMEOUT_MS)
    client = (genai.Client(vertexai=True, api_key=key, http_options=http) if USE_VERTEX
              else genai.Client(api_key=key, http_options=http))
    deadline = time.monotonic() + TOTAL_BUDGET_S

    def call(m):
        # Ask for quick answers (low "thinking") where the model supports it: much faster responses.
        fast = dict(cfg or {})
        fast["thinking_config"] = {"thinking_level": "low"} if not m.startswith("gemini-2") else {"thinking_budget": 0}
        try:
            return (client.models.generate_content(model=m, contents=prompt, config=fast).text or "").strip()
        except Exception as err:
            low = str(err).lower()
            if _transient(err) or not any(t in low for t in ("thinking", "validation", "extra", "unknown")):
                raise
        return (client.models.generate_content(model=m, contents=prompt, config=cfg).text or "").strip()

    def attempt(m):
        """One model, with a single quick retry on a temporary error."""
        try:
            return call(m)
        except Exception as err:
            if _transient(err) and time.monotonic() + 2 + TIMEOUT_MS / 1000 < deadline:
                time.sleep(2)
                return call(m)
            raise

    first = RESOLVED_MODEL.get(model, model)
    try:
        return attempt(first)
    except Exception as err:
        if not (_model_unavailable(err) or _transient(err)):
            raise
        last, retired = err, _model_unavailable(err)

    tried = [first]
    for cand in _flash_candidates(client):
        if cand in tried:
            continue
        if len(tried) > MAX_FALLBACK_MODELS or time.monotonic() + TIMEOUT_MS / 1000 > deadline:
            break
        tried.append(cand)
        try:
            out = call(cand)
            if retired:  # only remember the switch when the old model is gone, not when it was merely busy
                RESOLVED_MODEL[model] = cand
            return out
        except Exception as err:
            last = err
            if not (_model_unavailable(err) or _transient(err)):
                raise
    raise RuntimeError(f"Gemini is temporarily unavailable ({str(last)[:140]}). Models tried: {', '.join(tried)}.") from last


# ----------------------------- Backup provider (Groq) + the one entry point -----------------------------
GROQ_KEY = ""                          # set by the app from Secrets (GROQ_API_KEY); optional
GROQ_MODEL = "llama-3.3-70b-versatile"  # override with GROQ_MODEL in Secrets; model names change, check console.groq.com
_GEMINI_BLOCKED_UNTIL = 0.0            # after a permission/billing error, skip Gemini for a while (only if Groq is available)


def _permanent(err: Exception) -> bool:
    low = str(err).lower()
    return getattr(err, "code", None) in (401, 403) or any(
        t in low for t in ("free tier", "billing", "denied access", "api key not valid", "api_key_invalid", "unauthenticated"))


def _groq_generate(prompt: str, json_mode: bool = False) -> str:
    import time

    import requests

    body = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {GROQ_KEY.strip()}"}
    for attempt in range(2):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=45)
        if r.status_code in (429, 503) and attempt == 0:
            time.sleep(3)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Groq {r.status_code}: {r.text[:200]}")
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    raise RuntimeError("Groq did not respond")


def generate(prompt: str, key: str, model: str, json_mode: bool = False) -> str:
    """Gemini first; if it fails for any reason and a Groq key is configured, Groq answers instead."""
    global _GEMINI_BLOCKED_UNTIL
    import time

    problems = []
    have_gemini = bool((key or "").strip())
    if have_gemini and (time.time() >= _GEMINI_BLOCKED_UNTIL or not GROQ_KEY.strip()):
        try:
            return _gemini_generate(prompt, key, model, json_mode)
        except Exception as err:
            if not GROQ_KEY.strip():
                raise
            if _permanent(err):
                _GEMINI_BLOCKED_UNTIL = time.time() + 600
            problems.append(f"Gemini: {str(err)[:200]}")
    elif have_gemini:
        problems.append("Gemini skipped for a few minutes after a permission/billing error")
    if GROQ_KEY.strip():
        try:
            return _groq_generate(prompt, json_mode)
        except Exception as err:
            problems.append(f"Groq: {str(err)[:200]}")
        raise RuntimeError(" | ".join(problems))
    raise RuntimeError("No AI key is configured (set GEMINI_API_KEY and/or GROQ_API_KEY in Secrets).")


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("AI did not return valid JSON")
        return json.loads(m.group(0))


# ----------------------------- Dataset profile -----------------------------
def schema_profile(df: pd.DataFrame, samples: int = 3) -> list:
    """Column names, types and a few example values: what the AI sees (never the full data)."""
    prof = []
    for c in df.columns:
        vals = df[c].dropna()
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            ex = [str(v.date()) for v in vals.head(samples)]
            typ = "date"
        else:
            ex = [str(v)[:40] for v in vals.drop_duplicates().head(samples)]
            typ = "number" if pd.api.types.is_numeric_dtype(df[c]) else "text"
        prof.append({"column": str(c), "type": typ, "distinct_values": int(vals.nunique()), "examples": ex})
    return prof


# ----------------------------- 1. Column mapping -----------------------------
ROLE_HELP = {
    "revenue": "sales / revenue amount per row", "cost": "cost of goods or cost amount",
    "quantity": "units or quantity", "order_id": "order / invoice / transaction identifier",
    "customer_id": "customer identifier or name", "date": "transaction or record date",
    "marketing_spend": "advertising or marketing spend", "impressions": "ad impressions / views",
    "clicks": "clicks", "conversions": "conversions / purchases from marketing", "leads": "leads / enquiries",
    "product": "product, item, SKU or category", "region": "region, city, state, zone or market",
    "channel": "sales or marketing channel / source / campaign",
}


def ai_column_mapping(profile: list, missing_roles: list, used_columns: list, key: str, model: str) -> dict:
    prompt = (
        "You map spreadsheet columns to business roles for an analytics dashboard.\n"
        f"Columns (name, type, examples):\n{json.dumps(profile, indent=1)}\n\n"
        f"Roles to fill:\n{json.dumps({r: ROLE_HELP.get(r, r) for r in missing_roles}, indent=1)}\n\n"
        f"Columns already taken (do not reuse): {json.dumps(used_columns)}\n"
        "Return JSON {role: exact column name or null}. Only map when you are reasonably confident; "
        "numeric roles must map to number columns."
    )
    raw = parse_json(generate(prompt, key, model, json_mode=True))
    names = {p["column"] for p in profile}
    out, taken = {}, set(used_columns)
    for role, col in raw.items():
        if role in missing_roles and isinstance(col, str) and col in names and col not in taken:
            out[role] = col
            taken.add(col)
    return out


# ----------------------------- Safe SQL execution -----------------------------
def safe_sql(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql).strip().rstrip(";")
    stripped = re.sub(r"'(?:[^']|'')*'", "''", sql)          # ignore text inside string literals
    stripped = re.sub(r'"(?:[^"]|"")*"', '""', stripped)      # ignore quoted column names
    if ";" in stripped:
        raise ValueError("Only a single query is allowed")
    if not re.match(r"^\s*(select|with)\b", stripped, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed")
    if FORBIDDEN.search(stripped):
        raise ValueError("Query uses a command that is not allowed")
    return sql


def run_sql(df: pd.DataFrame, sql: str, limit: int = 500) -> pd.DataFrame:
    sql = safe_sql(sql)
    con = duckdb.connect()
    try:
        con.register("data", df)
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        return con.execute(f"SELECT * FROM ({sql}) AS ai_query LIMIT {int(limit)}").df()
    finally:
        con.close()


SQL_RULES = (
    "Write ONE DuckDB SQL SELECT query over the table named data.\n"
    "Rules: only SELECT/WITH; wrap every column name in double quotes exactly as given; "
    "use TRY_CAST(col AS DOUBLE) for maths on columns; if a \"_date\" TIMESTAMP column exists use it for "
    "anything time-based (e.g. date_trunc('month', \"_date\")); give result columns short readable aliases; "
    "order results sensibly; return at most 50 rows unless asked otherwise. "
    "Put the category or date column first and the main number second when it makes sense."
)


# ----------------------------- 2. Ask your data -----------------------------
def text_to_sql(question: str, profile: list, key: str, model: str, previous_sql: str = None,
                error: str = None) -> dict:
    prompt = (
        f"{SQL_RULES}\n\nColumns of data:\n{json.dumps(profile, indent=1)}\n\n"
        f"Question: {question}\n"
        + (f"\nYour previous query failed.\nQuery: {previous_sql}\nError: {error}\nFix it.\n" if error else "")
        + '\nReturn JSON {"sql": "...", "explanation": "one sentence on how the query answers the question"}. '
        'If the data cannot answer the question, return {"sql": null, "explanation": "why not"}.'
    )
    return parse_json(generate(prompt, key, model, json_mode=True))


def answer_from_result(question: str, result: pd.DataFrame, key: str, model: str) -> str:
    prompt = (
        "Answer the question in 1-3 plain sentences using ONLY this query result. "
        "Quote the key numbers; do not speculate about causes.\n"
        f"Question: {question}\nResult (first rows, CSV):\n{result.head(25).to_csv(index=False)}"
    )
    return generate(prompt, key, model)


# ----------------------------- 4. Suggested analyses -----------------------------
def suggest_analyses(profile: list, key: str, model: str, n: int = 5) -> dict:
    prompt = (
        f"{SQL_RULES}\n\nYou are a senior business analyst seeing this dataset for the first time.\n"
        f"Columns of data:\n{json.dumps(profile, indent=1)}\n\n"
        f"Suggest the {n} most useful analyses for a business team. Return JSON: "
        '{"summary": "2 sentences on what this dataset is and what it can tell the team", '
        '"analyses": [{"title": "...", "why": "one sentence on the business value", '
        '"sql": "...", "chart": "bar" | "line" | "table"}]}. '
        "Use line for trends over time, bar for comparisons between categories, table otherwise."
    )
    return parse_json(generate(prompt, key, model, json_mode=True))


# ----------------------------- 5. AI-designed dashboard -----------------------------
PLAN_SPEC = """Return JSON with exactly these keys:
{
 "dataset_type": "short label, e.g. 'E-commerce orders' or 'Ad campaign performance'",
 "summary": "one sentence on what this data covers",
 "primary_metric": {"name": "the single most important measure", "expr": "SQL aggregate expression over data, e.g. SUM(TRY_CAST(\\"Sales\\" AS DOUBLE))", "format": "money|number|percent|ratio|days"},
 "kpis": [{"title": "...", "sql": "SELECT <one aggregate expression> AS value FROM data", "format": "money|number|percent|ratio|days", "note": "very short explanation, e.g. 'Profit / revenue'", "better": "up|down (is a higher value good or bad for the business?)"}],
 "charts": [{"title": "...", "sql": "...", "chart": "line|bar|pie|table", "why": "one sentence on the business question it answers"}],
 "filters": ["column names that make good dashboard filters (categorical, under 50 distinct values)"],
 "breakdowns": ["categorical column names worth monitoring for big changes"]
}
Rules:
- 4 to 6 KPIs and 4 to 6 charts, ordered by importance for decisions.
- Include columns beyond the obvious ones when they carry business meaning (discounts, returns, ratings, delivery time, status...).
- KPI queries must return exactly one row with one column named value. Express percentages as 0-100 numbers.
- If a "_date" column exists, the first chart must be the main metric by month over "_date" (line).
- Use pie only for share across 2-6 categories. Bar charts: at most 15 categories, sorted descending.
- Up to 4 filters and up to 3 breakdowns; only use columns that exist."""


def plan_dashboard(profile: list, key: str, model: str) -> dict:
    prompt = (SQL_RULES + "\n\nYou are a senior business analyst designing an automated dashboard for a team, "
              "for data you have never seen. Decide what the team most needs to see.\n\n"
              "Columns of data:\n" + json.dumps(profile, indent=1) + "\n\n" + PLAN_SPEC)
    return parse_json(generate(prompt, key, model, json_mode=True))


def repair_queries(profile: list, failures: list, key: str, model: str) -> dict:
    prompt = (SQL_RULES + "\n\nColumns of data:\n" + json.dumps(profile, indent=1) +
              "\n\nThese dashboard queries failed. Fix each one so it runs and keeps its purpose "
              "(KPI queries must still return one row, one column named value).\n" + json.dumps(failures, indent=1) +
              '\n\nReturn JSON {"fixed": [{"id": "...", "sql": "..."}]}. Omit any you cannot fix.')
    return parse_json(generate(prompt, key, model, json_mode=True))


def write_insights(context: dict, key: str, model: str) -> str:
    prompt = (
        "You are the analyst behind an automated team dashboard. Using ONLY the figures in the JSON below "
        "(all calculated by SQL), write concise markdown with three sections:\n"
        "**What's happening** (3-5 bullets with numbers), **Watch-outs** (1-3 bullets, use the alerts if any), "
        "**Recommended actions** (3 specific bullets).\n"
        "Never invent figures or causes the data cannot show. If the latest period is partial, say so when comparing.\n\n"
        "```json\n" + json.dumps(context, default=str, indent=1) + "\n```"
    )
    return generate(prompt, key, model)
