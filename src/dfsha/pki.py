"""PKI propia de DFSha: una CA raíz y un certificado por nodo.

Modelo de confianza
-------------------
* Una CA privada firma un certificado por nodo (ECDSA P-256).  El campo
  ``OU`` del subject codifica el *rol* (``controlnode`` | ``datanode``) y el
  ``CN`` el identificador del nodo.
* Los listeners **internos** exigen certificado de cliente firmado por la CA
  (mTLS): sólo un nodo emitido por el operador puede hablar con el plano de
  control (Raft, registro de DataNodes, replicación DN↔DN).
* Los listeners **públicos** usan TLS de servidor; los usuarios se autentican
  a nivel de aplicación (usuario/clave + 2FA, API key) y verifican al servidor
  contra ``ca.pem``.

CLI::

    dfsha-pki init   --dir pki
    dfsha-pki issue  --dir pki --name c1 --role controlnode --san localhost --san 10.0.1.5
    dfsha-pki secret --file secrets/cluster.key
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import ssl
from pathlib import Path

import click
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .common.util import atomic_write, b64e, new_secret

ROLES = ("controlnode", "datanode", "admin")


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    atomic_write(
        path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        mode=0o600,
    )


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    atomic_write(path, cert.public_bytes(serialization.Encoding.PEM), mode=0o644)


def create_ca(directory: str | Path, *, days: int = 3650, name: str = "DFSha Root CA") -> Path:
    """Crea ``ca.pem`` y ``ca.key``.  Idempotente: no pisa una CA existente."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    ca_pem, ca_key = d / "ca.pem", d / "ca.key"
    if ca_pem.exists() and ca_key.exists():
        return ca_pem
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DFSha"),
            x509.NameAttribute(NameOID.COMMON_NAME, name),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(ca_key, key)
    _write_cert(ca_pem, cert)
    return ca_pem


def issue_cert(
    directory: str | Path,
    *,
    name: str,
    role: str,
    sans: list[str] | None = None,
    days: int = 825,
) -> tuple[Path, Path]:
    """Emite ``<name>.crt``/``<name>.key`` firmados por la CA de ``directory``."""
    if role not in ROLES:
        raise ValueError(f"rol inválido {role!r}; use {ROLES}")
    d = Path(directory)
    ca_key = serialization.load_pem_private_key((d / "ca.key").read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DFSha"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role),
            x509.NameAttribute(NameOID.COMMON_NAME, name),
        ]
    )
    alt: list[x509.GeneralName] = []
    for s in {*(sans or []), name, "localhost", "127.0.0.1"}:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(s)))
        except ValueError:
            alt.append(x509.DNSName(s))
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    crt_path, key_path = d / f"{name}.crt", d / f"{name}.key"
    _write_cert(crt_path, cert)
    _write_key(key_path, key)
    return crt_path, key_path


def create_cluster_secret(path: str | Path) -> Path:
    """Secreto maestro del cluster (32 bytes).  Idempotente.

    De él se derivan (HKDF) la KEK que envuelve las claves de archivo, la
    clave de firma de JWT y la clave de los tokens de bloque.  Sólo lo reciben
    los ControlNodes; los DataNodes únicamente reciben la clave de tokens de
    bloque (por mTLS) y jamás ven la KEK.
    """
    p = Path(path)
    if not p.exists():
        atomic_write(p, b64e(new_secret(32)).encode() + b"\n", mode=0o600)
    return p


def peer_identity(peercert: dict | None) -> tuple[str, str] | None:
    """(CN, OU) a partir del dict de ``ssl.SSLSocket.getpeercert()``."""
    if not peercert:
        return None
    cn = ou = ""
    for rdn in peercert.get("subject", ()):
        for key, val in rdn:
            if key == "commonName":
                cn = val
            elif key == "organizationalUnitName":
                ou = val
    return (cn, ou) if cn else None


def server_context(cert: str, key: str, ca: str, *, require_client_cert: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    ctx.load_verify_locations(ca)
    ctx.verify_mode = ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_NONE
    return ctx


def client_context(ca: str, cert: str | None = None, key: str | None = None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(ca)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if cert and key:
        ctx.load_cert_chain(cert, key)
    return ctx


# --------------------------------------------------------------------------- CLI


@click.group(help="Gestión de la PKI y secretos de un cluster DFSha.")
def main() -> None:  # pragma: no cover - thin CLI
    pass


@main.command("init", help="Crea la CA raíz.")
@click.option("--dir", "directory", default="pki", show_default=True)
def _init(directory: str) -> None:  # pragma: no cover
    click.echo(f"CA: {create_ca(directory)}")


@main.command("issue", help="Emite el certificado de un nodo.")
@click.option("--dir", "directory", default="pki", show_default=True)
@click.option("--name", required=True, help="Identificador del nodo (CN).")
@click.option("--role", type=click.Choice(ROLES), required=True)
@click.option("--san", multiple=True, help="DNS/IP adicionales (repetible).")
def _issue(directory: str, name: str, role: str, san: tuple[str, ...]) -> None:  # pragma: no cover
    crt, key = issue_cert(directory, name=name, role=role, sans=list(san))
    click.echo(f"{crt}\n{key}")


@main.command("secret", help="Genera el secreto maestro del cluster.")
@click.option("--file", "path", default="secrets/cluster.key", show_default=True)
def _secret(path: str) -> None:  # pragma: no cover
    click.echo(f"secreto: {create_cluster_secret(path)}")
