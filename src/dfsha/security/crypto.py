"""Primitivas criptográficas de DFSha.

Jerarquía de claves (cifrado de sobre / *envelope encryption*)
---------------------------------------------------------------
``cluster.key`` (secreto maestro, 256 bit, sólo ControlNodes)
    ├─ HKDF("kek")          → KEK: envuelve la DEK de cada archivo (se guarda
    │                         envuelta en la máquina de estados replicada).
    ├─ HKDF("jwt")          → firma de tokens de sesión de usuario (HS256).
    ├─ HKDF("block-token")  → sella los *capability tokens* de bloque; también
    │                         se entrega (por mTLS) a los DataNodes.
    └─ HKDF("secrets")      → envuelve secretos TOTP en reposo.

Cada archivo tiene una **DEK** aleatoria de 256 bit.  Los DataNodes cifran
cada bloque con AES-256-GCM usando esa DEK, pero **no la almacenan**: llega
sólo dentro del token sellado de la petición y vive en memoria durante la
operación.  Robar el disco de un DataNode no entrega ni la clave ni el texto
claro.  Los bloques en reposo son blobs autoverificables (ver ``encrypt_block``).
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..common.errors import CorruptBlock, Unauthenticated
from ..common.util import b64d, b64e, new_secret

BLOCK_MAGIC = b"DFSB"
BLOCK_VERSION = 1
_HDR = struct.Struct(">4sBI12s")  # magic, version, generation, nonce
_TAIL = 32  # sha256 del blob completo (sin la cola)


def hkdf(master: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=b"dfsha-v1", info=info).derive(master)


@dataclass(frozen=True)
class ClusterKeys:
    """Claves derivadas del secreto maestro."""

    kek: bytes
    jwt: bytes
    block_token: bytes
    secrets: bytes

    @classmethod
    def from_master(cls, master: bytes) -> ClusterKeys:
        return cls(
            kek=hkdf(master, b"kek"),
            jwt=hkdf(master, b"jwt"),
            block_token=hkdf(master, b"block-token"),
            secrets=hkdf(master, b"secrets"),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> ClusterKeys:
        return cls.from_master(b64d(Path(path).read_text().strip()))


# ------------------------------------------------------------- envoltura de claves


def wrap(key: bytes, plaintext: bytes, aad: bytes) -> str:
    nonce = new_secret(12)
    return b64e(nonce + AESGCM(key).encrypt(nonce, plaintext, aad))


def unwrap(key: bytes, wrapped: str, aad: bytes) -> bytes:
    raw = b64d(wrapped)
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag as e:  # pragma: no cover - sólo con datos manipulados
        raise Unauthenticated("clave envuelta inválida") from e


def new_dek() -> bytes:
    return new_secret(32)


# --------------------------------------------------------------- bloques en reposo


def _block_aad(block_id: str, gen: int) -> bytes:
    return b"dfsha-block|" + block_id.encode() + b"|" + str(gen).encode()


def encrypt_block(dek: bytes, block_id: str, gen: int, plaintext: bytes) -> bytes:
    """Devuelve el blob que se persiste en el DataNode.

    ``MAGIC | ver | gen | nonce(12) | AES-GCM(ct||tag) | SHA-256(todo lo anterior)``

    * El AAD liga el ciphertext a ``(block_id, gen)``: copiar el blob de un
      bloque sobre otro no pasa la verificación (anti *block swapping*).
    * La cola SHA-256 permite al *scrubber* detectar *bit rot* **sin** la DEK.
    """
    nonce = new_secret(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext, _block_aad(block_id, gen))
    body = _HDR.pack(BLOCK_MAGIC, BLOCK_VERSION, gen, nonce) + ct
    return body + hashlib.sha256(body).digest()


def verify_blob(blob: bytes) -> bool:
    """Integridad del blob en reposo, sin necesidad de la DEK."""
    if len(blob) < _HDR.size + 16 + _TAIL or blob[:4] != BLOCK_MAGIC:
        return False
    return hashlib.sha256(blob[:-_TAIL]).digest() == blob[-_TAIL:]


def blob_generation(blob: bytes) -> int:
    return _HDR.unpack_from(blob)[2]


def decrypt_block(dek: bytes, block_id: str, blob: bytes) -> bytes:
    if not verify_blob(blob):
        raise CorruptBlock(f"blob de {block_id} corrupto (hash de cola)")
    _magic, _ver, gen, nonce = _HDR.unpack_from(blob)
    try:
        return AESGCM(dek).decrypt(nonce, blob[_HDR.size : -_TAIL], _block_aad(block_id, gen))
    except InvalidTag as e:
        raise CorruptBlock(f"bloque {block_id}: falla de autenticación AES-GCM") from e


# ------------------------------------------------------- tokens sellados (capabilities)

_TOKEN_PREFIX = "v1."


def seal(key: bytes, payload: dict, *, ttl: float) -> str:
    """Token opaco: AES-GCM(JSON{...,exp}).  Confidencial *y* autenticado.

    Opaco para el cliente: no puede leer (la DEK va dentro) ni forjar.
    """
    body = dict(payload)
    body["exp"] = time.time() + ttl
    nonce = new_secret(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(body, separators=(",", ":")).encode(), b"dfsha-token")
    return _TOKEN_PREFIX + b64e(nonce + ct)


def unseal(key: bytes, token: str) -> dict:
    if not token.startswith(_TOKEN_PREFIX):
        raise Unauthenticated("token con formato inválido")
    try:
        raw = b64d(token[len(_TOKEN_PREFIX) :])
        body = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], b"dfsha-token"))
    except (InvalidTag, ValueError) as e:
        raise Unauthenticated("token inválido") from e
    if body.get("exp", 0) < time.time():
        raise Unauthenticated("token expirado")
    return body
