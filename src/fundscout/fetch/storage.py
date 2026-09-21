"""Raw snapshot storage. Local filesystem now; the interface is S3-ready."""

import os
import tempfile
from pathlib import Path
from typing import Protocol

from fundscout.fetch.hashing import sha256_hex


class RawStorage(Protocol):
    def save(self, content: bytes) -> str:
        """Store content and return its storage key."""
        ...

    def load(self, key: str) -> bytes: ...


class LocalRawStorage:
    """Content-addressed files: `<base>/<sha[:2]>/<sha[2:4]>/<sha>` (SHA-256 of the bytes)."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def save(self, content: bytes) -> str:
        digest = sha256_hex(content)
        key = f"{digest[:2]}/{digest[2:4]}/{digest}"
        path = self.base_dir / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp, path)
        return key

    def load(self, key: str) -> bytes:
        return (self.base_dir / key).read_bytes()
