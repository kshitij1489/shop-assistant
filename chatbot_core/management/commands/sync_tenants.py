import os
import yaml
import json
import logging
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from chatbot_core.models import TenantInfo
from mongoengine import connect
from chatbot_core.mongo_models import IntentPrompt, KnowledgeBase

TENANT_DIR = os.path.join(settings.BASE_DIR, 'tenants')

class Command(BaseCommand):
    help = "Sync tenant metadata and associated datasets like intents and knowledge base"

    def handle(self, *args, **kwargs):
        if not settings.MONGO_DB_URL:
            raise CommandError('Legacy MongoDB sync requires MONGO_DB_URL. New installations use PostgreSQL drafts and Knowledge → Publish.')
        self.stderr.write('Legacy sync writes MongoDB only; it does not publish PostgreSQL runtime configuration.')
        connect(host=settings.MONGO_DB_URL)

        if not os.path.exists(TENANT_DIR):
            self.stdout.write(self.style.WARNING(f"Tenant directory not found: {TENANT_DIR}"))
            return

        tenant_folders = [d for d in os.listdir(TENANT_DIR)
                          if os.path.isdir(os.path.join(TENANT_DIR, d))]

        for tenant_slug in tenant_folders:
            self.stdout.write(f"Processing tenant '{tenant_slug}'...")
            tenant_obj = self.sync_tenant_metadata(tenant_slug)
            if tenant_obj:
                self.sync_intents(tenant_slug, tenant_obj)
                self.sync_knowledge(tenant_slug, tenant_obj)
            else:
                self.stdout.write(self.style.WARNING(
                    f"Skipping dataset sync for '{tenant_slug}' due to missing tenant metadata."
                ))

    def sync_tenant_metadata(self, tenant_slug):
        config_path = os.path.join(TENANT_DIR, tenant_slug, 'config.yaml')
        if not os.path.exists(config_path):
            self.stdout.write(self.style.WARNING(f"Skipping {tenant_slug}: no config.yaml"))
            return None

        with open(config_path) as f:
            config = yaml.safe_load(f)

        if config.get('business_type', 'cafe') != 'cafe':
            self.stderr.write('Only café/restaurant tenants are supported; skipping ' + tenant_slug)
            return None

        tenant_obj, created = TenantInfo.objects.update_or_create(
            slug=tenant_slug,
            defaults={
                "display_name": config.get("display_name", tenant_slug),
                "business_type": config.get("business_type", "cafe"),
                "description": config.get("description", "")
            }
        )

        status = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{status} tenant '{tenant_slug}'"))
        return tenant_obj

    def sync_intents(self, tenant_slug, tenant_obj):
        intents_path = os.path.join(TENANT_DIR, tenant_slug, 'prompt_intent.json')
        if not os.path.exists(intents_path):
            self.stdout.write(self.style.WARNING(
                f"No prompt_intent.json found for tenant '{tenant_slug}', skipping intents load"
            ))
            return

        with open(intents_path) as f:
            try:
                intents_data = json.load(f)
            except json.JSONDecodeError as e:
                self.stdout.write(self.style.ERROR(
                    f"Failed to parse prompt_intent.json for '{tenant_slug}': {e}"
                ))
                return

        api_key = tenant_obj.api_key
        slug = tenant_obj.slug

        for intent, sub_intents in intents_data.items():
            for sub_intent, prompt in sub_intents.items():
                if not prompt:
                    self.stdout.write(self.style.WARNING(
                        f"Skipping empty prompt for {intent}.{sub_intent}"
                    ))
                    continue

                if not isinstance(prompt, str):
                    self.stdout.write(self.style.WARNING(
                        f"Skipping non-string prompt for {intent}.{sub_intent}: got {type(prompt).__name__}"
                    ))
                    continue

                try:
                    IntentPrompt.objects(api_key=api_key, sub_intent=sub_intent).update_one(
                        set__slug=slug,
                        set__intent=intent,
                        set__prompt=prompt,
                        upsert=True
                    )
                except Exception as ex:
                    logging.exception(f"Failed to upsert intent for tenant '{tenant_slug}': {ex}")
                    self.stdout.write(self.style.ERROR(
                        f"Error saving intent for {intent}.{sub_intent}: {ex}"
                    ))

        self.stdout.write(self.style.SUCCESS(f"Loaded intents for tenant '{tenant_slug}'"))


    def sync_knowledge(self, tenant_slug, tenant_obj):
        knowledge_path = os.path.join(TENANT_DIR, tenant_slug, 'knowledge_base.json')
        if not os.path.exists(knowledge_path):
            self.stdout.write(self.style.WARNING(
                f"No knowledge_base.json found for tenant '{tenant_slug}', skipping knowledge sync"
            ))
            return

        with open(knowledge_path) as f:
            try:
                knowledge_data = json.load(f)
            except json.JSONDecodeError as e:
                self.stdout.write(self.style.ERROR(
                    f"Failed to parse knowledge_base.json for '{tenant_slug}': {e}"
                ))
                return

        try:
            api_key = tenant_obj.api_key
            slug = tenant_obj.slug

            for intent, sub_intents in knowledge_data.items():
                for sub_intent, data in sub_intents.items():
                    if not isinstance(data, dict):
                        data = {}  # Ensure it's a dict to satisfy DictField

                    KnowledgeBase.objects(api_key=api_key, sub_intent=sub_intent).update_one(
                        set__slug=slug,
                        set__intent=intent,
                        set__data=data,
                        upsert=True
                    )
            self.stdout.write(self.style.SUCCESS(
                f"Loaded knowledge_base for tenant '{tenant_slug}'"
            ))
        except Exception as ex:
            logging.exception(f"Failed to upsert knowledge for tenant '{tenant_slug}': {ex}")
            self.stdout.write(self.style.ERROR(
                f"Error saving knowledge for tenant '{tenant_slug}': {ex}"
            ))
