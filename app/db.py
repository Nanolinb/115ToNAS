"""SQLite 存储。单连接 + 锁，低内存，适合 NAS。"""
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    created_at INTEGER,
    role       TEXT DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS media (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,            -- movie | episode
    path       TEXT UNIQUE NOT NULL,     -- 绝对路径（容器内）
    title      TEXT,
    year       INTEGER,
    season     INTEGER,
    episode    INTEGER,
    size       INTEGER,
    mtime      INTEGER,
    tmdb_id    INTEGER,
    name_cn    TEXT,
    poster     TEXT,                     -- 本地缓存文件名
    backdrop   TEXT,
    overview   TEXT,
    genres     TEXT,                     -- 逗号分隔
    rating     REAL,
    has_sub    INTEGER DEFAULT 0,
    sub_path   TEXT,
    sub_status TEXT DEFAULT '',          -- '' | searching | ok | failed
    status     TEXT DEFAULT 'ok',        -- ok | missing
    created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_type ON media(type);
CREATE INDEX IF NOT EXISTS idx_media_title ON media(title);
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    pickcode   TEXT,
    file_id    TEXT,
    target_dir TEXT,
    size       INTEGER DEFAULT 0,
    downloaded INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'queued',    -- queued|downloading|paused|done|failed|canceled
    error      TEXT DEFAULT '',
    speed      INTEGER DEFAULT 0,
    created_at INTEGER,
    updated_at INTEGER
);
"""


def init():
    with _lock:
        _conn.executescript(SCHEMA)
        # 老库迁移：sessions 表补 role 列
        cols = [r["name"] for r in _conn.execute("PRAGMA table_info(sessions)")]
        if cols and "role" not in cols:
            _conn.execute("ALTER TABLE sessions ADD COLUMN role TEXT DEFAULT 'admin'")
        _conn.commit()


def q(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def exe(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def get_setting(key, default=None):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    exe("INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def now():
    return int(time.time())
