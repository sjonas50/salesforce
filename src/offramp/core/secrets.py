"""Secret loading abstraction.

Phase 0 ships a filesystem + env-var loader. Production implementations for
Azure Key Vault and AWS Secrets Manager follow in Phase 5 (helm chart wiring).
The :class:`SecretSource` Protocol fixes the contract so swapping backends does
not require changes to caller code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class SecretSource(Protocol):
    """Backend-agnostic secret retrieval."""

    def get(self, key: str) -> str:
        """Return the secret value for ``key`` or raise ``KeyError``."""
        ...


class EnvSecretSource:
    """Read secrets from environment variables.

    Used in local dev and CI. Refuses to start if a required key is missing
    rather than silently returning ``""``.
    """

    def get(self, key: str) -> str:
        try:
            return os.environ[key]
        except KeyError as exc:
            raise KeyError(f"Required secret not in environment: {key}") from exc


class FileSecretSource:
    """Read secrets from a directory of one-secret-per-file (Kubernetes pattern).

    The init-container materializes Key Vault / Secrets Manager values into a
    tmpfs mount; this loader reads them lazily on demand.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def get(self, key: str) -> str:
        path = self.base_dir / key
        if not path.is_file():
            raise KeyError(f"Required secret file missing: {path}")
        return path.read_text(encoding="utf-8").strip()


def default_source() -> SecretSource:
    """Pick the right backend for the current process.

    If ``OFFRAMP_SECRETS_DIR`` is set and exists, use the file-mount backend.
    Otherwise fall back to environment variables.
    """
    secrets_dir = os.environ.get("OFFRAMP_SECRETS_DIR")
    if secrets_dir and Path(secrets_dir).is_dir():
        return FileSecretSource(Path(secrets_dir))
    return EnvSecretSource()
