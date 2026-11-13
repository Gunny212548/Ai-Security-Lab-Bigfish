import pytest

from app.settings import ProviderConfigError, Settings, get_settings, load_providers


def test_providers_yaml_matches_spec():
    s = get_settings()
    providers = load_providers(s.providers_file, s.default_provider)
    assert providers["cloud"].base_url == "https://api.groq.com/openai/v1"
    assert providers["cloud"].api_key_env == "GROQ_API_KEY"
    assert providers["cloud"].model == "openai/gpt-oss-120b"
    assert providers["cloud"].max_tokens == 2000
    assert set(providers) == {"cloud"}
    assert providers["cloud"].supports_seed is True


def test_api_key_env_resolves_from_settings(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = get_settings()
    providers = load_providers(s.providers_file, s.default_provider)
    fake = Settings(groq_api_key="gsk-test")
    assert providers["cloud"].resolve_api_key(fake) == "gsk-test"
    monkeypatch.setenv("GROQ_API_KEY", "gsk-env")
    assert providers["cloud"].resolve_api_key(fake) == "gsk-env"


def test_unknown_default_provider_fails():
    s = get_settings()
    with pytest.raises(ProviderConfigError, match="DEFAULT_PROVIDER='nope'"):
        load_providers(s.providers_file, "nope")


def test_provider_without_key_fails(tmp_path):
    p = tmp_path / "providers.yaml"
    p.write_text("cloud:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8")
    with pytest.raises(ProviderConfigError, match="api_key"):
        load_providers(p, "cloud")


def test_settings_defaults_match_env_example():
    s = Settings(_env_file=None)
    assert (s.llm_temperature, s.llm_seed, s.llm_max_tokens) == (0.3, 42, 400)
    assert (s.history_turns, s.rate_limit_per_min, s.default_provider) == (6, 6, "cloud")


def test_several_api_keys_comma_separated(monkeypatch):
    s = get_settings()
    providers = load_providers(s.providers_file, s.default_provider)
    monkeypatch.setenv("GROQ_API_KEY", " k1, k2 ,,k3,k1 ")
    assert providers["cloud"].resolve_api_keys() == ["k1", "k2", "k3"]
    monkeypatch.setenv("GROQ_API_KEY", "only")
    assert providers["cloud"].resolve_api_keys() == ["only"]
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert providers["cloud"].resolve_api_keys(Settings(_env_file=None)) == []
