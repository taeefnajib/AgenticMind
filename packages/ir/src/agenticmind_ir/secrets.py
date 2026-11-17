"""Secret reference helpers.

Secrets never appear as literal values in the IR. A config value that needs a credential holds
a reference object ``{"$secret": "NAME"}`` which the runtime resolves at execution time.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

SECRET_REF_KEY = "$secret"
SECRET_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{0,127}$"
_SECRET_NAME_RE = re.compile(SECRET_NAME_PATTERN)


def is_secret_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value.keys()) == {SECRET_REF_KEY}
        and isinstance(value[SECRET_REF_KEY], str)
        and bool(_SECRET_NAME_RE.match(value[SECRET_REF_KEY]))
    )


def secret_ref(name: str) -> dict[str, str]:
    if not _SECRET_NAME_RE.match(name):
        raise ValueError(f"Invalid secret name {name!r}; expected {SECRET_NAME_PATTERN}")
    return {SECRET_REF_KEY: name}


def iter_secret_refs(value: Any) -> Iterator[str]:
    """Yield every secret name referenced anywhere inside ``value``."""
    if is_secret_ref(value):
        yield value[SECRET_REF_KEY]
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_secret_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_secret_refs(v)
