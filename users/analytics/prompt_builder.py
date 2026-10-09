import json
import re
from typing import Any, Dict, Optional, List
from users.analytics.db_utils import SCHEMA_WHITELIST
from chatbot_core.llm.chains import structured_chain
from chatbot_core.llm.schemas import SQLProposal

# ---------- CONFIG ----------
MAX_ROWS = 100  # The server enforces this; model should not assume a particular limit.
# ----------------------------

class QueryGenerationError(Exception):
    pass


# System prompt: instruct model exactly what to output and what *not* to do.
# Important: model must NOT include tenant filters or append LIMIT; the server enforces those.
SYSTEM_PROMPT = """
You are an NL→SQL assistant for a PostgreSQL analytics environment.

OUTPUT CONTRACT: Return ONLY a JSON object (no markdown, no explanation) matching this exact schema:

{
  "query": {
    "sql": string | null,
    "params": array,
    "columns": array
  },
  "explanation": string,
  "safety": {
    "is_safe": boolean,
    "warnings": array
  }
}

RULES (must follow):
1) Use only the whitelisted tables/columns provided in the 'schema' field of the system context.
2) Return parameterized SQL using %s placeholders for parameters. DO NOT interpolate raw user text into the SQL.
3) Return SELECT-only queries. If user intent would modify data, return "query": {"sql": null, ...} and explain in safety.warnings.
4) Do NOT include any tenant-level filters (tenant_id comparisons). The server (backend) will always enforce tenant scoping. If user explicitly asks for tenant-specific data, still do NOT include tenant filters in the SQL — leave scoping to the server.
5) Do NOT append LIMIT clauses. The server enforces MAX_ROWS at execution time.
6) If request is ambiguous (multiple interpretations), set sql to null and include clarifying warnings.
7) Avoid selecting sensitive columns where possible. If sensitive columns are selected, mark them in safety.warnings.
8) Supported grammar: SELECT columns or COUNT/SUM/AVG/MIN/MAX(column) with optional AS aliases, FROM one table, optional WHERE column operator %s joined with AND/OR, GROUP BY columns, ORDER BY columns/aliases ASC/DESC. Use unqualified identifiers. No joins, subqueries, date functions, expressions or table aliases. For unsupported requests return null SQL and explain the limitation.
9) Keep 'explanation' short (1-2 sentences) describing why you chose the SQL.

Return the JSON only.
""".strip()


def _call_model_for_sql(user_text: str, schema_whitelist: Dict[str, Any], examples: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """Generate a typed SQL proposal; application validation still runs afterward."""
    context = SYSTEM_PROMPT + "\nSchema whitelist (tables -> columns & sensitive flags):\n" + json.dumps(schema_whitelist)
    if examples:
        context += "\n" + "\n".join("EXAMPLE: " + json.dumps(ex) for ex in examples)
    try:
        result = structured_chain(
            SQLProposal, context, 'User request: "{user_text}"\nReturn the JSON only.',
            task="analytics", max_tokens=800,
        ).invoke({"user_text": user_text})
        return result.model_dump()
    except Exception as exc:
        raise QueryGenerationError("SQL proposal generation or validation failed") from exc


def _contains_disallowed_tokens(sql: str) -> Optional[str]:
    """Return a reason string if disallowed tokens found, else None."""
    if ";" in sql and not re.search(r";\s*$", sql):
        return "Multi-statement SQL not allowed."
    bad = re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke)\b", sql, re.I)
    if bad:
        return f"Disallowed SQL keyword detected: {bad.group(1)}"
    return None


def _validate_against_whitelist(sql: str, columns: List[str], schema_whitelist: Dict[str, Any]) -> Optional[str]:
    """
    Validate that referenced columns and tables are within whitelist.
    Conservative checks: model also returns a 'columns' list which we validate; table names are extracted by regex.
    Returns None if OK, else reason string.
    """
    # The model's labels may be aggregate aliases and are not authority.
    # execute_db_query validates actual identifiers and obtains output labels
    # from the cursor, independently of this generation-time lint.

    # Conservative regex-based table extraction from FROM/JOIN tokens
    table_tokens = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([a-zA-Z0-9_]+)", sql, re.I):
        table = (m.group(1) or "").lower()
        if table:
            table_tokens.add(table)
    if table_tokens:
        for t in table_tokens:
            if t not in schema_whitelist:
                return f"Table '{t}' is not in whitelist."

    return None


def _is_aggregate_query(sql: str) -> bool:
    if re.search(r"\b(count|sum|avg|min|max)\s*\(", sql, re.I):
        return True
    if re.search(r"\bgroup\s+by\b", sql, re.I):
        return True
    return False


def _validate_tenant_consistency(sql: str, params: List[Any], tenant_id: str) -> Optional[str]:
    """
    Return a reason string if the SQL or params contain an explicit tenant filter
    that conflicts with the provided tenant_id, or if ambiguous in a way that
    could allow cross-tenant access. Returns None if OK.
    Heuristic rules:
      - If SQL contains literal `tenant_id = '...` and that literal != tenant_id -> reject.
      - If SQL contains `tenant_id = %s`, attempt to map which %s index this is by counting
        occurrences of %s up to that point; if corresponding param exists and != tenant_id -> reject.
      - If SQL contains ambiguous tenant references (IN lists, casts, expressions we can't reason about) -> reject.
    """
    # 1) Literal check: tenant_id = '...' or tenant_id='...'
    lit_match = re.search(r"\btenant_id\s*=\s*'([^']+)'\b", sql, re.I)
    if lit_match:
        lit_val = lit_match.group(1)
        if lit_val != tenant_id:
            return f"Query explicitly filters tenant_id = '{lit_val}', which does not match the provided tenant_id; rejecting to prevent cross-tenant access."
        # if it matches provided tenant_id exactly, we still reject here because server must control tenant scoping
        return "Query includes a literal tenant_id; model must not include tenant scoping. Rejecting to avoid accidental privilege elevation."

    # 2) Placeholder check: tenant_id = %s
    # Find all occurrences of "tenant_id = %s" and attempt to map each to the corresponding param by counting %s before it.
    for m in re.finditer(r"\btenant_id\s*=\s*%s\b", sql, re.I):
        # Determine how many "%s" placeholders appear before this match
        before = sql[:m.start()]
        placeholders_before = len(re.findall(r"%s", before))
        # The placeholder for this tenant comparison will be at index `placeholders_before` (0-based)
        idx = placeholders_before
        if idx >= len(params):
            return "Query uses tenant_id = %s but model did not supply sufficient params to determine value; rejecting as ambiguous."
        param_val = params[idx]
        # If param is not equal to tenant_id, reject.
        # We allow string/uuid comparisons; convert both to str for loose equality but require exact match.
        if str(param_val) != str(tenant_id):
            return f"Query uses tenant_id = %s with param value '{param_val}' which does not match provided tenant_id; rejecting to prevent cross-tenant access."
        # If it matches the provided tenant_id, we still reject because model should not assert tenant scoping.
        return "Query includes tenant_id as a parameterized filter. Models must not include tenant scoping; server enforces scoping. Rejecting."

    # 3) More complex/ambiguous tenant usage (IN lists, casts, comparisons with functions) -> conservative reject
    if re.search(r"\btenant_id\b.*\b(in|=|!=|<>|::)\b", sql, re.I) and not re.search(r"tenant_id\s*=\s*%s|\btenant_id\s*=\s*'[^']+'", sql, re.I):
        return "Query references tenant_id in an expression that is ambiguous or complex; rejecting to avoid cross-tenant leakage."

    return None


def _ensure_limit_warning(sql: str) -> Optional[str]:
    """
    The model is asked NOT to append LIMIT. The server enforces MAX_ROWS.
    Here we only emit a warning if a query is potentially unbounded (no LIMIT and not aggregate).
    """
    if _is_aggregate_query(sql):
        return None
    if not re.search(r"\blimit\b\s*\d+", sql, re.I):
        return f"Query does not include a LIMIT; server will enforce LIMIT {MAX_ROWS} at execution time."
    return None


def create_db_query(user_text: str, tenant_id: str, examples: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """
    Generate a parameterized SQL query using the language model, validate against whitelist and safety rules,
    and return a dict:
    {
      "sql": <string or None>,
      "params": [...],
      "columns": [...],
      "explanation": "...",
      "safety": {"is_safe": bool, "warnings": [...]}
    }

    NOTE: tenant_id is required and MUST be provided. The model is instructed NOT to include tenant filters;
    server will enforce tenant scoping. Any model attempt to include tenant filters (literal or parameterized)
    will cause this function to mark the query as unsafe / reject it.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required and must be provided (non-empty).")

    try:
        model_resp = _call_model_for_sql(user_text, SCHEMA_WHITELIST, examples=examples)
    except QueryGenerationError as e:
        return {
            "sql": None,
            "params": [],
            "columns": [],
            "explanation": "",
            "safety": {"is_safe": False, "warnings": [f"Model generation error: {e}"]},
        }

    query_obj = model_resp.get("query") or {}
    safety_obj = model_resp.get("safety", {"is_safe": False, "warnings": ["no safety info from model"]})
    explanation = model_resp.get("explanation", "") or ""

    result = {
        "sql": None,
        "params": [],
        "columns": [],
        "explanation": explanation,
        "safety": {"is_safe": False, "warnings": []},
    }

    # If model intentionally returned null SQL (ambiguity / safety), forward warnings
    if not query_obj or not query_obj.get("sql"):
        result["safety"]["is_safe"] = False
        result["safety"]["warnings"].extend(safety_obj.get("warnings", ["Model returned no query."]))
        return result

    sql = query_obj.get("sql", "").strip()
    params = list(query_obj.get("params", []) or [])
    columns = list(query_obj.get("columns", []) or [])

    # Basic syntactic checks
    if not re.match(r"^\s*select\b", sql, re.I):
        result["safety"]["is_safe"] = False
        result["safety"]["warnings"].append("Only SELECT queries are allowed.")
        return result

    disallowed = _contains_disallowed_tokens(sql)
    if disallowed:
        result["safety"]["is_safe"] = False
        result["safety"]["warnings"].append(disallowed)
        return result

    # Whitelist validation (conservative)
    wl_err = _validate_against_whitelist(sql, columns, SCHEMA_WHITELIST)
    if wl_err:
        result["safety"]["is_safe"] = False
        result["safety"]["warnings"].append(wl_err)
        return result

    # Tenant consistency: model must not include tenant scoping or reference other tenants.
    tenant_err = _validate_tenant_consistency(sql, params, tenant_id)
    if tenant_err:
        result["safety"]["is_safe"] = False
        result["safety"]["warnings"].append(tenant_err)
        return result

    # Issue a non-fatal warning if LIMIT is missing (server will apply MAX_ROWS)
    limit_warn = _ensure_limit_warning(sql)
    if limit_warn:
        result["safety"]["warnings"].append(limit_warn)

    # If the model included sensitive columns in 'columns' list, add warnings
    for c in columns:
        colname = c.split(".")[-1].lower()
        for tbl, info in SCHEMA_WHITELIST.items():
            if colname in (col.lower() for col in info.get("columns", [])):
                if colname in [s.lower() for s in info.get("sensitive", [])]:
                    result["safety"]["warnings"].append(f"Column '{c}' is marked sensitive in schema; ensure user has permission before returning results.")

    # All checks passed => mark safe. Note: we DO NOT modify SQL here (no tenant injection, no LIMIT append).
    result["sql"] = sql
    result["params"] = params
    result["columns"] = columns
    result["safety"]["is_safe"] = True
    # Use model-provided warnings (if any) plus our warnings
    model_warnings = safety_obj.get("warnings", []) or []
    # Combine, de-duplicate preserving order
    combined_warnings = []
    for w in (model_warnings + result["safety"]["warnings"]):
        if w not in combined_warnings:
            combined_warnings.append(w)
    result["safety"]["warnings"] = combined_warnings

    return result
