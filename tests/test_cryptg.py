"""The required native backend is the path Telethon actually dispatches to."""
from __future__ import annotations

import cryptg
from telethon.crypto import aes


def test_telethon_dispatches_aes_ige_to_cryptg(monkeypatch):
    encrypted = object()
    decrypted = object()
    calls = []

    def encrypt_ige(data, key, iv):
        calls.append(("encrypt", data, key, iv))
        return encrypted

    def decrypt_ige(data, key, iv):
        calls.append(("decrypt", data, key, iv))
        return decrypted

    monkeypatch.setattr(cryptg, "encrypt_ige", encrypt_ige)
    monkeypatch.setattr(cryptg, "decrypt_ige", decrypt_ige)

    key = b"k" * 32
    iv = b"i" * 32
    plain = b"p" * 16
    cipher = b"c" * 16
    assert aes.cryptg is cryptg
    assert aes.AES.encrypt_ige(plain, key, iv) is encrypted
    assert aes.AES.decrypt_ige(cipher, key, iv) is decrypted
    assert calls == [
        ("encrypt", plain, key, iv),
        ("decrypt", cipher, key, iv),
    ]
