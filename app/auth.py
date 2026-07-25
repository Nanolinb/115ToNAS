"""双通道鉴权：
- 管理端（admin）：115登录/下载/设置，必须密码（首次使用先设置）
- 观影端（viewer）：海报墙/播放，默认免密；可单独设置观影密码

安全设计：
- 密码 PBKDF2-HMAC-SHA256（26 万次迭代）加盐存储；旧 SHA-256 格式验证通过后自动升级
- 全链路 hmac.compare_digest 常量时间比较，防时序侧信道
- 登录接口按来源 IP 限流：10 分钟内失败 5 次锁定 10 分钟（内存计数，重启清零）
- 会话闲置自动过期：管理端 12 小时无操作退出，观影端 30 天（滑动续期）
- 外部播放器签名链接：HMAC 签名 + 24 小时有效期，IINA/VLC 等无 Cookie 场景专用
"""
import hashlib
import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request

from . import db

SESSION_TTL = 30 * 86400  # 30 天
COOKIE_NAME = "mh_session"

_PBKDF2_ITER = 260_000


def _hash(pw: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt),
                                 _PBKDF2_ITER).hex()
    return f"pbkdf2${_PBKDF2_ITER}${salt}${digest}"


def _verify_pbkdf2(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$", 3)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt),
                                   int(iters)).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, TypeError):
        return False


def _verify_legacy(pw: str, stored: str) -> bool:
    """旧格式 salt:sha256(salt+pw)，常量时间比较。"""
    try:
        salt, digest = stored.split(":", 1)
    except ValueError:
        return False
    calc = hashlib.sha256((salt + pw).encode()).hexdigest()
    return hmac.compare_digest(calc, digest)


# ---------- 管理端密码 ----------

def is_configured() -> bool:
    return db.get_setting("password_hash") is not None


def set_password(pw: str):
    db.set_setting("password_hash", _hash(pw))


def verify(pw: str) -> bool:
    stored = db.get_setting("password_hash")
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        return _verify_pbkdf2(pw, stored)
    if _verify_legacy(pw, stored):
        set_password(pw)  # 旧格式验证通过 → 就地升级为 PBKDF2
        return True
    return False


# ---------- 观影端密码（可选） ----------

def viewer_configured() -> bool:
    return db.get_setting("viewer_password_hash") is not None


def set_viewer_password(pw: str):
    if pw:
        db.set_setting("viewer_password_hash", _hash(pw))
    else:
        db.exe("DELETE FROM settings WHERE key='viewer_password_hash'")


def verify_viewer(pw: str) -> bool:
    stored = db.get_setting("viewer_password_hash")
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        return _verify_pbkdf2(pw, stored)
    if _verify_legacy(pw, stored):
        set_viewer_password(pw)
        return True
    return False


# ---------- 登录限流（防在线爆破） ----------

_RATE_WINDOW = 600      # 统计窗口 10 分钟
_RATE_MAX_FAILS = 5     # 窗口内允许失败次数
_RATE_LOCK = 600        # 锁定时长 10 分钟
_fails: dict[str, list[float]] = {}


def rate_locked(key: str) -> int:
    """返回剩余锁定秒数；0 = 未锁定。"""
    now = time.time()
    fails = [t for t in _fails.get(key, []) if now - t < _RATE_WINDOW]
    _fails[key] = fails
    if len(fails) >= _RATE_MAX_FAILS:
        return int(_RATE_LOCK - (now - fails[-1])) or 1
    return 0


def rate_record_fail(key: str):
    _fails.setdefault(key, []).append(time.time())


def rate_clear(key: str):
    _fails.pop(key, None)


# ---------- 会话 ----------
# 闲置自动过期：管理端 12 小时无操作退出（防局域网冒用），观影端 30 天
IDLE_TTL = {"admin": 12 * 3600, "viewer": 30 * 86400}


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
    if not row:
        return None
    ttl = IDLE_TTL.get(row["role"] or "admin", SESSION_TTL)
    if row["created_at"] < db.now() - ttl:
        destroy_session(token)  # 闲置超时，会话作废
        return None
    # 滑动续期：距上次活动超 5 分钟才写库，减少磁盘写入
    if db.now() - row["created_at"] > 300:
        db.exe("UPDATE sessions SET created_at=? WHERE token=?", (db.now(), token))
    return row


# ---------- 外部播放器签名链接（无 Cookie 场景：IINA/VLC/电视播放器） ----------

PLAY_LINK_TTL = 24 * 3600  # 签名链接 24 小时有效


def _link_secret() -> str:
    s = db.get_setting("link_secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("link_secret", s)
    return s


def make_play_token(media_id: int) -> str:
    exp = int(time.time()) + PLAY_LINK_TTL
    sig = hmac.new(_link_secret().encode(), f"play:{media_id}:{exp}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def check_play_token(media_id: int, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    calc = hmac.new(_link_secret().encode(), f"play:{media_id}:{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(calc, sig)


def _play_token_ok(request: Request) -> bool:
    """/api/stream/{id} 与 /api/subtitle/{id}/* 接受 ?pt= 签名令牌。"""
    m = re.search(r"^/api/(?:stream|subtitle)/(\d+)", request.url.path)
    pt = request.query_params.get("pt", "")
    return bool(m and pt and check_play_token(int(m.group(1)), pt))


def require_admin(request: Request):
    if not is_configured():
        return  # 首次使用未设密码时放行，前端引导设置
    sess = _session(request)
    if not sess or sess["role"] != "admin":
        raise HTTPException(status_code=401, detail="unauthorized")


def require_viewer(request: Request):
    if _play_token_ok(request):
        return  # 签名播放链接（外部播放器专用）
    if not viewer_configured():
        return  # 未设观影密码 → 免密
    if not _session(request):  # viewer 或 admin 会话都可以
        raise HTTPException(status_code=401, detail="viewer_unauthorized")


def admin_authed(request: Request) -> bool:
    sess = _session(request)
    return bool(sess and sess["role"] == "admin")
