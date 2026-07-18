from __future__ import annotations

from network_gate import (
    _network_target_allowed,
    _postgres_target_allowed,
    _sanitized_child_env,
)


def test_network_gate_rejects_fixed_runtime_ports_and_external_hosts():
    assert not _network_target_allowed(("127.0.0.1", 8000))
    assert not _network_target_allowed(("127.0.0.1", 8011))
    assert not _network_target_allowed(("127.0.0.1", 8787))
    assert not _network_target_allowed(("example.com", 443))


def test_network_gate_allows_dynamic_loopback_and_local_postgres():
    assert _network_target_allowed(("127.0.0.1", 43127))
    assert _network_target_allowed(("::1", 43127, 0, 0))
    assert _network_target_allowed(("localhost", 5432))
    assert _network_target_allowed(("127.0.0.1", 5432))
    assert _postgres_target_allowed("localhost", 5432)
    assert not _postgres_target_allowed("db.example.com", 5432)


def test_child_environment_removes_external_deepseek_and_gbrain_credentials():
    sanitized = _sanitized_child_env(
        {
            "DEEPSEEK_API_KEY": "secret",
            "GBRAIN_API_KEY": "secret",
            "GBRAIN_ENDPOINT": "https://example.invalid/mcp",
            "PATH": "keep-me",
        }
    )

    assert "DEEPSEEK_API_KEY" not in sanitized
    assert "GBRAIN_API_KEY" not in sanitized
    assert "GBRAIN_ENDPOINT" not in sanitized
    assert sanitized["PATH"] == "keep-me"


def test_child_environment_does_not_treat_a_hostname_suffix_as_loopback():
    sanitized = _sanitized_child_env(
        {"GBRAIN_ENDPOINT": "https://evil-localhost.example/mcp"}
    )

    assert "GBRAIN_ENDPOINT" not in sanitized


def test_child_environment_removes_fixed_runtime_gbrain_endpoint():
    sanitized = _sanitized_child_env(
        {
            "GBRAIN_ENDPOINT": "http://127.0.0.1:8787/mcp",
            "PATH": "keep-me",
        }
    )

    assert "GBRAIN_ENDPOINT" not in sanitized
    assert sanitized["PATH"] == "keep-me"
