"""SQL dialect + flush overrides for rustyhip's turbolite-backed SQLite.

Turbolite's tiered VFS enables ``PRAGMA foreign_keys`` by default, which is
the opposite of stock SQLite (FK off unless explicitly enabled). Django's
SQLite backend assumes stock-SQLite's off-by-default behavior in its flush
path — ``DELETE FROM parent_table`` then fails with
``FOREIGN KEY constraint failed`` because child rows still exist.

We wrap the flush statement list with explicit FK-off / FK-on bookends so
test isolation (and any programmatic ``flush`` calls) works regardless of
turbolite's connection-open defaults.
"""

from __future__ import annotations

from typing import Iterable

from django.db.backends.sqlite3.operations import DatabaseOperations as SqliteOperations


class DatabaseOperations(SqliteOperations):
    def execute_sql_flush(self, sql_list: Iterable[str]) -> None:  # type: ignore[override]
        """Execute a flush statement list with FK enforcement disabled."""
        with self.connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys = OFF")
            try:
                for sql in sql_list:
                    cursor.execute(sql)
            finally:
                cursor.execute("PRAGMA foreign_keys = ON")
