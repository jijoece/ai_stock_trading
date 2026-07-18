"""Shared SQLite transaction-ownership primitives (Milestone 11.3.1 Item 2).

Kept in its own module (rather than `database.py`) so `schema_version.py` —
which `database.py::connect()` itself calls during connection setup — can use
these primitives too without a circular import.

Every connection returned by `database.py::connect()` is opened with
`isolation_level=None` (true SQLite autocommit): no statement outside an
explicit `BEGIN`/`BEGIN IMMEDIATE` block ever silently opens an implicit
transaction. Under that model `conn.in_transaction` being `True` can only
mean a real, still-open, caller-owned transaction — never a stray implicit
one left behind by an unguarded write. `begin_immediate` therefore never
assumes a pre-existing transaction is abandoned and never rolls it back on
the caller's behalf; it fails closed with `TransactionAlreadyActiveError`
instead. This repository's existing `commit=False` parameter convention
(used throughout `paper_books/*.py` and `storage/*_repositories.py`) is the
supported way for an inner call to participate in an already-open outer
transaction — it must not call `begin_immediate`/`transaction` again.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager


class TransactionAlreadyActiveError(RuntimeError):
    """Raised when `begin_immediate`/`transaction` is invoked on a connection
    that already has an open transaction it does not own. Discarding that
    transaction via an unconditional `conn.rollback()` (the pre-11.3.1
    behavior) could silently erase real caller-owned reservation, fill,
    checkpoint, or lease work the caller still intends to commit. The caller
    holding the open transaction must finish it (commit or roll back)
    itself; a nested operation that wants to participate in it must issue
    its statements directly on the same connection instead of starting a new
    transaction."""


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Start a `BEGIN IMMEDIATE` transaction that the caller owns end-to-end.

    Requires `conn` to have been opened with `isolation_level=None` (every
    connection from `database.py::connect()` is). Raises
    `TransactionAlreadyActiveError` if a transaction is already open —
    it never silently rolls one back."""
    if conn.in_transaction:
        raise TransactionAlreadyActiveError(
            "begin_immediate() called while a transaction is already open on this connection — "
            "refusing to silently discard it. The caller already holding the open transaction "
            "must commit or roll it back before a new one starts; code that wants to participate "
            "in the existing transaction must not call begin_immediate()/transaction() again — "
            "it should run its statements directly on this connection (the repository-wide "
            "`commit=False` parameter convention)."
        )
    conn.execute("BEGIN IMMEDIATE")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit transaction-ownership context manager.

    Begins a `BEGIN IMMEDIATE` transaction this context manager owns end to
    end: commits on clean exit, rolls back on any `BaseException` (not just
    `Exception`, so a `KeyboardInterrupt`/`SystemExit` mid-write cannot leave
    a dangling open transaction), and never assumes a pre-existing
    transaction belongs to it — `begin_immediate` fails closed via
    `TransactionAlreadyActiveError` if one is already open."""
    begin_immediate(conn)
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
