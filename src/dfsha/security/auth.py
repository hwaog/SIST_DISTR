"""Autenticación de usuarios: claves, API keys, TOTP (2FA) y JWT de sesión."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from typing import Any

import jwt

from ..common.errors import Unauthenticated
from ..common.util import b64d, b64e, new_id, new_secret

# ------------------------------------------------------------------ contraseñas
# scrypt (memory-hard, en la stdlib).  Parámetros ajustables por entorno para
# que la suite de tests no tarde; el valor por defecto es el recomendado por
# OWASP para interactivo (N=2^15, r=8, p=1  ≈ 100 ms).

_SCRYPT_N = int(os.environ.get("DFSHA_SCRYPT_N", 2**15))
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str, *, n: int | None = None) -> str:
    n = n or _SCRYPT_N
    salt = new_secret(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=256 * 1024 * 1024, dklen=32
    )
    return f"scrypt${n}${_SCRYPT_R}${_SCRYPT_P}${b64e(salt)}${b64e(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = encoded.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(
            password.encode(),
            salt=b64d(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=256 * 1024 * 1024,
            dklen=32,
        )
        return hmac.compare_digest(calc, b64d(dk))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------- API keys
# Formato: dfsha_ak_<id>.<secreto>.  Sólo se persiste SHA-256(secreto): como el
# secreto tiene 256 bit de entropía un hash rápido es suficiente.


def new_api_key() -> tuple[str, str, str]:
    """(key_id, clave_completa_para_el_usuario, hash_a_guardar)."""
    key_id = new_id()[:12]
    secret = b64e(new_secret(32))
    return key_id, f"dfsha_ak_{key_id}.{secret}", hashlib.sha256(secret.encode()).hexdigest()


def parse_api_key(full: str) -> tuple[str, str]:
    if not full.startswith("dfsha_ak_") or "." not in full:
        raise Unauthenticated("API key con formato inválido")
    key_id, secret = full[len("dfsha_ak_") :].split(".", 1)
    return key_id, secret


def api_key_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), stored_hash)


# ------------------------------------------------------------------------ TOTP
# RFC 6238 (HMAC-SHA1, 30 s, 6 dígitos): compatible con Google Authenticator,
# Authy, 1Password, etc.

TOTP_STEP = 30
TOTP_DIGITS = 6


def new_totp_secret() -> str:
    return base64.b32encode(new_secret(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off : off + 4])[0] & 0x7FFFFFFF) % (10**TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // TOTP_STEP))


def totp_verify(secret_b32: str, code: str, *, at: float | None = None, window: int = 1) -> int | None:
    """Devuelve el *step* que validó (para impedir *replay*) o ``None``."""
    code = (code or "").strip()
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    step = int((at if at is not None else time.time()) // TOTP_STEP)
    for s in range(step - window, step + window + 1):
        if hmac.compare_digest(_hotp(secret_b32, s), code):
            return s
    return None


def totp_uri(secret_b32: str, user: str, issuer: str = "DFSha") -> str:
    return f"otpauth://totp/{issuer}:{user}?secret={secret_b32}&issuer={issuer}&digits={TOTP_DIGITS}&period={TOTP_STEP}"


# -------------------------------------------------------------------------- JWT
JWT_ALG = "HS256"


def issue_jwt(key: bytes, *, user: str, ver: int, ttl: float, method: str = "password") -> tuple[str, float]:
    iat = time.time()
    exp = iat + ttl
    tok = jwt.encode(
        {"sub": user, "ver": ver, "iat": int(iat), "exp": int(exp), "jti": new_id()[:16], "amr": method},
        key,
        algorithm=JWT_ALG,
    )
    return tok, exp


def decode_jwt(key: bytes, token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, key, algorithms=[JWT_ALG], options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError as e:
        raise Unauthenticated("sesión expirada; ejecute 'dfsha login'") from e
    except jwt.PyJWTError as e:
        raise Unauthenticated("token de sesión inválido") from e
