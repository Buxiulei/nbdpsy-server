"""安全底座：Fernet cookie 加解密 + apikey 生成/hash/校验。

Fernet key 派生方式与旧仓（小红书运营工具/backend/app/core/security.py）保持一致，
目的是让本仓能解密旧仓存量 cookie：
取 SECRET_KEY 的 UTF-8 字节前 32 个，不足则右侧补 b"0" 到 32 字节，再 urlsafe base64。
"""
import base64
import hashlib
import secrets

from cryptography.fernet import Fernet
from loguru import logger

from app.core.config import settings


def _fernet() -> Fernet:
    """从 SECRET_KEY 派生 Fernet 实例（派生方式与旧仓一致）。"""
    raw = settings.SECRET_KEY.encode("utf-8")[:32].ljust(32, b"0")
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_cookies(plaintext: str) -> str:
    """加密 cookie 明文，返回 Fernet token 字符串。"""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_cookies(ciphertext: str) -> str:
    """解密 cookie 密文；失败（非法 token / 篡改）时返回空串并记 warning。"""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.warning(f"cookie 解密失败(返回空串): {e}")
        return ""


def generate_apikey() -> str:
    """生成 URL-safe 随机 apikey（32 字节熵）。"""
    return secrets.token_urlsafe(32)


def hash_apikey(key: str) -> str:
    """对 apikey 做 SHA256，返回十六进制摘要（仅存 hash，不存明文）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_apikey(key: str, hashed: str) -> bool:
    """常数时间比较 apikey 与其 hash，防时序侧信道。"""
    return secrets.compare_digest(hash_apikey(key), hashed)
