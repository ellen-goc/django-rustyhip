"""Django DB backend — subclasses django.db.backends.sqlite3 but swaps the
sqlite3 Python module for a thin HTTP client that POSTs to rustyhip's /sql.

Django's SQLite dialect, schema editor, introspection, and features stay
intact; only the connection + cursor layer is overridden.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Iterable, Iterator

from django.db.backends.sqlite3 import base as sqlite3_base

from .features import DatabaseFeatures

logger = logging.getLogger("rustyhip")

_PRAGMA_RE = re.compile(
    r"^\s*PRAGMA\s+(?P<name>\w+)\s*(?:=\s*(?P<value>.+?))?\s*;?\s*$",
    re.IGNORECASE,
)

# Matches a `%s` placeholder that is NOT part of a literal `%%s`. Mirrors
# Django's own SQLiteCursorWrapper.FORMAT_QMARK_REGEX — when params are
# present we rewrite these to SQLite's `?` qmark style before POSTing.
_FORMAT_QMARK_RE = re.compile(r"(?<!%)%s")

# SQL statements that our server-side connection can't honor (no cross-call
# transaction state — each /sql call opens a fresh SQLite connection). We
# match on the first keyword and swallow them at the client.
_TRANSACTION_KEYWORDS = (
    "BEGIN",
    "COMMIT",
    "END",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
)


class Error(Exception):
    """Base DB-API exception."""


class Warning(Exception):  # noqa: N818  (DB-API requires this exact name)
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


class Database:
    """Stand-in for Python's ``sqlite3`` module. Django calls
    ``DatabaseWrapper.Database.connect(...)`` to build a connection — we return
    an HTTP-backed one instead.
    """

    Error = Error
    Warning = Warning
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError

    # Django's SQLite backend references these at import time. Real sqlite3
    # defines them as ints; they don't affect us, just need to exist.
    PARSE_DECLTYPES = 1
    PARSE_COLNAMES = 2

    apilevel = "2.0"
    threadsafety = 1
    paramstyle = "qmark"

    @staticmethod
    def connect(endpoint: str, timeout: float = 30.0, **_: Any) -> "RustyhipConnection":
        return RustyhipConnection(endpoint=endpoint, timeout=timeout)

    @staticmethod
    def register_converter(*_: Any, **__: Any) -> None:
        """No-op — we don't route values through Python's sqlite3 adapter layer."""

    @staticmethod
    def register_adapter(*_: Any, **__: Any) -> None:
        """No-op."""

    @staticmethod
    def enable_callback_tracebacks(*_: Any, **__: Any) -> None:
        """No-op."""


class RustyhipConnection:
    """Mimics just enough of sqlite3.Connection for Django's SQLite backend."""

    def __init__(self, endpoint: str, timeout: float = 30.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.closed = False
        # Django sets isolation_level to None to opt into manual tx mode. We
        # honor reads/writes of the attribute but don't act on it.
        self.isolation_level: str | None = None
        self.in_transaction = False
        self.row_factory: Any = None

    def cursor(self, factory: Any = None) -> "RustyhipCursor":
        return RustyhipCursor(self)

    # Convenience passthrough used by Django for a couple of pragmas at connect time.
    def execute(self, sql: str, params: Iterable[Any] | None = None) -> "RustyhipCursor":
        cur = self.cursor()
        cur.execute(sql, params or ())
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> "RustyhipCursor":
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def create_function(self, name: str, narg: int, func: Any, **_: Any) -> None:
        """No-op. Django registers a handful of SQLite UDFs via this method
        (``django_date_extract``, ``django_timestamp_diff``, etc.). Our
        server-side SQLite doesn't have them; queries that use them will fail
        with "no such function" — acceptable for the migrate happy path.
        """

    def create_collation(self, *_: Any, **__: Any) -> None:
        """No-op."""

    def getlimit(self, limit_id: int) -> int:
        """Mimic ``sqlite3.Connection.getlimit``. Django calls this to size
        bulk-insert batches via ``SQLITE_LIMIT_VARIABLE_NUMBER`` (id=9). Return
        SQLite's compile-time default so Django picks a reasonable batch size.
        """
        # SQLITE_LIMIT_VARIABLE_NUMBER — default 999 pre-3.32, 32766 since.
        if limit_id == 9:
            return 999
        return 0

    def commit(self) -> None:
        """Swallowed — each /sql call auto-commits server-side."""

    def rollback(self) -> None:
        """Swallowed — no cross-call transaction state on the server."""

    def close(self) -> None:
        self.closed = True

    # Context manager protocol (sqlite3.Connection is one).
    def __enter__(self) -> "RustyhipConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Mirror sqlite3 behavior: commit on success, rollback on error. Both are no-ops.
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


class RustyhipCursor:
    """DB-API 2.0 cursor backed by rustyhip's ``POST /sql`` endpoint."""

    arraysize = 1

    def __init__(self, conn: RustyhipConnection) -> None:
        self.conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self._row_iter: Iterator[tuple[Any, ...]] | None = None
        self.rowcount: int = -1
        self.lastrowid: int | None = None
        self.description: list[tuple[str, None, None, None, None, None, None]] | None = None

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> "RustyhipCursor":
        if _is_transaction_stmt(sql):
            # Silently succeed — we fake client-side transaction management.
            self._reset_result()
            return self
        if self._intercept_pragma(sql):
            return self
        if params is not None:
            sql = _convert_format_to_qmark(sql)
        payload = _build_payload(sql, params)
        response = self._post(payload)
        self._ingest(response)
        return self

    def _intercept_pragma(self, sql: str) -> bool:
        """Handle PRAGMAs client-side. Our remote server opens a fresh SQLite
        connection per /sql call, so setter pragmas (``PRAGMA x = ...``) would
        never stick. Reader pragmas that Django inspects (notably
        ``PRAGMA foreign_keys``) get synthetic answers so the SQLite schema
        editor proceeds.

        Returns True if the pragma was handled locally; False if the caller
        should fall through to the HTTP POST.
        """
        m = _PRAGMA_RE.match(sql)
        if not m:
            return False
        name = m.group("name").lower()
        is_setter = m.group("value") is not None
        if is_setter:
            # Swallow all PRAGMA writes — they don't persist across /sql calls.
            self._reset_result()
            return True
        if name == "foreign_keys":
            # Report FK as OFF — rustyhip doesn't enforce FK across calls anyway.
            self._rows = [(0,)]
            self._row_iter = iter(self._rows)
            self.description = [("foreign_keys", None, None, None, None, None, None)]
            self.rowcount = 1
            self.lastrowid = None
            return True
        if name == "foreign_key_check":
            # Report no violations — we're not enforcing FK at the rustyhip layer.
            self._rows = []
            self._row_iter = iter(self._rows)
            self.description = [
                ("table", None, None, None, None, None, None),
                ("rowid", None, None, None, None, None, None),
                ("parent", None, None, None, None, None, None),
                ("fkid", None, None, None, None, None, None),
            ]
            self.rowcount = 0
            self.lastrowid = None
            return True
        # Other reader pragmas go through (e.g., `PRAGMA table_info(...)` for introspection).
        return False

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> "RustyhipCursor":
        total_rowcount = 0
        for params in seq_of_params:
            self.execute(sql, params)
            if self.rowcount > 0:
                total_rowcount += self.rowcount
        # DB-API says rowcount on executemany is the sum of affected rows.
        self.rowcount = total_rowcount
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._row_iter is None:
            return None
        return next(self._row_iter, None)

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        if self._row_iter is None:
            return []
        count = size or self.arraysize
        out: list[tuple[Any, ...]] = []
        for _ in range(count):
            row = next(self._row_iter, None)
            if row is None:
                break
            out.append(row)
        return out

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self._row_iter is None:
            return []
        return list(self._row_iter)

    def close(self) -> None:
        self._row_iter = None
        self._rows = []

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self._rows)

    # ----- internals -----

    def _reset_result(self) -> None:
        self._rows = []
        self._row_iter = iter(())
        self.rowcount = -1
        self.lastrowid = None
        self.description = None

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310  (endpoint is operator-configured)
            f"{self.conn.endpoint}/sql",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.conn.timeout) as resp:  # noqa: S310
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # 4xx/5xx — rustyhip returns JSON error bodies for 400s.
            raw = e.read() if hasattr(e, "read") else b""
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OperationalError(f"rustyhip POST failed: {e}") from e

        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as e:
            raise OperationalError(f"rustyhip returned non-JSON body (status={status}): {raw!r}") from e

        if status >= 400:
            msg = data.get("error") if isinstance(data, dict) else str(data)
            _raise_for_sql_error(status, msg or f"rustyhip returned HTTP {status}")
        return data

    def _ingest(self, data: dict[str, Any]) -> None:
        columns = data.get("columns") or []
        rows_raw = data.get("rows") or []
        self._rows = [tuple(row.get(col) for col in columns) for row in rows_raw]
        self._row_iter = iter(self._rows)
        self.description = (
            [(name, None, None, None, None, None, None) for name in columns] if columns else None
        )
        if data.get("readonly"):
            self.rowcount = len(self._rows)
        else:
            self.rowcount = int(data.get("rowcount") or 0)
        lastrowid = data.get("lastrowid")
        self.lastrowid = int(lastrowid) if lastrowid else None


def _is_transaction_stmt(sql: str) -> bool:
    # Strip leading whitespace + leading "--" line comments, then look at the first word.
    cursor = 0
    n = len(sql)
    while cursor < n:
        ch = sql[cursor]
        if ch.isspace():
            cursor += 1
            continue
        if ch == "-" and cursor + 1 < n and sql[cursor + 1] == "-":
            end = sql.find("\n", cursor)
            cursor = n if end == -1 else end + 1
            continue
        break
    first_word = sql[cursor:].split(None, 1)[0].upper() if cursor < n else ""
    return first_word in _TRANSACTION_KEYWORDS


def _build_payload(sql: str, params: Iterable[Any] | None) -> dict[str, Any]:
    return {"sql": sql, "params": list(params) if params is not None else []}


def _convert_format_to_qmark(sql: str) -> str:
    """Mirror ``django.db.backends.sqlite3.base.SQLiteCursorWrapper.convert_query``.

    Django's ORM compiles SQL using ``%s`` positional markers; Python's
    sqlite3 module uses ``?``. The built-in SQLite cursor wrapper rewrites
    placeholders at execute time. We do the same for parity.
    """
    return _FORMAT_QMARK_RE.sub("?", sql).replace("%%", "%")


def _json_default(value: Any) -> Any:
    # Django passes datetime / date / Decimal etc. through the cursor. Python's
    # sqlite3 module would stringify via registered adapters — we do the same
    # with a plain str() fallback, which SQLite treats as TEXT.
    if isinstance(value, (bytes, bytearray, memoryview)):
        # SQLite BLOB round-trip isn't supported by rustyhip today.
        raise NotSupportedError("rustyhip backend does not yet support BLOB parameters")
    return str(value)


def _raise_for_sql_error(status: int, msg: str) -> None:
    lowered = msg.lower()
    if "unique" in lowered or "constraint" in lowered:
        raise IntegrityError(msg)
    if "syntax" in lowered or "no such" in lowered:
        raise ProgrammingError(msg)
    if status == 400:
        raise OperationalError(msg)
    raise DatabaseError(f"HTTP {status}: {msg}")


class DatabaseWrapper(sqlite3_base.DatabaseWrapper):
    """Connection wrapper. Inherits Django's SQLite schema editor / introspection /
    operations, but replaces the sqlite3 module with our HTTP shim.
    """

    Database = Database  # Django uses this for exception hierarchy lookups.
    vendor = "sqlite"
    display_name = "Rustyhip"
    features_class = DatabaseFeatures

    def get_connection_params(self) -> dict[str, Any]:
        conf = self.settings_dict
        options = conf.get("OPTIONS") or {}
        endpoint = options.get("endpoint") or options.get("ENDPOINT")
        if not endpoint:
            raise ImproperlyConfigured(
                "django-rustyhip requires DATABASES['default']['OPTIONS']['endpoint'] "
                "(e.g. 'http://localhost:9000')."
            )
        return {
            "endpoint": endpoint,
            "timeout": float(options.get("timeout", 30.0)),
        }

    def get_new_connection(self, conn_params: dict[str, Any]) -> RustyhipConnection:
        return self.Database.connect(**conn_params)

    def init_connection_state(self) -> None:
        """Django's SQLite backend runs pragmas here. Our remote server opens
        a fresh SQLite connection per /sql call, so per-connection pragmas
        (like ``PRAGMA foreign_keys=ON``) wouldn't stick. Skip them.
        """
        return None

    def create_cursor(self, name: str | None = None) -> RustyhipCursor:
        return self.connection.cursor()

    def _set_autocommit(self, autocommit: bool) -> None:
        # Every /sql call is auto-committed server-side; nothing to toggle.
        self.autocommit = autocommit

    def is_usable(self) -> bool:
        return not self.connection.closed if self.connection is not None else False

    def _start_transaction_under_autocommit(self) -> None:
        """Called by Django when it wants to begin a manual transaction in
        autocommit mode. No-op for us — transactions are faked client-side.
        """


# Imported late to avoid a circular import with django.conf during module init.
from django.core.exceptions import ImproperlyConfigured  # noqa: E402
