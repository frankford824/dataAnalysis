from finance_agent.config import DEFAULT_SOURCE_ROOTS, load_config, resolve_ssh_binary


def test_default_config_uses_finance_win_and_known_roots():
    config = load_config()
    assert config.ssh_alias == "finance-win-ro"
    assert config.source_roots == DEFAULT_SOURCE_ROOTS
    assert config.stable_for_seconds == 600
    assert config.ssh_binary == "auto"
    assert config.enrollment_token is None


def test_config_file_supports_fixture_without_credentials(tmp_path):
    fixture = tmp_path / "fixtures"
    config_file = tmp_path / "agent.toml"
    config_file.write_text(
        f"""
[agent]
control_plane_url = "http://localhost:9999/"
name = "fixture-agent"
connector = "local_fixture"
fixture_root = "{fixture}"
state_dir = "{tmp_path / 'state'}"
poll_seconds = 2
heartbeat_seconds = 7

[safety]
stable_for_seconds = 0
max_materialize_bytes = 1234

[[sources]]
path = 'D:\\Allowed'
purpose = "orders"
extensions = [".csv"]
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.control_plane_url == "http://localhost:9999"
    assert config.connector == "local_fixture"
    assert config.fixture_root == fixture
    assert config.max_materialize_bytes == 1234
    assert config.source_roots[0].extensions == (".csv",)


def test_explicit_ssh_binary_is_preserved():
    assert resolve_ssh_binary("/opt/openssh/bin/ssh") == "/opt/openssh/bin/ssh"
