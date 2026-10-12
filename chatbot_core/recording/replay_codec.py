"""Versioned, allowlisted object graphs for replay. Never unpickle recordings.

Shared references matter: active/previous/question intents can be the same object
as an entry in pending_queries. Ordinary session JSON intentionally loses these
turn-local links and handoffs.
"""
from enum import Enum
from types import SimpleNamespace
from uuid import UUID

from pydantic import BaseModel


class ReplaySnapshot(dict):
    """Protect codec tags/field names while redacting recorded application values."""
    def redacted(self, secrets=()):
        from .capture import snapshot, _SECRET_KEY
        from copy import deepcopy
        result = deepcopy(dict(self))
        for node in result['nodes'].values():
            if node['kind'] == 'dict':
                node['value'] = {
                    ('[REDACTED]' if key in secrets else key):
                    '[REDACTED]' if _SECRET_KEY.search(key) else snapshot(value, secrets)
                    for key, value in node['value'].items()
                }
            elif node['kind'] in {'schema', 'identity'}:
                node['value'] = snapshot(node['value'], secrets)
            elif node['kind'] in {'list', 'tuple'}:
                node['value'] = [snapshot(value, secrets) for value in node['value']]
        result['root'] = snapshot(result['root'], secrets)
        return result


class CodecError(ValueError):
    pass


def _types():
    from chatbot_core.logic.cafe.basket import Basket
    from chatbot_core.logic.cafe.intent_handler.base import BaseIntent
    from chatbot_core.logic.action_resolver import ResolvedAction
    from chatbot_core.logic.cafe.workflow.state import ConversationContext
    return Basket, BaseIntent, ResolvedAction, ConversationContext


class Encoder:
    def __init__(self):
        self.identities = {}
        self.objects = {}

    def encode(self, value):
        nodes = {}
        Basket, BaseIntent, ResolvedAction, ConversationContext = _types()

        def visit(obj):
            if isinstance(obj, Enum):
                return {'enum': type(obj).__name__, 'value': obj.value}
            if obj is None or type(obj) in (bool, int, float, str):
                return obj
            if isinstance(obj, UUID):
                return str(obj)
            key = self.identities.setdefault(id(obj), str(len(self.identities)))
            self.objects[key] = obj  # Keep objects alive across before/after capture.
            if key in nodes:
                return {'ref': key}
            node = nodes[key] = {}
            if isinstance(obj, dict):
                if any(not isinstance(k, str) for k in obj):
                    raise CodecError('Replay dictionaries require string keys')
                from .capture import _SECRET_KEY
                node.update(kind='dict', value={k: '[REDACTED]' if _SECRET_KEY.search(k) else visit(v)
                                                for k, v in obj.items()})
            elif isinstance(obj, (list, tuple)):
                node.update(kind='tuple' if isinstance(obj, tuple) else 'list', value=[visit(v) for v in obj])
            elif isinstance(obj, Basket):
                node.update(kind='basket', value=visit(vars(obj)))
            elif isinstance(obj, BaseIntent):
                node.update(kind='intent', intent=obj.intent_type, value=visit(vars(obj)))
            elif isinstance(obj, BaseModel):
                from chatbot_core.llm import schemas
                if getattr(schemas, type(obj).__name__, None) is not type(obj):
                    raise CodecError(f'Unsupported replay schema: {type(obj).__name__}')
                node.update(kind='schema', name=type(obj).__name__, value=obj.model_dump(mode='json'))
            elif isinstance(obj, ResolvedAction):
                node.update(kind='action', value=visit(vars(obj)))
            elif isinstance(obj, ConversationContext):
                node.update(kind='context', value=visit(vars(obj)))
            elif isinstance(obj, SimpleNamespace) or getattr(getattr(obj, '_meta', None), 'label_lower', '') in {
                    'chatbot_core.tenantinfo', 'orders.customer'}:
                # Identity only. No model managers, credentials or lazy relations.
                pk = str(obj.pk) if isinstance(obj.pk, UUID) else obj.pk
                identity = {'id': pk, 'pk': pk}
                if hasattr(obj, 'tenant_id'):
                    identity['tenant_id'] = str(obj.tenant_id)
                else:
                    identity['api_key'] = '[REDACTED]'
                node.update(kind='identity', value=identity)
            else:
                raise CodecError(f'Unsupported replay value: {type(obj).__name__}')
            return {'ref': key}

        root = visit(value)
        return ReplaySnapshot(version=1, root=root, nodes=nodes)


def decode(document, *, existing=None):
    if document.get('version') != 1:
        raise CodecError('Unsupported replay snapshot version')
    nodes, objects, done, visiting = document['nodes'], dict(existing or {}), set(), set()
    Basket, BaseIntent, ResolvedAction, ConversationContext = _types()

    def visit(value):
        if not isinstance(value, dict):
            return value
        if 'enum' in value:
            from chatbot_core.logic.outcomes import TaskOutcome
            if value['enum'] != 'TaskOutcome':
                raise CodecError('Unsupported replay enum')
            return TaskOutcome(value['value'])
        key = value['ref']
        if key in done or key in visiting:
            return objects[key]
        node = nodes[key]
        kind = node['kind']
        visiting.add(key)
        if kind == 'dict':
            obj = objects.setdefault(key, {})
            contents = {k: visit(v) for k, v in node['value'].items()}
            obj.clear()
            obj.update(contents)
        elif kind == 'list':
            obj = objects.setdefault(key, [])
            obj[:] = [visit(v) for v in node['value']]
        elif kind == 'tuple':
            contents = tuple(visit(v) for v in node['value'])
            objects.setdefault(key, contents)
        elif kind in {'basket', 'intent', 'action', 'context'}:
            if key not in objects:
                cls = {'basket': Basket, 'action': ResolvedAction, 'context': ConversationContext}.get(kind)
                if kind == 'intent':
                    from chatbot_core.logic.cafe.intent_handler.base import get_intent
                    cls = get_intent(node['intent'])
                objects[key] = object.__new__(cls)
            attributes = dict(visit(node['value']))
            objects[key].__dict__.clear()
            objects[key].__dict__.update(attributes)
        elif kind == 'schema':
            from chatbot_core.llm import schemas
            cls = getattr(schemas, node['name'], None)
            if not isinstance(cls, type) or not issubclass(cls, BaseModel):
                raise CodecError('Unsupported replay schema')
            restored = cls.model_validate(node['value'])
            if key in objects:
                objects[key].__dict__.update(restored.__dict__)
            else:
                objects[key] = restored
        elif kind == 'identity':
            objects.setdefault(key, SimpleNamespace(**node['value']))
        else:
            raise CodecError(f'Unsupported replay kind: {kind}')
        visiting.remove(key)
        done.add(key)
        return objects[key]

    return visit(document['root'])


def encode(value):
    return Encoder().encode(value)
