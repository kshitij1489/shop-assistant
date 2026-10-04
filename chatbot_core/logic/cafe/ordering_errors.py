"""A rejected operation is terminal; a missing choice can be clarified."""


from chatbot_core.logic.action_resolver import TerminalRejection


class OrderingRejected(TerminalRejection):
    """The requested change violates ordering policy and must not be retained."""
