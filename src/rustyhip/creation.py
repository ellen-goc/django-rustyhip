"""Test-database lifecycle overrides for the rustyhip backend.

Django's stock SQLite creation creates an in-memory DB per test run
(``file:memorydb_X?mode=memory&cache=shared``). That model doesn't fit
rustyhip — the "database" is a single long-lived S3-backed instance shared
by all callers. We no-op database creation + destruction and rely on flush
for per-test isolation (enabled by ``supports_transactions = False`` in
features.py).

It is the operator's job to point the test run at a rustyhip *endpoint*
that is safe to clear between runs (e.g. a throwaway floci-backed local
instance, not production).
"""

from __future__ import annotations

from typing import Any

from django.db.backends.sqlite3.creation import DatabaseCreation as SqliteCreation


class DatabaseCreation(SqliteCreation):
    def _get_test_db_name(self) -> str:
        # Use the configured NAME verbatim. Never switch to :memory:.
        return self.connection.settings_dict.get("NAME") or "rustyhip"

    def _create_test_db(self, verbosity: int, autoclobber: bool, keepdb: bool = False) -> str:
        # Nothing to create — the rustyhip endpoint is already live.
        return self._get_test_db_name()

    def _destroy_test_db(self, test_database_name: str, verbosity: int) -> None:
        # Nothing to destroy — the endpoint persists for later runs.
        return None

    def is_in_memory_db(self, database_name: Any) -> bool:
        # We never run against an in-memory SQLite, even if Django's default
        # machinery thinks we might.
        return False
