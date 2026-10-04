from django.urls import path
from . import api
from .dashboard import settings_view, connections_view, stock_view, operations_view, issue_view
app_name = 'commerce'
urlpatterns = [
    path('operations/', operations_view, name='operations'),
    path('issues/<uuid:issue_id>/', issue_view, name='issue'),
    path('settings/', settings_view, name='settings'),
    path('connections/', connections_view, name='connections'),
    path('connections/<uuid:connection_id>/', connections_view, name='connection_edit'),
    path('stock/', stock_view, name='stock'),
    path('stock/<uuid:stock_id>/', stock_view, name='stock_edit'),
    path('v1/connections/<uuid:connection_id>/events/', api.events),
    path('v1/connections/<uuid:connection_id>/events/<str:event_id>/', api.event_status),
    path('v1/connections/<uuid:connection_id>/manifest/', api.manifest),
    path('v1/connections/<uuid:connection_id>/commands/claim/', api.commands),
    path('v1/connections/<uuid:connection_id>/commands/<uuid:command_id>/ack/', api.ack),
    path('v1/connections/<uuid:connection_id>/schema/', api.schema),
    path('v1/connections/<uuid:connection_id>/mappings/', api.mappings),
    path('v1/connections/<uuid:connection_id>/catalog/', api.catalog),
    path('v1/connections/<uuid:connection_id>/catalog/snapshot/', api.menu_snapshot),
    path('v1/connections/<uuid:connection_id>/stock/', api.stock),
    path('v1/connections/<uuid:connection_id>/orders/<uuid:order_id>/', api.order_snapshot),
]
