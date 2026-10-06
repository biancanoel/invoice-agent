import stat

from invoice_agent.google_auth import save_token


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_token_is_readable_by_owner_only(tmp_path):
    token = tmp_path / "token.json"
    save_token(token, '{"refresh_token": "secret"}')
    assert mode(token) == 0o600
    assert token.read_text() == '{"refresh_token": "secret"}'


def test_existing_world_readable_token_is_locked_down(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("old")
    token.chmod(0o644)
    save_token(token, "new")
    assert mode(token) == 0o600
    assert token.read_text() == "new"
