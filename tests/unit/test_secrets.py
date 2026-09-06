from __future__ import annotations

from pathlib import Path

import pytest

from offramp.core.secrets import EnvSecretSource, FileSecretSource, default_source


def test_env_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFRAMP_TEST_SECRET", "from-env")
    assert EnvSecretSource().get("OFFRAMP_TEST_SECRET") == "from-env"
    with pytest.raises(KeyError):
        EnvSecretSource().get("OFFRAMP_DEFINITELY_MISSING")


def test_file_source_and_default_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "sf_jwt").write_text("  pem-body \n")
    src = FileSecretSource(tmp_path)
    assert src.get("sf_jwt") == "pem-body"
    with pytest.raises(KeyError):
        src.get("nope")
    monkeypatch.setenv("OFFRAMP_SECRETS_DIR", str(tmp_path))
    assert isinstance(default_source(), FileSecretSource)
    monkeypatch.delenv("OFFRAMP_SECRETS_DIR")
    assert isinstance(default_source(), EnvSecretSource)
