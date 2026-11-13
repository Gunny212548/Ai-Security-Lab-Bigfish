"""App settings (.env) and named LLM providers (config/providers.yaml)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    app_secret: str = "change-me"
    admin_password: str = "change-me"
    default_provider: str = "cloud"
    groq_api_key: str = ""
    llm_temperature: float = 0.3
    llm_seed: int = 42
    llm_max_tokens: int = 400
    history_turns: int = 6
    rate_limit_per_min: int = 6
    fast_solve_seconds: int = 90  # admin marks correct flags faster than this (from first page view) with ⚡
    per_team_flags: bool = True  # each team gets its own flag variant (app/flags.py)
    log_level: str = "INFO"
    activity_log_file: str = (ROOT_DIR / "data" / "activity.log").as_posix()  # "" = Docker logs only

    database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'bigfishdesk.db').as_posix()}"
    challenges_dir: Path = ROOT_DIR / "challenges"
    providers_file: Path = ROOT_DIR / "config" / "providers.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    max_tokens: int | None = None  # overrides LLM_MAX_TOKENS (reasoning models need room to think)
    supports_seed: bool = True  # false for an endpoint that rejects `seed` with 400

    @model_validator(mode="after")
    def _one_key_source(self) -> "Provider":
        if not self.api_key and not self.api_key_env:
            raise ValueError("provider needs `api_key` or `api_key_env`")
        return self

    def resolve_api_keys(self, settings: Settings | None = None) -> list[str]:
        """Every key, in order: the setting may list several, comma-separated (GROQ_API_KEY=k1,k2,k3). The app uses
        the first and moves to the next when one is rate-limited, used up or rejected (app/llm.py KeyPool)."""
        keys: list[str] = []
        for k in self.resolve_api_key(settings).split(","):
            if k.strip() and k.strip() not in keys:
                keys.append(k.strip())
        return keys

    def resolve_api_key(self, settings: Settings | None = None) -> str:
        if self.api_key:
            return self.api_key
        env_name = self.api_key_env or ""
        value = os.environ.get(env_name, "")
        if not value and settings is not None:
            # pydantic-settings reads .env without exporting it to os.environ
            value = getattr(settings, env_name.lower(), "") or ""
        return value


class ProviderConfigError(ValueError):
    pass


def load_providers(path: Path, default_provider: str) -> dict[str, Provider]:
    if not path.exists():
        raise ProviderConfigError(f"providers file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not raw:
        raise ProviderConfigError(f"{path}: expected a mapping of provider name -> settings")
    providers: dict[str, Provider] = {}
    for name, cfg in raw.items():
        try:
            providers[name] = Provider(name=name, **(cfg or {}))
        except Exception as e:  # noqa: BLE001 - re-raise with file context
            raise ProviderConfigError(f"{path}: provider '{name}': {e}") from e
    if default_provider not in providers:
        raise ProviderConfigError(
            f"DEFAULT_PROVIDER='{default_provider}' not found in {path} (have: {', '.join(providers)})"
        )
    return providers


@lru_cache
def get_providers() -> dict[str, Provider]:
    s = get_settings()
    return load_providers(s.providers_file, s.default_provider)
