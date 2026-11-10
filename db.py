"""SQLite storage for chat conversations and messages."""
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("CHAT_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "instance", "chat.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    messages_used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations (updated_at DESC);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id():
    return str(uuid.uuid4())


@contextmanager
def connect():
    """One short-lived connection per operation (safe with the threaded dev server)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Migrations for databases created before accounts / message limits existed.
        if "messages_used" not in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN messages_used INTEGER NOT NULL DEFAULT 0")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "user_id" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, updated_at DESC)")


# ---------- users ----------
def create_user(email, name, password_hash):
    """Insert a user; returns the new id, or None if the email is already registered.

    The first account ever created also adopts conversations that predate accounts.
    """
    try:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (email, name, password_hash, _now()),
            )
            user_id = cur.lastrowid
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
                conn.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (user_id,))
            return user_id
    except sqlite3.IntegrityError:
        return None


def get_user(user_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email, name, messages_used FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def get_messages_used(user_id):
    with connect() as conn:
        row = conn.execute("SELECT messages_used FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["messages_used"] if row else 0


def consume_message(user_id, limit):
    """Atomically use one of the account's free messages; False if none are left."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET messages_used = messages_used + 1 WHERE id = ? AND messages_used < ?",
            (user_id, limit),
        )
        return cur.rowcount > 0


def refund_message(user_id):
    """Give back a message that was reserved but produced no reply."""
    with connect() as conn:
        conn.execute(
            "UPDATE users SET messages_used = messages_used - 1 WHERE id = ? AND messages_used > 0",
            (user_id,),
        )


def get_user_by_email(email):
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email, name, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    return dict(row) if row else None


# ---------- conversations ----------
def list_conversations(user_id, limit=500):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id, user_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_messages(conversation_id, limit=None):
    """Messages oldest-first; with `limit`, only the most recent `limit` of them."""
    with connect() as conn:
        if limit:
            rows = conn.execute(
                "SELECT role, content, created_at FROM ("
                "  SELECT id, role, content, created_at FROM messages "
                "  WHERE conversation_id = ? ORDER BY id DESC LIMIT ?"
                ") ORDER BY id",
                (conversation_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def save_exchange(conversation_id, user_id, title, user_text, reply):
    """Store a user/assistant pair, creating the conversation if it is new."""
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, user_id, title, now, now),
        )
        conn.executemany(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [
                (conversation_id, "user", user_text, now),
                (conversation_id, "assistant", reply, now),
            ],
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))


def set_title(conversation_id, user_id, title):
    with connect() as conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
            (title, conversation_id, user_id),
        )
        return cur.rowcount > 0


def delete_conversation(conversation_id, user_id):
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        )
        return cur.rowcount > 0
