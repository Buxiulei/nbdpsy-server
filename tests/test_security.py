"""core/security 测试：Fernet cookie 加解密 + apikey 生成/hash/校验。"""
from app.core import security


def test_cookie_roundtrip():
    """加密后解密应还原明文（roundtrip）。"""
    ct = security.encrypt_cookies('[{"name":"a","value":"1"}]')
    assert security.decrypt_cookies(ct) == '[{"name":"a","value":"1"}]'


def test_decrypt_bad_returns_empty():
    """非法密文解密失败时返回空串，不抛异常。"""
    assert security.decrypt_cookies("not-a-token") == ""


def test_apikey_hash_verify():
    """apikey 生成 → hash → 校验：正确 key 通过，错误 key 拒绝。"""
    k = security.generate_apikey()
    h = security.hash_apikey(k)
    assert security.verify_apikey(k, h)
    assert not security.verify_apikey("wrong", h)
