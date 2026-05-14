"""
models.py  —  Quantly Platform Database Models
SQLite + simple ORM via sqlite3. No heavy dependencies needed.
"""
import sqlite3, os, hashlib, secrets, json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "quantly.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name   TEXT NOT NULL,
        email       TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now')),
        is_active   INTEGER DEFAULT 1
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        created_at  TEXT DEFAULT (datetime('now')),
        expires_at  TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS broker_connections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        broker_name TEXT NOT NULL,
        api_key     TEXT,
        api_secret  TEXT,
        access_token TEXT,
        is_active   INTEGER DEFAULT 1,
        connected_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")

    conn.commit()
    conn.close()


# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_pw(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return salt, h.hex()


def _verify_pw(password: str, salt: str, pw_hash: str) -> bool:
    _, h = _hash_pw(password, salt)
    return secrets.compare_digest(h, pw_hash)


# ── User CRUD ─────────────────────────────────────────────────────────────────

def create_user(full_name: str, email: str, password: str) -> dict:
    conn = get_conn()
    try:
        salt, pw_hash = _hash_pw(password)
        stored = f"{salt}:{pw_hash}"  # store salt+hash together
        conn.execute(
            "INSERT INTO users (full_name, email, password_hash) VALUES (?,?,?)",
            (full_name.strip(), email.strip().lower(), stored)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise ValueError("Email already registered")
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def verify_user(email: str, password: str) -> dict | None:
    user = get_user_by_email(email)
    if not user:
        return None
    stored = user["password_hash"]
    parts = stored.split(":", 1)
    if len(parts) != 2:
        return None
    salt, pw_hash = parts
    if _verify_pw(password, salt, pw_hash):
        return user
    return None


# ── Session CRUD ──────────────────────────────────────────────────────────────

def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions (token, user_id) VALUES (?,?)",
        (token, user_id)
    )
    conn.commit()
    conn.close()
    return token


def get_session_user(token: str) -> dict | None:
    if not token:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=?",
        (token,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token: str):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


# ── Broker CRUD ───────────────────────────────────────────────────────────────

def save_broker(user_id: int, broker_name: str, api_key: str, api_secret: str, access_token: str = "") -> int:
    conn = get_conn()
    # Deactivate old connections for same broker
    conn.execute(
        "UPDATE broker_connections SET is_active=0 WHERE user_id=? AND broker_name=?",
        (user_id, broker_name)
    )
    cursor = conn.execute(
        "INSERT INTO broker_connections (user_id, broker_name, api_key, api_secret, access_token) VALUES (?,?,?,?,?)",
        (user_id, broker_name, api_key, api_secret, access_token)
    )
    conn.commit()
    conn.close()
    return cursor.lastrowid


def get_broker(user_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM broker_connections WHERE user_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# Init on import
init_db()