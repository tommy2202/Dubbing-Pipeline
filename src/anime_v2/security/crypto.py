from __future__ import annotations

import base64
import os
import struct
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from anime_v2.config import get_settings
from anime_v2.utils.io import atomic_write_bytes


class CryptoConfigError(RuntimeError):
    pass


class CryptoFormatError(RuntimeError):
    pass


MAGIC = b"ANV2ENC"  # 7 bytes
FORMAT_VERSION_CHUNKED = 2


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise RuntimeError("cryptography is required for encryption at rest") from ex
    return AESGCM


def _enabled_classes() -> set[str]:
    s = get_settings()
    raw = str(getattr(s, "encrypt_at_rest_classes", "") or "").strip()
    if not raw:
        return {"uploads", "audio", "transcripts", "voice_memory", "review", "logs"}
    out = set()
    for p in raw.replace(";", ",").split(","):
        k = p.strip().lower()
        if k:
            out.add(k)
    return out


def encryption_enabled_for(kind: str) -> bool:
    s = get_settings()
    if not bool(getattr(s, "encrypt_at_rest", False)):
        return False
    k = str(kind or "").strip().lower()
    return k in _enabled_classes()


def _read_key_bytes() -> bytes:
    """
    Returns 32-byte key.
    Sources:
      - ARTIFACTS_KEY (base64)
      - ARTIFACTS_KEY_FILE (base64 file)
    """
    s = get_settings()
    raw = None
    try:
        if getattr(s, "artifacts_key", None):
            raw = s.artifacts_key.get_secret_value()  # type: ignore[union-attr]
    except Exception:
        raw = None

    if not raw:
        try:
            p = getattr(s, "artifacts_key_file", None)
            if p:
                key_path = Path(str(p)).expanduser().resolve()
                if key_path.exists() and key_path.is_file():
                    raw = key_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            raw = None

    if not raw:
        raise CryptoConfigError(
            "Encryption at rest enabled but ARTIFACTS_KEY/ARTIFACTS_KEY_FILE is missing."
        )

    try:
        key = base64.b64decode(str(raw).strip(), validate=True)
    except Exception as ex:
        raise CryptoConfigError("ARTIFACTS_KEY must be valid base64") from ex
    if len(key) != 32:
        raise CryptoConfigError("ARTIFACTS_KEY must decode to exactly 32 bytes") from None
    return key


def is_encrypted_path(path: Path) -> bool:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return False
    try:
        with p.open("rb") as f:
            head = f.read(len(MAGIC))
        return head == MAGIC
    except Exception:
        return False


def _aad(*, kind: str, job_id: str | None = None) -> bytes:
    # AAD is not secret; it binds ciphertext to a context to reduce accidental misuse.
    k = str(kind or "").strip().lower()
    jid = str(job_id or "").strip()
    return f"anime_v2:{k}:{jid}".encode("utf-8")


def encrypt_file(in_path: Path, out_path: Path, *, kind: str, job_id: str | None = None) -> None:
    """
    Stream-encrypt a file using chunked AES-GCM. Atomic write to out_path.
    """
    key = _read_key_bytes()
    AESGCM = _aesgcm()
    aad_base = _aad(kind=kind, job_id=job_id)

    inp = Path(in_path).resolve()
    outp = Path(out_path).resolve()
    if not inp.exists() or not inp.is_file():
        raise FileNotFoundError(str(inp))

    outp.parent.mkdir(parents=True, exist_ok=True)
    tmp = outp.with_suffix(outp.suffix + f".tmp.{os.getpid()}")

    chunk_bytes = 4 * 1024 * 1024
    # Header: MAGIC (7) + ver (1) + chunk_bytes (4, big endian)
    header = MAGIC + bytes([FORMAT_VERSION_CHUNKED]) + struct.pack(">I", int(chunk_bytes))

    try:
        with inp.open("rb") as fin, tmp.open("wb") as fout:
            fout.write(header)
            idx = 0
            while True:
                pt = fin.read(chunk_bytes)
                if not pt:
                    break
                nonce = os.urandom(12)
                aad = aad_base + b":" + str(idx).encode("ascii")
                ct = AESGCM(key).encrypt(nonce, pt, aad)
                fout.write(nonce)
                fout.write(struct.pack(">I", int(len(ct))))
                fout.write(ct)
                idx += 1
        tmp.replace(outp)
    except Exception:
        with __import__("contextlib").suppress(Exception):
            tmp.unlink(missing_ok=True)
        raise


def decrypt_file(in_path: Path, out_path: Path, *, kind: str, job_id: str | None = None) -> None:
    """
    Stream-decrypt a chunked AES-GCM file. Atomic write to out_path.
    """
    key = _read_key_bytes()
    AESGCM = _aesgcm()
    aad_base = _aad(kind=kind, job_id=job_id)

    inp = Path(in_path).resolve()
    outp = Path(out_path).resolve()
    if not inp.exists() or not inp.is_file():
        raise FileNotFoundError(str(inp))

    outp.parent.mkdir(parents=True, exist_ok=True)
    tmp = outp.with_suffix(outp.suffix + f".tmp.{os.getpid()}")

    try:
        with inp.open("rb") as fin, tmp.open("wb") as fout:
            head = fin.read(len(MAGIC))
            if head != MAGIC:
                raise CryptoFormatError("Not an encrypted file (missing header)")
            ver_b = fin.read(1)
            if not ver_b:
                raise CryptoFormatError("Corrupted encrypted file (missing version)")
            ver = int(ver_b[0])
            if ver != FORMAT_VERSION_CHUNKED:
                raise CryptoFormatError(f"Unsupported encrypted format version: {ver}")
            cb_raw = fin.read(4)
            if len(cb_raw) != 4:
                raise CryptoFormatError("Corrupted encrypted file (missing chunk size)")
            # chunk_bytes = struct.unpack(">I", cb_raw)[0]  # currently unused
            _ = struct.unpack(">I", cb_raw)[0]

            idx = 0
            while True:
                nonce = fin.read(12)
                if not nonce:
                    break
                if len(nonce) != 12:
                    raise CryptoFormatError("Corrupted encrypted file (truncated nonce)")
                ln_raw = fin.read(4)
                if len(ln_raw) != 4:
                    raise CryptoFormatError("Corrupted encrypted file (truncated length)")
                ct_len = int(struct.unpack(">I", ln_raw)[0])
                if ct_len <= 0:
                    raise CryptoFormatError("Corrupted encrypted file (invalid chunk length)")
                ct = fin.read(ct_len)
                if len(ct) != ct_len:
                    raise CryptoFormatError("Corrupted encrypted file (truncated ciphertext)")
                aad = aad_base + b":" + str(idx).encode("ascii")
                pt = AESGCM(key).decrypt(nonce, ct, aad)
                fout.write(pt)
                idx += 1
        tmp.replace(outp)
    except Exception:
        with __import__("contextlib").suppress(Exception):
            tmp.unlink(missing_ok=True)
        raise


def encrypt_bytes(data: bytes, *, kind: str, job_id: str | None = None) -> bytes:
    """
    Convenience wrapper: encrypt bytes by writing through the file format.
    """
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "in.bin"
        outp = Path(td) / "out.bin"
        inp.write_bytes(data)
        encrypt_file(inp, outp, kind=kind, job_id=job_id)
        return outp.read_bytes()


def decrypt_bytes(data: bytes, *, kind: str, job_id: str | None = None) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "in.bin"
        outp = Path(td) / "out.bin"
        inp.write_bytes(data)
        decrypt_file(inp, outp, kind=kind, job_id=job_id)
        return outp.read_bytes()


@dataclass(frozen=True, slots=True)
class Materialized:
    path: Path
    cleanup: bool


@contextmanager
def materialize_decrypted(
    path: Path,
    *,
    kind: str,
    job_id: str | None = None,
    suffix: str = ".plain",
) -> Iterator[Materialized]:
    """
    If `path` is encrypted, decrypt to a temp file and yield that.
    Otherwise yield the original path.
    """
    p = Path(path).resolve()
    if not is_encrypted_path(p):
        yield Materialized(path=p, cleanup=False)
        return

    # Decrypt to a temp file in system tmp.
    fd, tmp_name = tempfile.mkstemp(prefix="animev2_dec_", suffix=str(suffix))
    os.close(fd)
    tmp_path = Path(tmp_name).resolve()
    try:
        decrypt_file(p, tmp_path, kind=kind, job_id=job_id)
        yield Materialized(path=tmp_path, cleanup=True)
    finally:
        with __import__("contextlib").suppress(Exception):
            tmp_path.unlink(missing_ok=True)


def write_bytes_encrypted(path: Path, data: bytes, *, kind: str, job_id: str | None = None) -> None:
    """
    Atomic write of encrypted bytes at `path`. Never writes plaintext when encryption is enabled.
    """
    # Encrypt via in-memory temp file format.
    blob = encrypt_bytes(data, kind=kind, job_id=job_id)
    atomic_write_bytes(Path(path), blob)

