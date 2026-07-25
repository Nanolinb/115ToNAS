"""敏感信息加密存储（AES-256-GCM）。

- 密钥随机生成一次，保存在 NAS 本地 /config/secret.key，权限 600；
- 密文格式：enc1:base64(nonce|tag|ciphertext)；
- 兼容读取旧的明文值，便于平滑迁移；
- 依赖 pycryptodome（p115client 已带入，无新增依赖）。
"""
import base64
import os

from .config import CONFIG_DIR

KEY_PATH = CONFIG_DIR / "secret.key"
_PREFIX = "enc1:"
_NONCE_LEN = 12
_TAG_LEN = 16

_key: bytes | None = None


def _load_key() -> bytes:
    global _key
    if _key:
        return _key
    if KEY_PATH.exists():
        _key = base64.b64decode(KEY_PATH.read_bytes())
    else:
        _key = os.urandom(32)
        KEY_PATH.write_bytes(base64.b64encode(_key))
        os.chmod(KEY_PATH, 0o600)
    return _key


def encrypt(plain: str) -> str:
    """明文 -> 密文字符串。空字符串原样返回。"""
    if not plain:
        return ""
    from Crypto.Cipher import AES
    nonce = os.urandom(_NONCE_LEN)
    ct, tag = AES.new(_load_key(), AES.MODE_GCM, nonce=nonce) \
        .encrypt_and_digest(plain.encode("utf-8"))
    return _PREFIX + base64.b64encode(nonce + tag + ct).decode("ascii")


def decrypt(value: str) -> str:
    """密文 -> 明文。非密文格式（旧数据）原样返回，实现平滑迁移。"""
    if not value:
        return ""
    if not is_encrypted(value):
        return value
    from Crypto.Cipher import AES
    raw = base64.b64decode(value[len(_PREFIX):])
    nonce, tag, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:_NONCE_LEN + _TAG_LEN], raw[_NONCE_LEN + _TAG_LEN:]
    return AES.new(_load_key(), AES.MODE_GCM, nonce=nonce) \
        .decrypt_and_verify(ct, tag).decode("utf-8")


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)
