"""Embedding model loading must not block Celery child startup."""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from celery import signals
from django.test import SimpleTestCase

from chatbot_core import tasks
from chatbot_core.vector_store import embedding_client, embeddings


class EmbeddingLifecycleTests(SimpleTestCase):
    def setUp(self):
        self.model = Mock()
        self.model.encode.return_value = np.array([[3.0, 4.0]], dtype="float32")
        self.constructor = Mock(return_value=self.model)
        self.enterContext(patch.dict("sys.modules", {
            "sentence_transformers": SimpleNamespace(SentenceTransformer=self.constructor),
        }))
        self.enterContext(patch.dict(os.environ, {
            "EMBEDDING_MODEL": "test-embedding-model", "HF_HOME": "/tmp/test-model-cache",
        }))
        self.enterContext(patch.object(embeddings, "_MODEL", None))

    def test_worker_startup_does_not_load_the_model(self):
        signals.worker_process_init.send(sender=None)
        self.constructor.assert_not_called()

    def test_inline_and_task_embeddings_share_one_lazy_model(self):
        self.constructor.assert_not_called()
        inline = embedding_client.get_embedding("Menu question")
        queued = tasks.embed_text.run("Cafe question")
        np.testing.assert_allclose(inline, [0.6, 0.8])
        np.testing.assert_allclose(queued, inline)
        self.constructor.assert_called_once_with(
            "test-embedding-model", cache_folder="/tmp/test-model-cache", device="cpu",
        )
        self.assertEqual(self.model.encode.call_count, 2)

    def test_failed_model_load_can_be_retried_on_the_next_request(self):
        self.constructor.side_effect = [RuntimeError("Model unavailable"), self.model]
        with self.assertRaisesMessage(RuntimeError, "Model unavailable"):
            embedding_client.get_embedding("First attempt")
        np.testing.assert_allclose(embedding_client.get_embedding("Retry"), [0.6, 0.8])
        self.assertEqual(self.constructor.call_count, 2)
