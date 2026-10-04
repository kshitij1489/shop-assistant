"""Server-to-server adapter API. Browser redirects are never payment evidence."""
import hashlib
import hmac
import json
import re
import time
from functools import wraps
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from pydantic import ValidationError as SchemaError
from .models import Connection, ExternalMapping, AcceptedOrder, StockItem, Payment
from .schemas import Event, Acknowledgement, MappingInput
from .credentials import adapter_secret


def signature(secret, timestamp, method, path, body):
    message = str(timestamp).encode() + b'\n' + method.encode() + b'\n' + path.encode() + b'\n' + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def authenticated(methods):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, connection_id, **kwargs):
            if request.method not in methods:
                return JsonResponse({'error': 'method_not_allowed'}, status=405)
            if len(request.body) > 262144:
                return JsonResponse({'error': 'payload_too_large'}, status=413)
            connection = Connection.objects.select_related('location').filter(pk=connection_id, active=True).first()
            secret = adapter_secret(connection) if connection else None
            stamp = request.headers.get('X-Commerce-Timestamp', '')
            supplied = request.headers.get('X-Commerce-Signature', '')
            try:
                valid_time = abs(time.time() - int(stamp)) <= 300
            except ValueError:
                valid_time = False
            if not secret or not valid_time or not re.fullmatch(r'[0-9a-f]{64}', supplied) or not hmac.compare_digest(signature(secret, stamp, request.method, request.get_full_path(), request.body), supplied):
                return JsonResponse({'error': 'unauthorized'}, status=401)
            try:
                return view(request, connection, **kwargs)
            except (SchemaError, ValueError, ValidationError, IntegrityError) as exc:
                return JsonResponse({'error': 'invalid_request', 'detail': str(exc) if isinstance(exc, (ValueError, SchemaError)) else 'Invalid or conflicting record.'}, status=400)
            except ObjectDoesNotExist:
                return JsonResponse({'error': 'not_found'}, status=404)
        return wrapped
    return decorate


@authenticated(['POST'])
def events(request, connection):
    from .events import receive
    row = receive(connection, json.loads(request.body))
    return JsonResponse({'event_id': row.event_id, 'status': row.status}, status=200 if row.status == 'processed' else 202)


@authenticated(['POST'])
def commands(request, connection):
    from .queue import claim
    return JsonResponse({'schema_version': 1, 'commands': claim(connection)})


@authenticated(['POST'])
def ack(request, connection, command_id):
    from .queue import acknowledge
    acknowledge(connection, command_id, Acknowledgement.model_validate_json(request.body))
    return JsonResponse({'ok': True})


@authenticated(['GET'])
def schema(request, connection):
    from .command_schemas import command_schema, ClaimResponse
    from .menu_schema import MenuSnapshot
    return JsonResponse({'event': Event.model_json_schema(), 'acknowledgement': Acknowledgement.model_json_schema(), 'mapping': MappingInput.model_json_schema(), 'menu_snapshot': MenuSnapshot.model_json_schema(),
                         'command': command_schema.json_schema(), 'claim_response': ClaimResponse.model_json_schema()})


@authenticated(['POST'])
def menu_snapshot(request, connection):
    if connection.role != 'pos' or 'catalog.write' not in connection.capabilities:
        return JsonResponse({'error': 'forbidden'}, status=403)
    from .menu_sync import import_snapshot
    return JsonResponse(import_snapshot(connection, json.loads(request.body)))


def assert_mapping_subject(connection, kind, pk):
    from orders.models import MenuItem, MenuItemVariant, MenuCategory, AddonGroup, AddonItem, Tax, Customer, Order
    tenant = connection.location.tenant_id
    if kind in ('order_line', 'fulfillment'):
        order_id, separator, part = pk.partition(':')
        record = AcceptedOrder.objects.get(order_id=order_id, location=connection.location)
        if connection.role != 'pos' or not separator or (kind == 'order_line' and pk not in {line['line_id'] for line in record.snapshot['pricing']['lines']}) or (kind == 'fulfillment' and part != 'fulfillment'):
            raise ValueError('Invalid order component mapping.')
        return
    if kind in ('pricing_tax', 'discount', 'fee'):
        from .models import Configuration
        config = Configuration.objects.get(tenant_id=tenant)
        codes = {'fulfillment', 'packaging'} if kind == 'fee' else {r['code'] for r in config.policy.get('taxes' if kind == 'pricing_tax' else 'discounts', [])}
        if connection.role != 'pos' or pk not in codes:
            raise ValueError('Invalid pricing rule mapping.')
        return
    models = {'item': (MenuItem, {'tenant_id': tenant}), 'variant': (MenuItemVariant, {'menu_item__tenant_id': tenant}),
              'category': (MenuCategory, {'tenant_id': tenant}), 'modifier_group': (AddonGroup, {'tenant_id': tenant}),
              'modifier': (AddonItem, {'group__tenant_id': tenant}), 'tax': (Tax, {'tenant_id': tenant}),
              'customer': (Customer, {'tenant_id': tenant}), 'order': (Order, {'commerce_record__location': connection.location}),
              'stock': (StockItem, {'location': connection.location}), 'payment': (Payment, {'connection': connection}),
              'location': (type(connection.location), {'pk': connection.location_id})}
    model, filters = models[kind]
    if kind == 'location' and str(pk) != str(connection.location_id):
        raise ValueError('Location is outside this connection.')
    if not model.objects.filter(**filters).filter(pk=pk).exists():
        raise ValueError('Canonical mapping subject is outside this connection.')
    if connection.role == 'payment' and kind not in ('payment', 'order', 'customer', 'location'):
        raise ValueError('Mapping kind is outside this connection role.')


@authenticated(['GET', 'POST'])
def mappings(request, connection):
    if request.method == 'POST':
        data = MappingInput.model_validate_json(request.body).model_dump()
        assert_mapping_subject(connection, data['kind'], data['canonical_id'])
        # Existing identities cannot be silently rebound by a retry.
        with transaction.atomic():
            mapping, created = ExternalMapping.objects.get_or_create(connection=connection, kind=data['kind'], scope=data['scope'], canonical_id=data['canonical_id'],
                defaults={k: data[k] for k in ('external_id', 'revision', 'metadata')})
            if mapping.external_id != data['external_id']:
                raise ValueError('Mapping already has a different external ID.')
        return JsonResponse({'id': str(mapping.pk), 'created': created})
    rows = ExternalMapping.objects.filter(connection=connection).order_by('pk')
    offset = max(0, int(request.GET.get('offset', 0)))
    data = list(rows.values('id', 'kind', 'canonical_id', 'external_id', 'scope', 'revision', 'metadata')[offset:offset + 200])
    return JsonResponse({'mappings': data, 'next_offset': offset + len(data) if len(data) == 200 else None})


@authenticated(['GET'])
def catalog(request, connection):
    if connection.role != 'pos' or 'catalog.read' not in connection.capabilities:
        return JsonResponse({'error': 'forbidden'}, status=403)
    from orders.models import MenuItem, Tax, MenuCategory
    from chatbot_core.logic.cafe.catalog import serialize_item
    offset = max(0, int(request.GET.get('offset', 0)))
    items = MenuItem.objects.filter(tenant_id=connection.location.tenant_id).order_by('pk').prefetch_related('variants', 'addon_groups__group__addons')[offset:offset + 100]
    data = [{**serialize_item(item), 'available': item.is_available, 'category_id': str(item.category_fk_id) if item.category_fk_id else None} for item in items]
    return JsonResponse({'schema_version': 1, 'location_id': str(connection.location_id), 'items': data,
        'categories': list(MenuCategory.objects.filter(tenant_id=connection.location.tenant_id).values('id', 'name', 'is_active')),
        'taxes': list(Tax.objects.filter(tenant_id=connection.location.tenant_id).values('id', 'title', 'type', 'rate_display')),
        'next_offset': offset + len(data) if len(data) == 100 else None})


@authenticated(['GET'])
def order_snapshot(request, connection, order_id):
    record = AcceptedOrder.objects.get(order_id=order_id, location=connection.location)
    if not record.commands.filter(connection=connection).exists():
        return JsonResponse({'error': 'not_found'}, status=404)
    # Payment adapters receive only the payment request's minimal customer data.
    if connection.role != 'pos':
        return JsonResponse({'error': 'forbidden'}, status=403)
    return JsonResponse({'accepted_order_id': str(record.pk), 'snapshot_hash': record.snapshot_hash,
                         'snapshot': record.snapshot, 'state': record.state, 'pos_state': record.pos_state})


@authenticated(['GET'])
def stock(request, connection):
    if connection.role != 'pos' or 'inventory.update' not in connection.capabilities:
        return JsonResponse({'error': 'forbidden'}, status=403)
    offset = max(0, int(request.GET.get('offset', 0)))
    rows = list(StockItem.objects.filter(location=connection.location, authority=connection).order_by('pk').values(
        'id', 'item_id', 'variant_id', 'addon_id', 'mode', 'on_hand', 'reserved', 'pending_consumed', 'sequence', 'available', 'observed_at')[offset:offset + 100])
    return JsonResponse({'stock': rows, 'next_offset': offset + len(rows) if len(rows) == 100 else None})


@authenticated(['GET'])
def manifest(request, connection):
    from .models import Configuration
    from .menu_sync import source_for
    config = Configuration.objects.filter(tenant=connection.location.tenant).first()
    source = source_for(connection.location.tenant_id)
    return JsonResponse({'schema_version': 1, 'connection_id': str(connection.pk),
        'provider': connection.provider, 'role': connection.role, 'account_id': connection.account_id,
        'environment': connection.environment, 'capabilities': connection.capabilities,
        'location': {'id': str(connection.location_id), 'code': connection.location.code,
                     'name': connection.location.name, 'timezone': connection.location.timezone,
                     'address': connection.location.address},
        'pricing_policy': config.policy if config else None,
        'menu_source': {'mode': source.mode if source else 'local',
            'is_authority': bool(source and source.mode == 'external' and source.connection_id == connection.pk),
            'generation': str(source.generation) if source else None,
            'sequence': source.sequence if source else 0,
            'max_age_seconds': source.max_age_seconds if source else None,
            'observed_at': source.observed_at if source else None}})


@authenticated(['GET'])
def event_status(request, connection, event_id):
    from .models import Inbox
    row = Inbox.objects.get(connection=connection, event_id=event_id)
    return JsonResponse({'event_id': row.event_id, 'status': row.status, 'attempts': row.attempts,
                         'error': row.error, 'processed_at': row.processed_at})
