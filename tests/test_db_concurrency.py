"""What has to hold once more than one person is using this.

The app is a single process with genuinely concurrent database users: uvicorn
runs sync route handlers in a thread pool, and APScheduler runs the reminder and
discovery loops on their own threads. With one tester that is invisible. The
failure it produces with a dozen is a phone request timing out behind the
discovery pass, which is exactly the kind of thing nobody reproduces on demand.

So these are about contention, not correctness of any one query.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from app.config import get_settings
from app.db import (BUSY_TIMEOUT_MS, connect, init_db,
                    journal_mode)


def test_the_database_is_in_wal_mode():
    """Under the default rollback journal a writer locks the whole file against
    readers, so the discovery loop would block every phone in the beta."""
    assert journal_mode() == "wal"


def test_wal_survives_reopening(tmp_path, monkeypatch):
    """journal_mode is a property of the file, not the connection — which is the
    only reason setting it once in init_db is enough."""
    from app import config

    path = tmp_path / "reopen.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    config.get_settings.cache_clear()
    init_db()
    assert journal_mode() == "wal"
    # A fresh process would do exactly this: connect without calling init_db.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_every_connection_gets_a_busy_timeout():
    """Without this a lock contention is an instant `database is locked` 500
    rather than a short wait."""
    with connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_a_reader_is_not_blocked_by_an_exclusive_writer():
    """The property WAL actually buys.

    Worth being precise about, because the weaker version of this test passes
    either way: an ordinary INSERT only takes a RESERVED lock until it commits,
    so a reader gets through under a rollback journal too. The lock that bites is
    EXCLUSIVE — taken at commit, and for the whole of a bulk write like a
    discovery pass saving a tick's worth of postings. Under a rollback journal
    that lock stops *readers*; under WAL they read the last committed snapshot
    and never wait. This test fails, by timing out, without WAL.
    """
    with connect() as conn:
        conn.execute("INSERT INTO applications (user_id, company, status, "
                     "last_updated_at) VALUES ('u1', 'Acme', 'Applied', '2026-01-01')")

    started = threading.Event()
    release = threading.Event()

    def writer():
        conn = sqlite3.connect(get_settings().database_path)
        conn.isolation_level = None  # we drive the transaction ourselves
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("INSERT INTO applications (user_id, company, status, "
                         "last_updated_at) VALUES ('u1', 'Held', 'Applied', "
                         "'2026-01-01')")
            started.set()
            release.wait(5)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    assert started.wait(5)

    began = time.monotonic()
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT company FROM applications WHERE user_id = 'u1'").fetchall()
    finally:
        elapsed = time.monotonic() - began
        release.set()
        t.join(10)

    assert [r["company"] for r in rows] == ["Acme"]  # the uncommitted row is not visible
    assert elapsed < 1.0, f"the read waited {elapsed:.2f}s on an exclusive writer"


def test_concurrent_writers_all_land():
    """Eight threads writing at once — roughly a discovery tick racing a handful
    of phones. Every row must arrive, and none may raise."""
    errors: list[Exception] = []

    def write(n: int):
        try:
            for i in range(10):
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO applications (user_id, company, status, "
                        "last_updated_at) VALUES (?, ?, 'Applied', '2026-01-01')",
                        (f"u{n}", f"Co{n}-{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == [], f"writes raised: {errors[:3]}"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 80


def test_health_reports_the_journal_mode(monkeypatch):
    """A restored snapshot can come back in the wrong mode, and there is no way
    to see that from outside the machine otherwise."""
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/health").json()
    assert body["db_journal"] == "wal"
