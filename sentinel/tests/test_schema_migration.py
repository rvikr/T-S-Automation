"""Unit tests for sentinel/tools/audit_log.py schema initialisation and migrations.

Verifies that init_db() is idempotent and that _ensure_column() correctly
adds missing columns to an existing database.  All tests use temporary files
and are fully offline.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from sentinel.tools.audit_log import _ensure_column, db_connection, init_db


class InitDbIdempotencyTests:
    def test_init_db_creates_all_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "audit.sqlite"
            init_db(db_path)

            with db_connection(db_path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            assert {"audits", "tickets", "api_keys", "precedents"} <= tables

    def test_init_db_is_idempotent(self):
        """Calling init_db() twice on the same database must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "audit.sqlite"
            init_db(db_path)
            # Second call must be safe (CREATE IF NOT EXISTS + _ensure_column).
            init_db(db_path)

    def test_init_db_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nested" / "dir" / "audit.sqlite"
            assert not db_path.parent.exists()
            init_db(db_path)
            assert db_path.exists()

    def test_init_db_returns_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "audit.sqlite"
            result = init_db(db_path)
            assert result == db_path


class EnsureColumnTests:
    def _create_table(self, conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
        col_defs = ", ".join(f"{col} TEXT" for col in columns)
        conn.execute(f"CREATE TABLE {table} ({col_defs})")
        conn.commit()

    def test_adds_missing_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                self._create_table(conn, "test_table", ["id", "name"])

                _ensure_column(conn, "test_table", "new_col", "TEXT")
                conn.commit()

                columns = {row[1] for row in conn.execute("PRAGMA table_info(test_table)").fetchall()}
                assert "new_col" in columns
            finally:
                conn.close()

    def test_is_idempotent_for_existing_column(self):
        """Calling _ensure_column for an already-present column must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                self._create_table(conn, "test_table", ["id", "existing_col"])
                # Should not raise even if the column already exists.
                _ensure_column(conn, "test_table", "existing_col", "TEXT")
                conn.commit()
            finally:
                conn.close()

    def test_migration_on_legacy_db_without_column(self):
        """Simulate a legacy database missing the external_key column on tickets."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite"
            # Create the tickets table *without* external_key (legacy schema).
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE tickets "
                "(id TEXT PRIMARY KEY, case_id TEXT, severity INTEGER, "
                "category TEXT, status TEXT, created_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE audits "
                "(id INTEGER PRIMARY KEY, case_id TEXT, decision TEXT, clause TEXT, "
                "reviewer TEXT, rationale TEXT, timestamp TEXT)"
            )
            conn.commit()
            conn.close()

            # init_db should add the missing column transparently.
            init_db(db_path)

            with db_connection(db_path) as conn:
                ticket_columns = {row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()}
                audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(audits)").fetchall()}
            assert "external_key" in ticket_columns
            assert "external_url" in ticket_columns
            assert "run_id" in ticket_columns
            assert "run_id" in audit_columns
