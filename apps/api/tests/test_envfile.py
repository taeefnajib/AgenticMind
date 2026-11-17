import os

from agenticmind_api.envfile import load_env_file


def test_env_file_loads_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nAM_TEST_NEW=from-file\nAM_TEST_EXISTING=from-file\nAM_TEST_EMPTY=\n", encoding="utf-8"
    )
    monkeypatch.setenv("AM_TEST_EXISTING", "from-shell")
    monkeypatch.delenv("AM_TEST_NEW", raising=False)
    monkeypatch.delenv("AM_TEST_EMPTY", raising=False)
    loaded = load_env_file(env)
    try:
        assert loaded == ["AM_TEST_NEW"]
        assert os.environ["AM_TEST_NEW"] == "from-file"
        assert os.environ["AM_TEST_EXISTING"] == "from-shell"  # real environment wins
        assert "AM_TEST_EMPTY" not in os.environ  # empty placeholders are ignored
    finally:
        os.environ.pop("AM_TEST_NEW", None)


def test_missing_env_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []
