"""Most API tests exercise features, not sign-in: they run with ``AGENTICMIND_AUTH=off``.

``test_auth.py`` turns sign-in on explicitly (``Settings(auth_mode="required")``).
"""

import pytest


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch):
    monkeypatch.setenv("AGENTICMIND_AUTH", "off")
