"""pytest configuration — makes Django settings available for imports that
touch `django.db.backends.sqlite3` at module load time.
"""

from __future__ import annotations

import django
from django.conf import settings


def pytest_configure() -> None:
    if not settings.configured:
        settings.configure(
            INSTALLED_APPS=[],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            USE_TZ=True,
        )
        django.setup()
