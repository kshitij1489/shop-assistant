class BaseTenantHandler:
    def __init__(self, tenant_obj, request=None):
        self.tenant = tenant_obj
        self.request = request

    def handle_message(self, message, customer=None):
        raise NotImplementedError("Must override handle_message()")
