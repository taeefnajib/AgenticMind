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
