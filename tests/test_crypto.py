"""Тесты шифрования ключей НП (Fernet) — чистая логика, без БД."""

from __future__ import annotations

from app.utils import crypto


def test_encrypt_decrypt_roundtrip():
    plaintext = "np-api-key-1234567890"
    token = crypto.encrypt(plaintext)
    assert token != plaintext  # в БД хранится шифртекст
    assert crypto.decrypt(token) == plaintext


def test_encrypt_is_non_deterministic():
    # Fernet включает временную метку/IV → два шифрования дают разные токены,
    # но оба расшифровываются в исходную строку.
    a = crypto.encrypt("same")
    b = crypto.encrypt("same")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same"


def test_generate_key_is_valid_fernet_key():
    from cryptography.fernet import Fernet

    key = crypto.generate_key()
    # Не бросает → ключ валиден.
    Fernet(key.encode())
