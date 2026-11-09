"""A controlled wall clock for Python and each isolated PostgreSQL test database."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


class TestClock:
    def __init__(self, at: datetime) -> None:
        self.at = at
        self.started = time.monotonic()
        self.connection_info: str | None = None

    def now(self) -> datetime:
        return self.at + timedelta(seconds=time.monotonic() - self.started)

    def set(self, at: datetime) -> None:
        self.at, self.started = at, time.monotonic()
        if self.connection_info is not None:
            self._set_database_clock()

    def patch(self, monkeypatch, *modules) -> None:
        clock = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                at = clock.now()
                return at.astimezone(tz) if tz else at.replace(tzinfo=None)

        for module in modules:
            monkeypatch.setattr(module, "datetime", Clock)

    def bind(self, connection_info: str) -> str:
        self.connection_info = connection_info
        self._set_database_clock()
        with psycopg.connect(connection_info) as connection:
            # Migration defaults bind to built-in functions when the template
            # is created. Rebind only time defaults in this disposable database
            # so inserts and explicit SQL reads share the test's clock.
            defaults = connection.execute(
                """
                SELECT c.relname, a.attname, pg_get_expr(d.adbin, d.adrelid)
                FROM pg_attrdef AS d
                JOIN pg_attribute AS a ON (a.attrelid, a.attnum) = (d.adrelid, d.adnum)
                JOIN pg_class AS c ON c.oid = d.adrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'python_domain'
                  AND pg_get_expr(d.adbin, d.adrelid) ~ '(clock_timestamp|now|statement_timestamp)\\('
                """
            ).fetchall()
            connection.execute("SET search_path = python_domain, pg_catalog")
            for table, column, expression in defaults:
                connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET DEFAULT " + expression).format(
                    sql.Identifier(table), sql.Identifier(column)))
        return make_conninfo(connection_info, options="-c search_path=python_domain,pg_catalog")

    def _set_database_clock(self) -> None:
        with psycopg.connect(self.connection_info) as connection:
            actual = connection.execute("SELECT pg_catalog.clock_timestamp()").fetchone()[0]
            current = self.now()
            for name in ("clock_timestamp", "now", "statement_timestamp"):
                body = sql.SQL("SELECT {}::timestamptz + (pg_catalog.clock_timestamp() - {}::timestamptz)").format(
                    sql.Literal(current), sql.Literal(actual)).as_string(connection)
                connection.execute(sql.SQL(
                    "CREATE OR REPLACE FUNCTION python_domain.{}() RETURNS timestamptz "
                    "LANGUAGE SQL VOLATILE AS {}"
                ).format(sql.Identifier(name), sql.Literal(body)))
