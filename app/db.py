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
    subs       TEXT DEFAULT '',            -- JSON 数组：[{lang,label,path}] 多语言字幕轨道
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
CREATE TABLE IF NOT EXISTS downloads (
    pickcode   TEXT PRIMARY KEY,       -- 115 文件唯一标识
    name       TEXT,
    path       TEXT,                   -- 落盘绝对路径（容器内）
    size       INTEGER DEFAULT 0,
    done_at    INTEGER
);
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    platform     TEXT DEFAULT 'android_tv',
    capabilities TEXT DEFAULT '{}',
    last_seen    INTEGER
);
CREATE TABLE IF NOT EXISTS watch_progress (
    profile_key TEXT NOT NULL DEFAULT 'default',
    media_id    INTEGER NOT NULL,
    device_id   TEXT DEFAULT '',
    position_ms INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    completed   INTEGER DEFAULT 0,
    updated_at  INTEGER,
    PRIMARY KEY(profile_key, media_id)
);
CREATE INDEX IF NOT EXISTS idx_progress_updated
ON watch_progress(profile_key, completed, updated_at DESC);
"""


def init():
    with _lock:
        _conn.executescript(SCHEMA)
        # 老库迁移：sessions 表补 role 列
        cols = [r["name"] for r in _conn.execute("PRAGMA table_info(sessions)")]
        if cols and "role" not in cols:
            _conn.execute("ALTER TABLE sessions ADD COLUMN role TEXT DEFAULT 'admin'")
        # 老库迁移：media 表补 subs 列（多语言字幕轨道，JSON 数组）
        cols = [r["name"] for r in _conn.execute("PRAGMA table_info(media)")]
        if cols and "subs" not in cols:
            _conn.execute("ALTER TABLE media ADD COLUMN subs TEXT DEFAULT ''")
            # 已有的单字幕记录迁移为轨道数组
            for row in _conn.execute(
                    "SELECT id, sub_path FROM media WHERE sub_path IS NOT NULL AND sub_path != ''").fetchall():
                import json as _json
                _conn.execute("UPDATE media SET subs=? WHERE id=?", (
                    _json.dumps([{"lang": "zh", "label": "中文字幕",
                                  "path": row["sub_path"]}]), row["id"]))
        # 下载任务补来源字段：旧任务默认归属 115，为多网盘 Provider 留出边界
        for table in ("tasks", "downloads"):
            cols = [r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")]
            if cols and "provider" not in cols:
                _conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN provider TEXT DEFAULT '115'")
            if cols and "account_id" not in cols:
                _conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN account_id TEXT DEFAULT 'default'")
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


# ---------- 敏感设置（AES-GCM 加密存储） ----------

from . import crypto  # noqa: E402


def get_secret(key, default=""):
    """读取加密的敏感设置；发现旧明文时自动迁移为密文。"""
    v = get_setting(key, None)
    if v is None:
        return default
    plain = crypto.decrypt(v)
    if plain and not crypto.is_encrypted(v):
        set_setting(key, crypto.encrypt(plain))
    return plain


def set_secret(key, value):
    value = (value or "").strip()
    set_setting(key, crypto.encrypt(value) if value else "")


def now():
    return int(time.time())
