import os
import sqlite3
from pathlib import Path


def connect() -> sqlite3.Connection:
    path = Path(os.environ.get("AIMO_DB", "var/aimo.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    return con
