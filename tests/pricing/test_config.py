from pathlib import Path

import pytest

from ultimatescrape.pricing import config


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("ONETAKE_DATABASE_URL", "postgresql://env-wins")
    assert config.warehouse_dsn() == "postgresql://env-wins"


def test_env_local_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("ONETAKE_DATABASE_URL", raising=False)
    envfile = tmp_path / ".env.local"
    envfile.write_text('FOO=1\nDATABASE_URL="postgresql://from-file"\n')
    monkeypatch.setattr(config, "ENV_LOCAL", envfile)
    assert config.warehouse_dsn() == "postgresql://from-file"


def test_missing_dsn_is_loud(monkeypatch, tmp_path):
    monkeypatch.delenv("ONETAKE_DATABASE_URL", raising=False)
    monkeypatch.setattr(config, "ENV_LOCAL", tmp_path / "nope")
    with pytest.raises(RuntimeError, match="ONETAKE_DATABASE_URL"):
        config.warehouse_dsn()


def test_proxy_secret_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("ONETAKE_PROXY_SECRET", raising=False)
    envfile = tmp_path / ".env.local"
    envfile.write_text("PROXY_SECRET=shh\n")
    monkeypatch.setattr(config, "ENV_LOCAL", envfile)
    assert config.proxy_secret() == "shh"
