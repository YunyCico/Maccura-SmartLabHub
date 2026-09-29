from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
import sqlite3


DATABASE_PATH = Path(__file__).resolve().parents[2] / "department_platform.db"


@contextmanager
def connection() -> Generator[sqlite3.Connection, None, None]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DATABASE_PATH)
    database.row_factory = sqlite3.Row
    try:
        ensure_schema(database)
        yield database
        database.commit()
    finally:
        database.close()


def ensure_schema(database: sqlite3.Connection) -> None:
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS import_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            total_rows INTEGER NOT NULL,
            sheet_count INTEGER NOT NULL,
            imported_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def initialize_database() -> None:
    with connection():
        pass


def create_import_batch(record: dict[str, object]) -> dict[str, object]:
    with connection() as database:
        cursor = database.execute(
            """
            INSERT INTO import_batches
                (original_filename, stored_filename, stored_path, total_rows, sheet_count, imported_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """ ,
            (
                record["original_filename"],
                record["stored_filename"],
                record["stored_path"],
                record["total_rows"],
                record["sheet_count"],
                record["imported_by"],
                record["created_at"],
            ),
        )
        return {"id": cursor.lastrowid, **record}


def list_import_batches() -> list[dict[str, object]]:
    with connection() as database:
        rows = database.execute(
            "SELECT * FROM import_batches ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return [dict(row) for row in rows]
