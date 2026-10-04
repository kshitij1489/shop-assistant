from . import modifier_views
from . import menu_source
from . import location_views
from django.urls import path, include
from .views import (
    signup_view, SignInView, LogoutView,
    impersonate_tenant_view, stop_impersonation_view,
    create_tenant_view, delete_tenant_view, tenant_settings_view,
    master_tenants_view, set_tenant_active_view, update_tenant_details_view,
    dashboard_redirect_view, master_dashboard_view, tenant_dashboard_view,
    tenant_activity_view, generate_jwt_token_view,
    pending_review_view, approve_tenant_view, reject_tenant_view,
)

# Tenant sub-dashboard views
from .views import (
    tenant_analytics_view, tenant_orders_view, tenant_campaigns_view,
    tenant_users_view, tenant_knowledge_view, tenant_billing_view, tenant_menu_view,
    tenant_menu_category_save_view, tenant_menu_category_delete_view,
    tenant_menu_item_update_view, tenant_menu_variant_add_view, 
    tenant_menu_variant_update_view, tenant_menu_variant_delete_view, tenant_menu_item_detail_view,
    # NEW imports
    tenant_menu_ingest_json_view, tenant_menu_item_catalog_update_view, upload_knowledge_prompt_view
)

# Tenant live chat views
from .views import (
    tenant_chats_view, tenant_chats_list_api, tenant_chats_toggle_api, tenant_chats_messages_api, tenant_chats_send_api,
    tenant_chats_global_status_api, tenant_chats_toggle_global_api, voice_assistant, voice_messages_api
)

tenant_dashboard_patterns = [
    path('menu/source/', menu_source.settings_view, name='menu_source'),
    path('menu/modifiers/', modifier_views.modifiers, name='modifiers'),
    path('menu/modifiers/<int:group_id>/', modifier_views.modifiers, name='modifier_edit'),
    path('menu/item/<uuid:item_id>/modifiers/', modifier_views.item_modifiers, name='item_modifiers'),
    path('menu/item/<uuid:item_id>/modifiers/<int:link_id>/', modifier_views.item_modifiers, name='item_modifier_edit'),
    path('', tenant_dashboard_view, name='tenant_dashboard'),
    path('analytics/', tenant_analytics_view, name='tenant_analytics'),
    path('orders/', tenant_orders_view, name='tenant_orders'),
    path('campaigns/', tenant_campaigns_view, name='tenant_campaigns'),
    path('users/', tenant_users_view, name='tenant_users'),
    path('knowledge/', tenant_knowledge_view, name='tenant_knowledge'),
    path("upload-knowledge/", upload_knowledge_prompt_view, name="upload_knowledge_prompt"),
    path('billing/', tenant_billing_view, name='tenant_billing'),
    path('settings/', tenant_settings_view, name='tenant_settings'),
    path('settings/cities/', location_views.cities, name='site_location_cities'),
    path('activity/', tenant_activity_view, name='tenant_activity'),
    path('settings/generate-token/', generate_jwt_token_view, name='generate_jwt_token'),

    # Live chats
    path('chats/', tenant_chats_view, name='tenant_chats'),
    path('chats/api/list', tenant_chats_list_api, name='tenant_chats_list_api'),
    path('chats/api/toggle', tenant_chats_toggle_api, name='tenant_chats_toggle_api'),
    path('chats/api/messages', tenant_chats_messages_api, name='tenant_chats_messages_api'),
    path('chats/api/send', tenant_chats_send_api, name='tenant_chats_send_api'), 
    path('chats/api/global/status', tenant_chats_global_status_api, name='tenant_chats_global_status_api'),
    path('chats/api/global/toggle', tenant_chats_toggle_global_api, name='tenant_chats_toggle_global_api'),

    # Voice assistant (tenant-scoped) ---
    path('assistant/', voice_assistant, name='voice_assistant'),
    path('voice/messages/', voice_messages_api, name='voice_messages_api'),

    # Menu pages
    path('menu/', tenant_menu_view, name='tenant_menu'),
    path('menu/ingest-json', tenant_menu_ingest_json_view, name='tenant_menu_ingest_json'),  # NEW

    path('menu/category/add', tenant_menu_category_save_view, name='tenant_menu_category_add'),
    path('menu/category/<int:category_id>/update', tenant_menu_category_save_view, name='tenant_menu_category_update'),
    path('menu/category/<int:category_id>/delete', tenant_menu_category_delete_view, name='tenant_menu_category_delete'),
    # Menu item + variants
    path('menu/item/<uuid:item_id>/', tenant_menu_item_detail_view, name='tenant_menu_item_detail'),
    path('menu/item/<uuid:item_id>/update', tenant_menu_item_update_view, name='tenant_menu_item_update'),
    path('menu/item/<uuid:item_id>/catalog/update', tenant_menu_item_catalog_update_view, name='tenant_menu_item_catalog_update'),  # NEW
    path('menu/item/<uuid:item_id>/variant/add', tenant_menu_variant_add_view, name='tenant_menu_variant_add'),
    path('menu/variant/<uuid:variant_id>/update', tenant_menu_variant_update_view, name='tenant_menu_variant_update'),
    path('menu/variant/<uuid:variant_id>/delete', tenant_menu_variant_delete_view, name='tenant_menu_variant_delete'),

]

urlpatterns = [
    # Authentication
    path('signup/', signup_view, name='signup'),
    path('login/', SignInView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),

    # Unified dashboard redirect
    path('dashboard/', dashboard_redirect_view, name='dashboard'),

    # Master dashboard
    path('master-dashboard/', master_dashboard_view, name='master_dashboard'),
    path('tenants/', master_tenants_view, name='master_tenants'),
    path('tenants/<int:tenant_id>/active/', set_tenant_active_view, name='set_tenant_active'),
    path('tenants/<int:tenant_id>/details/', update_tenant_details_view, name='update_tenant_details'),

    # NEW: Pending page
    path('pending/', pending_review_view, name='pending_review'),

    # NEW: Master approval actions
    path('approve-tenant/<int:tenant_id>/', approve_tenant_view, name='approve_tenant'),
    path('reject-tenant/<int:tenant_id>/', reject_tenant_view, name='reject_tenant'),

    # Tenant dashboard and sub-pages
    path('tenant-dashboard/', include((tenant_dashboard_patterns, 'tenant'))),

    # Master actions
    path('impersonate/<int:tenant_id>/', impersonate_tenant_view, name='impersonate_tenant'),
    path('stop-impersonation/', stop_impersonation_view, name='stop_impersonation'),
    path('create-tenant/', create_tenant_view, name='create_tenant'),
    path('delete-tenant/<int:tenant_id>/', delete_tenant_view, name='delete_tenant'),
]
