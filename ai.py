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
def generate(prompt: str, key: str, model: str, json_mode: bool = False) -> str:
    """Call Gemini. Supports AI Studio keys (AIza...) and Vertex express keys (AQ....)."""
    from google import genai

    cfg = {"response_mime_type": "application/json"} if json_mode else None
    use_vertex = key.startswith("AQ.")

    def run(vertex: bool) -> str:
        client = genai.Client(vertexai=True, api_key=key) if vertex else genai.Client(api_key=key)
        return (client.models.generate_content(model=model, contents=prompt, config=cfg).text or "").strip()

    try:
        return run(use_vertex)
    except Exception as first:
        try:
            return run(not use_vertex)
        except Exception:
            raise first


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
