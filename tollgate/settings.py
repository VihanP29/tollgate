"""Explicit runtime capabilities. Credentials alone never enable paid execution."""
from __future__ import annotations

import os
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["local", "hosted"] = "local"
    database_url: str = "sqlite:///.runtime/platform.sqlite"
    public_origin: str = "http://127.0.0.1:8765"
    identity_issuer: str = ""
    identity_audience: str = ""
    identity_jwks_url: str = ""
    live_jev_enabled: bool = False
    live_agent_enabled: bool = False
    claims_mode: Literal["off", "observe", "enforce"] = "off"
    vision_enabled: bool = False
    workspace_calls: int = Field(default=100, ge=0, le=100000)
    global_calls: int = Field(default=1000, ge=0, le=1000000)
    workspace_tokens: int = Field(default=500000, ge=0)
    global_tokens: int = Field(default=2000000, ge=0)
    evaluation_usage_fraction: float = Field(default=0.2, ge=0, le=0.5)
    max_jobs: int = Field(default=20, ge=1, le=1000)
    global_jobs: int = Field(default=100, ge=1, le=1000)
    max_proposals: int = Field(default=500, ge=20, le=5000)
    max_runs: int = Field(default=20, ge=1, le=1000)
    max_steps: int = Field(default=45, ge=1, le=100)
    provider_timeout_s: int = Field(default=15, ge=1, le=120)
    max_retries: int = Field(default=2, ge=0, le=3)
    lease_seconds: int = Field(default=120, ge=30)
    max_request_bytes: int = Field(default=65536, ge=1024, le=1048576)
    worker_stale_seconds: int = Field(default=180, ge=30)

    @model_validator(mode="after")
    def validate_environment(self):
        if ":memory:" in self.database_url:
            raise ValueError("The workspace platform requires a durable database, not in-memory SQLite")
        if self.mode == "hosted":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Hosted mode requires PostgreSQL via psycopg")
            if not all((self.identity_issuer, self.identity_audience, self.identity_jwks_url)):
                raise ValueError("Hosted mode requires verified JWT identity; local operator tokens are prohibited")
            if not self.public_origin.startswith("https://") or not self.identity_jwks_url.startswith("https://"):
                raise ValueError("Hosted identity and public origin require HTTPS")
        # No new public live execution until a matching admission system is installed.
        if self.live_jev_enabled or self.live_agent_enabled or self.claims_mode == "enforce":
            raise ValueError("Platform live execution/enforcing claims unavailable pending matching live admission")
        if self.vision_enabled:
            raise ValueError("Visual inspection is unavailable in this release")
        return self

    @classmethod
    def from_env(cls):
        names = {name: "TOLLGATE_" + name.upper() for name in cls.model_fields}
        names["database_url"] = "DATABASE_URL"
        return cls(**{name: os.environ[key] for name, key in names.items() if key in os.environ})


def load_env(path, names=None):
    """Load the documented settings without printing values; environment wins."""
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        return
    allowed = {"TOLLGATE_"+name.upper() for name in Settings.model_fields} | {"DATABASE_URL", "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL",
               "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "TOLLGATE_OPERATOR_TOKEN", "TOLLGATE_AGENT_MODEL", "TOLLGATE_AUDITOR_TOKEN"}
    if names is not None:
        allowed &= set(names)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if key.strip() in allowed and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
