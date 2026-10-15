import pytest

from dfsha.common.errors import CorruptBlock, Unauthenticated
from dfsha.security import auth, crypto


def test_block_roundtrip_and_blob_hash():
    dek = crypto.new_dek()
    blob = crypto.encrypt_block(dek, "blk1", 3, b"hola mundo" * 1000)
    assert crypto.verify_blob(blob)
    assert crypto.blob_generation(blob) == 3
    assert crypto.decrypt_block(dek, "blk1", blob) == b"hola mundo" * 1000


def test_block_is_not_plaintext_and_nonce_random():
    dek = crypto.new_dek()
    a = crypto.encrypt_block(dek, "b", 1, b"A" * 64)
    b = crypto.encrypt_block(dek, "b", 1, b"A" * 64)
    assert a != b  # nonce aleatorio
    assert b"A" * 16 not in a


def test_bitrot_detected_without_dek():
    blob = bytearray(crypto.encrypt_block(crypto.new_dek(), "b", 1, b"x" * 100))
    blob[30] ^= 0x01
    assert not crypto.verify_blob(bytes(blob))


def test_wrong_block_id_fails_aad():
    dek = crypto.new_dek()
    blob = crypto.encrypt_block(dek, "blockA", 1, b"secreto")
    with pytest.raises(CorruptBlock):
        crypto.decrypt_block(dek, "blockB", blob)


def test_wrong_dek_fails():
    blob = crypto.encrypt_block(crypto.new_dek(), "b", 1, b"secreto")
    with pytest.raises(CorruptBlock):
        crypto.decrypt_block(crypto.new_dek(), "b", blob)


def test_key_wrap_roundtrip_and_aad():
    kek = crypto.new_dek()
    dek = crypto.new_dek()
    w = crypto.wrap(kek, dek, b"inode:7")
    assert crypto.unwrap(kek, w, b"inode:7") == dek
    with pytest.raises(Unauthenticated):
        crypto.unwrap(kek, w, b"inode:8")


def test_sealed_token_roundtrip_expiry_and_tamper():
    key = crypto.new_dek()
    t = crypto.seal(key, {"bid": "x", "dek": "abc"}, ttl=60)
    assert crypto.unseal(key, t)["bid"] == "x"
    assert "abc" not in t  # opaco
    with pytest.raises(Unauthenticated):
        crypto.unseal(crypto.new_dek(), t)
    with pytest.raises(Unauthenticated):
        crypto.unseal(key, t[:-3] + "AAA")
    expired = crypto.seal(key, {"a": 1}, ttl=-1)
    with pytest.raises(Unauthenticated, match="expirado"):
        crypto.unseal(key, expired)


def test_hkdf_domains_are_independent():
    k = crypto.ClusterKeys.from_master(b"\x01" * 32)
    assert len({k.kek, k.jwt, k.block_token, k.secrets}) == 4


def test_password_hash():
    h = auth.hash_password("correcta")
    assert auth.verify_password("correcta", h)
    assert not auth.verify_password("incorrecta", h)
    assert h != auth.hash_password("correcta")  # sal aleatoria
    assert not auth.verify_password("x", "basura")


def test_api_key():
    kid, full, stored = auth.new_api_key()
    pk, secret = auth.parse_api_key(full)
    assert pk == kid and auth.api_key_matches(secret, stored)
    assert not auth.api_key_matches("otro", stored)
    with pytest.raises(Unauthenticated):
        auth.parse_api_key("nope")


def test_totp_rfc6238_vectors():
    # RFC 6238 Apéndice B (SHA-1, secreto ASCII "12345678901234567890"); 6 dígitos = últimos 6 de los 8.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth.totp_now(secret, at=59) == "287082"
    assert auth.totp_now(secret, at=1111111109) == "081804"
    assert auth.totp_now(secret, at=1234567890) == "005924"


def test_totp_window_and_replay_step():
    s = auth.new_totp_secret()
    t = 1_700_000_000
    code = auth.totp_now(s, at=t)
    step = auth.totp_verify(s, code, at=t)
    assert step == t // 30
    assert auth.totp_verify(s, code, at=t + 25) is not None  # ventana ±1
    assert auth.totp_verify(s, code, at=t + 120) is None
    assert auth.totp_verify(s, "12ab56", at=t) is None


def test_jwt():
    key = b"k" * 32
    tok, exp = auth.issue_jwt(key, user="alice", ver=2, ttl=60)
    claims = auth.decode_jwt(key, tok)
    assert claims["sub"] == "alice" and claims["ver"] == 2
    with pytest.raises(Unauthenticated):
        auth.decode_jwt(b"z" * 32, tok)
    tok2, _ = auth.issue_jwt(key, user="alice", ver=2, ttl=-10)
    with pytest.raises(Unauthenticated, match="expirada"):
        auth.decode_jwt(key, tok2)
