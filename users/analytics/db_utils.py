# db_utils.py
from django.db import connection
import json
import re

MAX_ROWS = 100  # authoritative server-side enforced limit

# --------------------------------------------------------------------
# YOUR WHITELIST: keys are actual DB table names (e.g. orders_order)
# Keep this updated to reflect current DB tables & sensitive columns.
# --------------------------------------------------------------------
SCHEMA_WHITELIST = {
    "orders_customer": {
        "columns": [
            "id",
            "tenant_id",
            "name",
            "phone",
            "whatsapp_number",
            "telegram_id",
            "address",
            "location_coordinates",
            "created_at",
        ],
        "sensitive": ["phone", "whatsapp_number", "telegram_id", "address"],
    },
    "orders_menucategory": {
        "columns": ["id", "tenant_id", "name"],
        "sensitive": [],
    },
    "orders_menuitem": {
        "columns": [
            "id",
            "tenant_id",
            "name",
            "quantity",
            "is_available",
            "description",
            "platform_item_ids",
            "category_fk_id",
            "meta",
        ],
        "sensitive": [],
    },
    "orders_menuitemvariant": {
        "columns": [
            "id",
            "menu_item_id",
            "size",
            "price",
            "volume_ml",
            "weight_grams",
            "description",
            "gst_liability",
        ],
        "sensitive": [],
    },
    "orders_menucatalogmeta": {
        "columns": [
            "id",
            "menu_item_id",
            "dietary_preferences",
            "allergens",
            "preparation",
            "nutrition",
            "explore_options",
            "ingredients",
            "recommendations",
            "specialty_items",
            "source_quality",
            "pairings",
            "flavor_profile",
            "updated_at",
        ],
        "sensitive": [],
    },
    "orders_order": {
        "columns": [
            "id",
            "tenant_id",
            "external_order_id",
            "source",
            "customer_id",
            "delivery_partner_id",
            "order_status",
            "payment_status",
            "payment_mode",
            "total_amount",
            "tax_amount",
            "discount_amount",
            "created_at",
            "updated_at",
            "meta",
            "location_coordinates",
            "order_type",
            "advanced_order",
            "preorder_date",
            "preorder_time",
            "payment_type",
            "packing_charges",
            "service_charge",
            "delivery_charges",
            "enable_delivery",
            "urgent_order",
            "urgent_time_mins",
            "pickup_otp",
            "discount_type",
            "callback_url",
        ],
        "sensitive": ["callback_url", "location_coordinates", "meta"],
    },
    "orders_orderitem": {
        "columns": [
            "id",
            "order_id",
            "item_id",
            "item_name",
            "quantity",
            "unit_price",
            "total_price",
            "variant_id",
            "gst_liability",
            "item_tax_snapshot",
        ],
        "sensitive": [],
    },
    "orders_chatsession": {
        "columns": [
            "id",
            "tenant_id",
            "customer_id",
            "session_id",
            "platform",
            "order_id",
            "state",
            "language",
            "device_info",
            "geo_metadata",
            "is_completed",
            "last_interaction_at",
            "created_at",
        ],
        "sensitive": ["geo_metadata", "device_info", "state"],
    },
    "orders_customeraddress": {
        "columns": [
            "id",
            "tenant_id",
            "customer_id",
            "label",
            "address_line",
            "location_coordinates",
            "components",
            "is_default",
            "created_at",
            "updated_at",
        ],
        "sensitive": ["address_line", "location_coordinates", "components"],
    },
}

class ExecutionError(Exception):
    pass


def apply_tenant_scoping(sql, params, tenant_id):
    """Compile a deliberately small SELECT grammar over an ORM-scoped relation.

    No arbitrary SQL, joins, subqueries, functions or model-declared safety flags
    reach the database. Unsupported reports fail closed instead of guessing how
    to inject a predicate into SQL. Child tables inherit scope through their FK.
    """
    from django.apps import apps
    if not tenant_id or not str(tenant_id).isdigit():
        raise ExecutionError('A tenant identity is required.')
    if not isinstance(sql, str) or len(sql) > 10000:
        raise ExecutionError('Invalid analytics query.')
    match = re.fullmatch(
        r"\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<table>[a-z_]+)"
        r"(?:\s+WHERE\s+(?P<where>.+?))?"
        r"(?:\s+GROUP\s+BY\s+(?P<group>.+?))?"
        r"(?:\s+ORDER\s+BY\s+(?P<order>.+?))?\s*;?\s*", sql, re.I)
    if not match or match['table'].lower() not in SCHEMA_WHITELIST:
        raise ExecutionError('Use a single supported analytics table.')
    table = match['table'].lower()
    model = next(m for m in apps.get_app_config('orders').get_models() if m._meta.db_table == table)
    allowed = set(SCHEMA_WHITELIST[table]['columns']) & {f.column for f in model._meta.fields}
    sensitive = set(SCHEMA_WHITELIST[table]['sensitive'])
    identifier = r'[a-z_][a-z_0-9]*'

    def column(value):
        value = value.lower()
        if value not in allowed:
            raise ExecutionError('Unsupported analytics column.')
        return connection.ops.quote_name(value)

    projections, aliases = [], set()
    for part in match['select'].split(','):
        item = re.fullmatch(rf"\s*(?P<expr>\*|{identifier}|(?:COUNT|SUM|AVG|MIN|MAX)\(\s*(?:DISTINCT\s+)?(?:{identifier}|\*)\s*\))"
                            rf"(?:\s+AS\s+(?P<alias>{identifier}))?\s*", part, re.I)
        if not item:
            raise ExecutionError('Unsupported analytics expression.')
        expr = item['expr'].lower()
        aggregate = re.fullmatch(r'(count|sum|avg|min|max)\(\s*(distinct\s+)?([a-z_0-9]+|\*)\s*\)', expr)
        if aggregate:
            function, distinct, name = aggregate.groups()
            if name == '*' and (function != 'count' or distinct):
                raise ExecutionError('Unsupported aggregate.')
            expr = f"{function.upper()}({('DISTINCT ' if distinct else '')}{'*' if name == '*' else column(name)})"
        elif expr != '*':
            expr = column(expr)
        if item['alias']:
            alias = item['alias'].lower()
            aliases.add(alias)
            expr += ' AS ' + connection.ops.quote_name(alias)
        projections.append(expr)

    where = ''
    parameter_count = 0
    if match['where']:
        parts = re.split(r'\s+(AND|OR)\s+', match['where'], flags=re.I)
        predicates = []
        for i, part in enumerate(parts):
            if i % 2:
                predicates.append(part.upper())
                continue
            predicate = re.fullmatch(rf'\s*({identifier})\s*(=|!=|<>|<=|>=|<|>|LIKE|ILIKE)\s*%s\s*', part, re.I)
            if not predicate:
                raise ExecutionError('Filters must compare a column with a parameter.')
            predicates.append(f'{column(predicate[1])} {predicate[2].upper()} %s')
            parameter_count += 1
        where = ' WHERE ' + ' '.join(predicates)
    if len(params) != parameter_count or any(not isinstance(v, (str, int, float, bool, type(None))) for v in params):
        raise ExecutionError('Invalid analytics parameters.')
    group = ''
    if match['group']:
        group = ' GROUP BY ' + ', '.join(column(v.strip()) for v in match['group'].split(','))
    order = ''
    if match['order']:
        parts = []
        for part in match['order'].split(','):
            sort = re.fullmatch(rf'\s*({identifier})(?:\s+(ASC|DESC))?\s*', part, re.I)
            if not sort:
                raise ExecutionError('Unsupported ordering.')
            name = sort[1].lower()
            parts.append((connection.ops.quote_name(name) if name in aliases else column(name)) + ' ' + (sort[2] or 'ASC').upper())
        order = ' ORDER BY ' + ', '.join(parts)
    ownership = {'orders_orderitem': 'order__tenant_id', 'orders_menuitemvariant': 'menu_item__tenant_id',
                 'orders_menucatalogmeta': 'menu_item__tenant_id'}.get(table, 'tenant_id')
    qs = model.objects.filter(**{ownership: tenant_id}).order_by()
    # Mask at the source, before aliases, aggregates and filters can expose PII.
    source_columns = sorted(allowed - sensitive)
    source_sql, source_params = qs.values(*source_columns).query.sql_with_params()
    if sensitive & allowed:
        source_sql = 'SELECT _owned.*, ' + ', '.join('NULL AS ' + connection.ops.quote_name(c) for c in sorted(sensitive & allowed)) + f' FROM ({source_sql}) _owned'
    compiled = f"SELECT {', '.join(projections)} FROM ({source_sql}) _tenant_rows{where}{group}{order} LIMIT {MAX_ROWS}"
    return compiled, [*source_params, *params]


def execute_db_query(model_query_output, tenant_id, enforce_tenant=True, mask_sensitive=True, allow_model_tenant=False):
    if not enforce_tenant or not mask_sensitive:
        raise ExecutionError('Analytics ownership and sensitive-field protection cannot be disabled.')
    if not isinstance(model_query_output, dict):
        raise ExecutionError('Invalid analytics proposal.')
    sql, params = apply_tenant_scoping(model_query_output.get('sql'), list(model_query_output.get('params') or []), tenant_id)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [col[0] for col in cursor.description]
            values = cursor.fetchmany(MAX_ROWS)
    except Exception as exc:
        raise ExecutionError('The requested analytics query could not be executed.') from exc
    from django.core.serializers.json import DjangoJSONEncoder
    rows = json.loads(json.dumps([dict(zip(columns, row)) for row in values], cls=DjangoJSONEncoder))
    return {'sql': sql, 'params': params, 'columns': columns, 'rows': rows, 'row_count': len(rows), 'warnings': []}
