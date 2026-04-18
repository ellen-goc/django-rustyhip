"""Feature flags that narrow Django's SQLite backend to fit rustyhip's
single-statement-per-call, no-cross-call-state execution model.
"""

from django.db.backends.sqlite3.features import DatabaseFeatures as SqliteFeatures


class DatabaseFeatures(SqliteFeatures):
    # Each rustyhip /sql call opens a fresh server-side SQLite connection.
    # Real savepoints / transactions can't span calls, so we tell Django not
    # to rely on them. Our cursor swallows BEGIN/SAVEPOINT/etc. regardless —
    # this just stops Django from issuing advanced tx patterns that would
    # surprise us.
    uses_savepoints = False
    can_release_savepoints = False
    atomic_transactions = False
    # Tell Django we don't support transactions at all. Consequence: Django's
    # `TestCase` falls back to `TransactionTestCase` behavior (flush-based
    # fixture teardown between test methods instead of transaction rollback).
    # Without this, tests that re-run the same `setUp` across methods hit
    # UNIQUE constraint violations because seed data is never rolled back.
    supports_transactions = False
    # The stock SQLite backend uses `file:memorydb_X?mode=memory&cache=shared`
    # for its test DB. That makes no sense for us — there's only one rustyhip
    # endpoint, and the test DB == the real DB. Disable the in-memory shortcut
    # so Django honors an explicit TEST.NAME or falls back to settings NAME.
    can_share_in_memory_db = False
    # migrate still runs even though we don't offer real rollback — failed
    # migrations leave partial schema, documented in the README.
