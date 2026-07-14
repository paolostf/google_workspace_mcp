from pathlib import Path

import pytest

from core import cli


def test_default_profile_preserves_legacy_token_directory(monkeypatch, tmp_path):
    token_dir = tmp_path / "cli-tokens"
    monkeypatch.setattr(cli, "TOKEN_DIR", str(token_dir))

    assert cli._profile_token_dir() == str(token_dir)
    assert cli._profile_token_dir("default") == str(token_dir)


def test_named_profiles_are_isolated_under_token_directory(monkeypatch, tmp_path):
    token_dir = tmp_path / "cli-tokens"
    monkeypatch.setattr(cli, "TOKEN_DIR", str(token_dir))

    personal = Path(cli._profile_token_dir("gws-personal"))
    business = Path(cli._profile_token_dir("gws-business"))

    assert personal == token_dir / "gws-personal"
    assert business == token_dir / "gws-business"
    assert personal != business


@pytest.mark.parametrize(
    "profile",
    ["", "../escape", "/absolute", "with space", "a" * 65, None],
)
def test_profile_rejects_unsafe_names(profile):
    with pytest.raises(ValueError, match="profile must be"):
        cli._profile_token_dir(profile)


def test_named_profile_storage_uses_shared_key_and_isolated_directory(
    monkeypatch, tmp_path
):
    cli_home = tmp_path / ".workspace-mcp"
    token_dir = cli_home / "cli-tokens"
    key_path = cli_home / ".cli-encryption-key"
    monkeypatch.setattr(cli, "TOKEN_DIR", str(token_dir))
    monkeypatch.setattr(cli, "KEY_PATH", str(key_path))

    personal = cli._get_token_storage("gws-personal")
    business = cli._get_token_storage("gws-business")

    assert personal is not None
    assert business is not None
    assert (token_dir / "gws-personal").is_dir()
    assert (token_dir / "gws-business").is_dir()
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_build_oauth_forwards_explicit_scopes(monkeypatch):
    captured = {}

    class FakeOAuth:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(cli, "OAuth", FakeOAuth)
    monkeypatch.setattr(cli, "_get_token_storage", lambda profile: f"store:{profile}")
    scopes = ["openid", "https://www.googleapis.com/auth/gmail.modify"]

    cli.build_oauth("gws-personal", scopes)

    assert captured == {"token_storage": "store:gws-personal", "scopes": scopes}
