import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent / "alma.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome TEXT NOT NULL,
            date TEXT,
            amount REAL,
            option_id TEXT,
            notes TEXT
        )
    """)

    conn.commit()
    conn.close()


def save_outcome(
    outcome,
    date=None,
    amount=None,
    option_id=None,
    notes=None,
):
    conn = sqlite3.connect(DB_PATH)

    cursor = conn.execute(
        """
        INSERT INTO outcomes (
            outcome,
            date,
            amount,
            option_id,
            notes
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            outcome,
            date,
            amount,
            option_id,
            notes,
        ),
    )

    conn.commit()

    outcome_id = cursor.lastrowid

    conn.close()

    return outcome_id