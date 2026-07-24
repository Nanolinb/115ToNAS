"""双通道鉴权：
- 管理端（admin）：115登录/下载/设置，必须密码（首次使用先设置）
- 观影端（viewer）：海报墙/播放，默认免密；可单独设置观影密码
"""
import hashlib
import secrets

from fastapi import HTTPException, Request

from . import db

SESSION_TTL = 30 * 86400  # 30 天
COOKIE_NAME = "mh_session"


def _hash(pw: str) -> str:
    salt = secrets.token_hex(16)
    return f"{salt}:{hashlib.sha256((salt + pw).encode()).hexdigest()}"


def _check(pw: str, stored: str | None) -> bool:
    if not stored:
        return False
    salt, digest = stored.split(":", 1)
    return hashlib.sha256((salt + pw).encode()).hexdigest() == digest


# ---------- 管理端密码 ----------

def is_configured() -> bool:
    return db.get_setting("password_hash") is not None


def set_password(pw: str):
    db.set_setting("password_hash", _hash(pw))


def verify(pw: str) -> bool:
    return _check(pw, db.get_setting("password_hash"))


# ---------- 观影端密码（可选） ----------

def viewer_configured() -> bool:
    return db.get_setting("viewer_password_hash") is not None


def set_viewer_password(pw: str):
    if pw:
        db.set_setting("viewer_password_hash", _hash(pw))
    else:
        db.exe("DELETE FROM settings WHERE key='viewer_password_hash'")


def verify_viewer(pw: str) -> bool:
    return _check(pw, db.get_setting("viewer_password_hash"))


# ---------- 会话 ----------

def create_session(role: str = "admin") -> str:
    token = secrets.token_urlsafe(32)
    db.exe("INSERT INTO sessions(token, created_at, role) VALUES(?,?,?)",
           (token, db.now(), role))
    db.exe("DELETE FROM sessions WHERE created_at < ?", (db.now() - SESSION_TTL,))
    return token


def destroy_session(token: str):
    db.exe("DELETE FROM sessions WHERE token=?", (token,))


def _session(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = db.one("SELECT created_at, role FROM sessions WHERE token=?", (token,))
    if not row or row["created_at"] < db.now() - SESSION_TTL:
        return None
    return row


def require_admin(request: Request):
    if not is_configured():
        return  # 首次使用未设密码时放行，前端引导设置
    sess = _session(request)
    if not sess or sess["role"] != "admin":
        raise HTTPException(status_code=401, detail="unauthorized")


def require_viewer(request: Request):
    if not viewer_configured():
        return  # 未设观影密码 → 免密
    if not _session(request):  # viewer 或 admin 会话都可以
        raise HTTPException(status_code=401, detail="viewer_unauthorized")


def admin_authed(request: Request) -> bool:
    sess = _session(request)
    return bool(sess and sess["role"] == "admin")
