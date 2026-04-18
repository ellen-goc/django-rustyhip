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
    # migrate still runs even though we don't offer real rollback — failed
    # migrations leave partial schema, documented in the README.
