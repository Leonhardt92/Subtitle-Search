from __future__ import annotations

import hashlib
import re
import sqlite3


def load_sqlite_vec_extension(connection: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # type: ignore
    except ModuleNotFoundError:
        return False

    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    return True


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def vec_table_name(model_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", model_name.lower()).strip("_")
    digest = hashlib.sha1(model_name.encode("utf-8")).hexdigest()[:8]
    return f"subtitle_vec_{slug[:40]}_{digest}"


def vec_table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def list_vec_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name LIKE 'subtitle_vec_%'
          AND sql LIKE 'CREATE VIRTUAL TABLE%USING vec0%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def delete_vec_rows(connection: sqlite3.Connection, subtitle_ids: list[int]) -> None:
    if not subtitle_ids:
        return

    try:
        loaded = load_sqlite_vec_extension(connection)
    except Exception:
        loaded = False
    if not loaded:
        return

    for table_name in list_vec_tables(connection):
        quoted_table = quote_identifier(table_name)
        connection.executemany(
            f"DELETE FROM {quoted_table} WHERE rowid = ?",
            [(subtitle_id,) for subtitle_id in subtitle_ids],
        )


def drop_vec_tables(connection: sqlite3.Connection) -> None:
    for table_name in list_vec_tables(connection):
        connection.execute(f"DROP TABLE {quote_identifier(table_name)}")


def count_vec_rows(connection: sqlite3.Connection) -> int:
    try:
        loaded = load_sqlite_vec_extension(connection)
    except Exception:
        loaded = False
    if not loaded:
        return 0

    total = 0
    for table_name in list_vec_tables(connection):
        total += int(connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}").fetchone()[0])
    return total


def ensure_vec_table(
    connection: sqlite3.Connection,
    *,
    model_name: str,
    dimensions: int,
    force_rebuild: bool = False,
) -> str:
    table_name = vec_table_name(model_name)
    quoted_name = quote_identifier(table_name)
    if force_rebuild and vec_table_exists(connection, table_name):
        connection.execute(f"DROP TABLE {quoted_name}")

    if not vec_table_exists(connection, table_name):
        connection.execute(f"CREATE VIRTUAL TABLE {quoted_name} USING vec0(embedding float[{dimensions}])")

    return table_name
