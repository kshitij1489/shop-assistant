# chatbot_core/apps.py
from django.apps import AppConfig
import logging
from importlib import import_module
import pkgutil

class ChatbotCoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chatbot_core'

    def ready(self):
        # 1) initialize caches (your existing code)
        try:
            from chatbot_core.knowledge_cache import initialize_caches
            initialize_caches()
            logging.info("✅ Caches loaded successfully at startup.")
        except Exception as e:
            logging.exception("❌ Failed to load chatbot caches: %s", e)

        # 2) force-load all channel modules so they call register_adapter(...)
        try:
            channels_pkg = import_module("chatbot_core.channels")
            for m in pkgutil.iter_modules(channels_pkg.__path__):
                mod_name = f"chatbot_core.channels.{m.name}"
                import_module(mod_name)
            logging.info("✅ Channel modules imported and adapters registered.")
        except Exception as e:
            logging.exception("❌ Failed importing channel modules: %s", e)

        from chatbot_core.vector_store.faiss_index import rebuild_faiss_from_db
        try:
            rebuild_faiss_from_db()
        except Exception as e:
            logging.getLogger(__name__).warning("FAISS rebuild skipped: %s", e)