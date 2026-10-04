from django.apps import AppConfig
from django.contrib.admin import apps as admin_apps


class OperationsAdminConfig(admin_apps.AdminConfig):
    default = False
    default_site = 'users.admin_site.OperationsAdminSite'


class UsersConfig(AppConfig):
    default = True
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'users'
