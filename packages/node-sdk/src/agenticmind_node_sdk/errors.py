"""Errors raised by node handlers.

Error classes drive retry behaviour: only ``retryable`` errors are retried, and only within the
node's execution policy.
"""

from __future__ import annotations


class NodeError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class TransientNodeError(NodeError):
    """A failure that is safe and sensible to retry (rate limit, temporary network error...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=True)


class SecretAccessError(NodeError):
    def __init__(self, message: str) -> None:
        super().__init__("SECRET_ACCESS_DENIED", message)


class SuspendNode(Exception):  # noqa: N818 - a control signal, not an error
    """Raised by a node declared ``can_suspend`` to pause the run until someone responds (ADR-0013).

    The runtime records ``request`` (what to show the person) and ``response_schema`` (JSON Schema the
    response must satisfy), checkpoints the run and ends it with status ``waiting``. When the run is
    resumed, the node is invoked again with the response in ``ctx.resume_value``.
    """

    def __init__(self, request: dict, response_schema: dict | None = None) -> None:
        super().__init__(request.get("title", "Waiting for input"))
        self.request = request
        self.response_schema = response_schema
