"""Flush-path override for rustyhip's turbolite-backed SQLite.

Turbolite's tiered VFS enables ``PRAGMA foreign_keys`` by default, which is
the opposite of stock SQLite (FK off unless explicitly enabled). Django's
stock SQLite flush path doesn't disable FK around the DELETE cascade — it
assumes stock-SQLite's off-by-default behavior. Against turbolite that
crashes on ``FOREIGN KEY constraint failed``.

We delegate the FK toggle to Django's own ``disable_constraint_checking`` /
``enable_constraint_checking`` hooks (which issue the same PRAGMAs) so the
mechanism is visible to anyone reading Django's abstractions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.backends.sqlite3.operations import DatabaseOperations as SqliteOperations

if TYPE_CHECKING:
    from collections.abc import Iterable


class DatabaseOperations(SqliteOperations):
    def execute_sql_flush(self, sql_list: Iterable[str]) -> None:  # type: ignore[override]
        disabled = self.connection.disable_constraint_checking()
        try:
            super().execute_sql_flush(sql_list)
        finally:
            if disabled:
                self.connection.enable_constraint_checking()
