"""
Base SQLite : stocke les réponses WhatsApp reçues via le webhook UltraMsg
et l'état des runs (dernier passage du pipeline, dernier check Gmail/réponses).
"""
import sqlite3
import json
import time
from contextlib import contextmanager

from config import DB_PATH


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Crée les tables si elles n'existent pas."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS incoming_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                phone       TEXT NOT NULL,
                body        TEXT,
                received_at REAL NOT NULL,
                processed   INTEGER NOT NULL DEFAULT 0,
                raw         TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_phone ON incoming_messages(phone)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_processed ON incoming_messages(processed)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS run_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)


# ---------------------------------------------------------------------------
# Messages WhatsApp entrants
# ---------------------------------------------------------------------------
def save_incoming_message(phone, body, raw=None, received_at=None):
    """Enregistre un message reçu via le webhook. Renvoie l'id."""
    phone = normalize_phone(phone)
    received_at = received_at or time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO incoming_messages (phone, body, received_at, processed, raw) "
            "VALUES (?, ?, ?, 0, ?)",
            (phone, body, received_at, json.dumps(raw) if raw else None),
        )
        return cur.lastrowid


def get_unprocessed_messages():
    """Renvoie tous les messages non encore traités par le pipeline."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM incoming_messages WHERE processed = 0 ORDER BY received_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_messages_for_phone(phone, since=0.0):
    """Renvoie les messages d'un numéro depuis un timestamp donné."""
    phone = normalize_phone(phone)
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM incoming_messages WHERE phone = ? AND received_at >= ? "
            "ORDER BY received_at ASC",
            (phone, since),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_processed(message_ids):
    """Marque une liste d'ids comme traités."""
    if not message_ids:
        return
    with _conn() as c:
        c.executemany(
            "UPDATE incoming_messages SET processed = 1 WHERE id = ?",
            [(mid,) for mid in message_ids],
        )


def phones_with_replies_since(since=0.0):
    """Ensemble des numéros ayant répondu depuis un timestamp."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT phone FROM incoming_messages WHERE received_at >= ?",
            (since,),
        ).fetchall()
        return {r["phone"] for r in rows}


# ---------------------------------------------------------------------------
# État des runs (clé/valeur)
# ---------------------------------------------------------------------------
def get_state(key, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM run_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO run_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_last_run_ts():
    """Timestamp du dernier run terminé (0 si jamais)."""
    return float(get_state("last_run_ts", 0.0))


def set_last_run_ts(ts=None):
    set_state("last_run_ts", ts or time.time())


def get_last_reply_check_ts():
    """Timestamp à partir duquel chercher les nouvelles réponses."""
    return float(get_state("last_reply_check_ts", 0.0))


def set_last_reply_check_ts(ts=None):
    set_state("last_reply_check_ts", ts or time.time())


# ---------------------------------------------------------------------------
# Utilitaire de normalisation des numéros
# ---------------------------------------------------------------------------
def normalize_phone(phone):
    """Garde uniquement les chiffres (supprime +, espaces, @c.us, etc.)."""
    if not phone:
        return ""
    phone = str(phone).split("@")[0]  # UltraMsg renvoie 34XXXXXXXXX@c.us
    return "".join(ch for ch in phone if ch.isdigit())
