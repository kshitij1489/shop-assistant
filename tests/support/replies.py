"""Presentation boundary for tests of routing, handlers and saved business state."""
from unittest.mock import patch


def verified_reply(*, response, question, **context):
    """Keep the graph's verified wording and selected question without model I/O."""
    return response, question


def install_reply_renderer(testcase):
    """Renderer behavior itself is exercised separately with an offline provider."""
    return testcase.enterContext(patch(
        'chatbot_core.logic.cafe.workflow.graph.render_reply', side_effect=verified_reply))
