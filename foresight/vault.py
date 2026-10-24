"""Encryption at rest for the files Foresight Wing keeps: meeting audio, imported recordings, uploads and thumbnails.

Each file is AES-256-GCM in 64 KiB chunks, so audio can be decoded from any point and the player can ask for byte
ranges without decrypting the whole file. Chunk i uses nonce = file prefix (8 random bytes) + i, and its additional
data marks whether it is the last chunk, so chunks cannot be reordered, swapped between files or silently cut off.

The key is random, made once, and stored wrapped by Windows DPAPI for the signed-in user (`files.key` in the data
folder): only this Windows account on this PC can unwrap it, with no password to type. Copying the data folder to
another PC or account copies unreadable files. The database is not encrypted (transcripts and text chunks live there).
Files Foresight Wing did not create (the user's own documents, pictures and memos in watched folders) are never touched.
"""
from __future__ import annotations

import ctypes
import io
import logging
import os
import struct
import sys
import threading
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

log = logging.getLogger(__name__)

MAGIC = b"FSV1"
CHUNK = 64 * 1024
TAG = 16
HEADER = 16  # MAGIC + chunk size (uint32 LE) + 8-byte nonce prefix
KEY_FILE = settings.data_dir / "files.key"
_ENTROPY = b"Foresight for Windows: files at rest v1"

_key: bytes | None = None
_key_lock = threading.Lock()


class VaultError(RuntimeError):
    pass


# ---------------------------------------------------------------- the key (DPAPI, current Windows user)

def _dpapi(data: bytes, protect: bool) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    entropy_buf = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    entropy = Blob(len(_ENTROPY), ctypes.cast(entropy_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "Foresight files", ctypes.byref(entropy), None, None, flags,
                                      ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, flags,
                                        ctypes.byref(blob_out))
    if not ok:
        raise VaultError(f"Windows DPAPI {'protect' if protect else 'unprotect'} failed (error {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def available() -> bool:
    return sys.platform == "win32" and settings.encrypt_files


def key() -> bytes:
    """The files key, unwrapped with DPAPI (made and wrapped on first use)."""
    global _key
    with _key_lock:
        if _key is None:
            if KEY_FILE.is_file():
                _key = _dpapi(KEY_FILE.read_bytes(), protect=False)
            else:
                fresh = AESGCM.generate_key(bit_length=256)
                KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp = KEY_FILE.with_suffix(".tmp")
                tmp.write_bytes(_dpapi(fresh, protect=True))
                os.replace(tmp, KEY_FILE)
                _key = fresh
        return _key


# ---------------------------------------------------------------- file format

def _aad(header: bytes, index: int, last: bool) -> bytes:
    return header + struct.pack(">I", index) + (b"\x01" if last else b"\x00")


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def is_encrypted(path: str | Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


class Writer(io.RawIOBase):
    """Write-only, streaming: chunks are sealed as they fill; the last one when closed."""

    def __init__(self, path: str | Path) -> None:
        self._f = open(path, "wb")
        self._aes = AESGCM(key())
        self._prefix = os.urandom(8)
        self._header = MAGIC + struct.pack("<I", CHUNK) + self._prefix
        self._f.write(self._header)
        self._buf = bytearray()
        self._index = 0
        self._pos = 0

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self._buf += data
        while len(self._buf) > CHUNK:  # keep at least one byte back: the last chunk is sealed differently
            self._seal(bytes(self._buf[:CHUNK]), last=False)
            del self._buf[:CHUNK]
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def _seal(self, chunk: bytes, last: bool) -> None:
        self._f.write(self._aes.encrypt(_nonce(self._prefix, self._index), chunk,
                                        _aad(self._header, self._index, last)))
        self._index += 1

    def close(self) -> None:
        if not self.closed:
            try:
                self._seal(bytes(self._buf), last=True)
                self._f.close()
            finally:
                super().close()


class Reader(io.RawIOBase):
    """Read-only and seekable, decrypting one chunk at a time."""

    def __init__(self, path: str | Path) -> None:
        self._f = open(path, "rb")
        self._header = self._f.read(HEADER)
        if len(self._header) != HEADER or self._header[:4] != MAGIC:
            self._f.close()
            raise VaultError(f"{Path(path).name} is not an encrypted Foresight Wing file")
        self._chunk = struct.unpack("<I", self._header[4:8])[0]
        self._prefix = self._header[8:16]
        self._aes = AESGCM(key())
        body = os.fstat(self._f.fileno()).st_size - HEADER
        stride = self._chunk + TAG
        self._count = max(1, -(-body // stride))
        last_len = body - (self._count - 1) * stride - TAG
        if last_len < 0:
            raise VaultError(f"{Path(path).name} is damaged")
        self.size = (self._count - 1) * self._chunk + last_len
        self._pos = 0
        self._cached: tuple[int, bytes] | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _plain(self, index: int) -> bytes:
        if self._cached and self._cached[0] == index:
            return self._cached[1]
        stride = self._chunk + TAG
        self._f.seek(HEADER + index * stride)
        sealed = self._f.read(stride)
        last = index == self._count - 1
        try:
            plain = self._aes.decrypt(_nonce(self._prefix, index), sealed, _aad(self._header, index, last))
        except Exception:
            # A recording cut short by a crash ends without its sealed last chunk: what is there is still genuine.
            try:
                plain = self._aes.decrypt(_nonce(self._prefix, index), sealed, _aad(self._header, index, False)) \
                    if last else None
            except Exception:
                plain = None
            if plain is None:
                raise VaultError("this file was changed or damaged, or was encrypted under another Windows account")
        self._cached = (index, plain)
        return plain

    def readinto(self, buffer) -> int:
        if self._pos >= self.size:
            return 0
        index, offset = divmod(self._pos, self._chunk)
        piece = self._plain(index)[offset:offset + len(buffer)]
        buffer[:len(piece)] = piece
        self._pos += len(piece)
        return len(piece)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self._pos
        out = bytearray()
        while len(out) < size and self._pos < self.size:
            index, offset = divmod(self._pos, self._chunk)
            piece = self._plain(index)[offset:offset + size - len(out)]
            out += piece
            self._pos += len(piece)
        return bytes(out)

    def close(self) -> None:
        if not self.closed:
            self._f.close()
            super().close()


# ---------------------------------------------------------------- helpers used across the app

def open_read(path: str | Path) -> BinaryIO:
    """A readable, seekable file object with the plain bytes, whether or not the file is encrypted."""
    return Reader(path) if is_encrypted(path) else open(path, "rb")


def read_bytes(path: str | Path) -> bytes:
    with open_read(path) as f:
        return f.read()


def plain_size(path: str | Path) -> int:
    if is_encrypted(path):
        with Reader(path) as r:
            return r.size
    return Path(path).stat().st_size


def open_write(path: str | Path) -> BinaryIO:
    """Where Foresight Wing writes a file it keeps: encrypted when encryption is on."""
    return Writer(path) if available() else open(path, "wb")


def write_bytes(path: str | Path, data: bytes) -> None:
    """Write a file atomically (encrypted when encryption is on)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    with open_write(tmp) as f:
        f.write(data)
    os.replace(tmp, path)


def encrypt_file(path: str | Path) -> bool:
    """Encrypt a plain file in place (atomically). Returns False if it was already encrypted or encryption is off."""
    path = Path(path)
    if not available() or is_encrypted(path):
        return False
    tmp = path.with_name(path.name + ".part")
    with open(path, "rb") as src, Writer(tmp) as dst:
        while block := src.read(1 << 20):
            dst.write(block)
    os.replace(tmp, path)
    return True


def encrypt_existing(folders: list[Path], skip: set[str] | None = None) -> int:
    """Encrypt plain files left from before encryption was on (once, in the background). `skip`: paths being written."""
    if not available():
        return 0
    skip = {os.path.normcase(os.path.abspath(p)) for p in (skip or set())}
    done = 0
    for folder in folders:
        if not folder.is_dir():
            continue
        for p in folder.rglob("*"):
            if not p.is_file() or p.name.endswith(".part") or os.path.normcase(os.path.abspath(p)) in skip:
                continue
            try:
                done += encrypt_file(p)
            except OSError as exc:  # e.g. open in another program: next start
                log.warning("could not encrypt %s: %s", p, exc)
    if done:
        log.info("encrypted %d existing files", done)
    return done
