# chatbot_core/tenant_handlers/dispatcher.py
from .cafe_handler import CafeHandler

TENANT_HANDLER_MAP = {
    "cafe": CafeHandler,
}

def get_handler(tenant_obj, session_store, request):
    handler_class = TENANT_HANDLER_MAP.get(tenant_obj.business_type)
    if not handler_class:
        raise NotImplementedError('Only café/restaurant workflows are supported. Additional workflows require development.')
    return handler_class(tenant_obj, session_store, request=request)
