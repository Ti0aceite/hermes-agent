from pathlib import Path

from gateway import run as gateway_run


def test_resolve_gateway_max_tokens_defaults_to_4096(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_MAX_OUTPUT_TOKENS", raising=False)

    assert gateway_run._resolve_gateway_max_tokens() == 4096


def test_resolve_gateway_max_tokens_reads_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n"
        "  max_output_tokens: 2048\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_MAX_OUTPUT_TOKENS", raising=False)

    assert gateway_run._resolve_gateway_max_tokens() == 2048


def test_resolve_gateway_max_tokens_env_overrides_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n"
        "  max_output_tokens: 2048\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_MAX_OUTPUT_TOKENS", "1024")

    assert gateway_run._resolve_gateway_max_tokens() == 1024
