"""
SQL backend for the dashboard (DuckDB, in-process — no server or account needed).

Stage 1  clean_with_sql(df)        raw table  -> clean table   (trim, null-normalise, type-cast, dedupe)
Stage 2  kpis_with_sql(df, map)    clean table -> KPI summary  (all headline metrics in one query)
Stage 3  breakdown_with_sql(...)   clean table -> grouped aggregates for charts / trends

Every generated query is returned alongside its result so the dashboard can show
"the SQL behind these numbers".
"""
import duckdb
import pandas as pd

CURRENCY_PATTERN = r"[₹$€£,%\s]"
NULL_TOKENS = ("", "nan", "none", "null", "n/a", "na", "-")


def q(name: str) -> str:
    """Quote a column name as a SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Unique, trimmed column names; object columns as nullable strings so DuckDB types them cleanly."""
    df = df.copy()
    seen, cols = {}, []
    for c in [str(c).strip() or "column" for c in df.columns]:
        if c in seen:
            seen[c] += 1
            cols.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            cols.append(c)
    df.columns = cols
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].astype("string")
    return df


def clean_with_sql(df: pd.DataFrame, numeric_threshold: float = 0.9) -> tuple:
    """Stage 1: validate + clean in SQL. Returns (clean_df, report, sql)."""
    raw = _prepare(df)
    con = duckdb.connect()
    con.register("raw_input", raw)
    con.execute("CREATE TABLE raw AS SELECT *, ROW_NUMBER() OVER () AS _rn FROM raw_input")
    types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                             "WHERE table_name = 'raw' AND column_name <> '_rn'").fetchall())
    nulls = ", ".join(lit(t) for t in NULL_TOKENS)

    select_exprs, converted, dropped = [], [], []
    for col, dtype in types.items():
        c = q(col)
        if dtype == "VARCHAR":
            text = f"CASE WHEN lower(trim({c})) IN ({nulls}) THEN NULL ELSE trim({c}) END"
            num = f"TRY_CAST(regexp_replace({text}, '{CURRENCY_PATTERN}', '', 'g') AS DOUBLE)"
            nonnull, numeric = con.execute(f"SELECT COUNT({text}), COUNT({num}) FROM raw").fetchone()
            if nonnull == 0:
                dropped.append(col)
                continue
            if numeric >= numeric_threshold * nonnull:
                select_exprs.append(f"{num} AS {c}")
                converted.append(col)
            else:
                select_exprs.append(f"{text} AS {c}")
        else:
            if con.execute(f"SELECT COUNT({c}) FROM raw").fetchone()[0] == 0:
                dropped.append(col)
                continue
            select_exprs.append(f"{c}")

    if not select_exprs:
        con.close()
        return pd.DataFrame(), {"rows_in": len(raw), "rows_out": 0, "empty_rows_removed": len(raw),
                                "duplicates_removed": 0, "columns": 0, "numeric_converted": [],
                                "empty_columns_dropped": dropped}, ""

    cols_out = [e.rsplit(" AS ", 1)[-1] for e in select_exprs]
    not_empty = " OR ".join(f"{c} IS NOT NULL" for c in cols_out)
    sql = (
        "-- Stage 1: clean & deduplicate\n"
        "WITH typed AS (\n  SELECT\n    " + ",\n    ".join(select_exprs) + ",\n    _rn\n  FROM raw\n),\n"
        f"non_empty AS (\n  SELECT * FROM typed WHERE {not_empty}\n)\n"
        "SELECT * EXCLUDE (_rn) FROM (\n  SELECT * EXCLUDE (_rn), MIN(_rn) AS _rn\n"
        "  FROM non_empty\n  GROUP BY ALL\n) ORDER BY _rn"
    )
    non_empty_rows = con.execute(f"WITH typed AS (SELECT {', '.join(select_exprs)} FROM raw) "
                                 f"SELECT COUNT(*) FROM typed WHERE {not_empty}").fetchone()[0]
    clean = con.execute(sql).df()
    con.close()
    report = {
        "rows_in": int(len(raw)),
        "empty_rows_removed": int(len(raw) - non_empty_rows),
        "duplicates_removed": int(non_empty_rows - len(clean)),
        "rows_out": int(len(clean)),
        "columns": int(clean.shape[1]),
        "numeric_converted": converted,
        "empty_columns_dropped": dropped,
    }
    return clean, report, sql


def _num(mapping: dict, role: str):
    col = mapping.get(role)
    return f"TRY_CAST({q(col)} AS DOUBLE)" if col else None


def kpis_with_sql(df: pd.DataFrame, mapping: dict) -> tuple:
    """Stage 2: every headline KPI in one aggregate query. Returns (dict, sql)."""
    rev, cost, qty = _num(mapping, "revenue"), _num(mapping, "cost"), _num(mapping, "quantity")
    spend, imp = _num(mapping, "marketing_spend"), _num(mapping, "impressions")
    clicks, conv = _num(mapping, "clicks"), _num(mapping, "conversions")
    parts = ["COUNT(*) AS row_count"]
    if rev:
        parts.append(f"SUM({rev}) AS total_revenue")
        parts.append(f"COUNT(DISTINCT {q(mapping['order_id'])}) AS orders" if mapping.get("order_id")
                     else f"COUNT({rev}) AS orders")
    if cost:
        parts.append(f"SUM({cost}) AS total_cost")
    if rev and cost:
        parts.append(f"SUM({rev} - {cost}) AS gross_profit")
    if qty:
        parts.append(f"SUM({qty}) AS units_sold")
    if mapping.get("customer_id"):
        parts.append(f"COUNT(DISTINCT {q(mapping['customer_id'])}) AS unique_customers")
    if spend:
        parts.append(f"SUM({spend}) AS marketing_spend")
    if imp:
        parts.append(f"SUM({imp}) AS impressions")
    if clicks:
        parts.append(f"SUM({clicks}) AS clicks")
    if conv:
        parts.append(f"SUM({conv}) AS conversions")

    derived = []
    if rev:
        derived.append("total_revenue / NULLIF(orders, 0) AS avg_order_value")
    if rev and cost:
        derived.append("gross_profit / NULLIF(total_revenue, 0) * 100 AS gross_margin_pct")
    if rev and mapping.get("customer_id"):
        derived.append("total_revenue / NULLIF(unique_customers, 0) AS revenue_per_customer")
    if rev and spend:
        derived.append("total_revenue / NULLIF(marketing_spend, 0) AS roas")
    if spend and conv:
        derived.append("marketing_spend / NULLIF(conversions, 0) AS cost_per_conversion")
    if imp and clicks:
        derived.append("clicks / NULLIF(impressions, 0) * 100 AS ctr_pct")
    if clicks and conv:
        derived.append("conversions / NULLIF(clicks, 0) * 100 AS click_to_conversion_pct")

    sql = ("-- Stage 2: headline KPIs\nWITH base AS (\n  SELECT\n    " + ",\n    ".join(parts) +
           "\n  FROM dashboard_data\n)\nSELECT *" + ("".join(f",\n  {d}" for d in derived)) + "\nFROM base")
    con = duckdb.connect()
    con.register("dashboard_data", df)
    row = con.execute(sql).df().iloc[0].to_dict()
    con.close()
    return {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}, sql


def breakdown_with_sql(df: pd.DataFrame, group_expr: str, measures: dict, order_by: str = None,
                       limit: int = None) -> tuple:
    """Stage 3: grouped aggregate, e.g. revenue by product or by month. Returns (df, sql)."""
    sel = ",\n  ".join(f"{expr} AS {q(alias)}" for alias, expr in measures.items())
    sql = (f"SELECT\n  {group_expr} AS grp,\n  {sel}\nFROM dashboard_data\nWHERE {group_expr} IS NOT NULL\n"
           f"GROUP BY 1\nORDER BY {order_by or '1'}" + (f"\nLIMIT {int(limit)}" if limit else ""))
    con = duckdb.connect()
    con.register("dashboard_data", df)
    out = con.execute(sql).df()
    con.close()
    return out, sql
