"""Load and validate configuration from config.yaml + .env.

Nothing target-specific is hardcoded anywhere else in the codebase; every
site-specific value flows through the Config object built here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when config.yaml is missing required values or is malformed."""


@dataclass
class OutputConfig:
    csv_path: str = "./out/listings.csv"
    google_sheet_id: str = ""
    sheet_tab: str = "listings"


@dataclass
class PaginationConfig:
    mode: str = "none"            # none | query | link
    query_param: str = "page"
    start_page: int = 1
    next_selector: Optional[str] = None


@dataclass
class Secrets:
    discord_webhook_url: Optional[str] = None
    google_service_account_json: Optional[str] = None


@dataclass
class Config:
    target_url: str
    rate_limit_seconds: float
    max_pages: int
    dedupe_key: str
    fields: dict[str, Optional[str]]
    row_selector: Optional[str]
    pagination: PaginationConfig
    output: OutputConfig
    user_agent: str = "config-scraper/1.0"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    jitter_min_seconds: float = 0.5
    jitter_max_seconds: float = 1.5
    secrets: Secrets = field(default_factory=Secrets)

    # Convenience view of only the fields that actually have a selector set.
    @property
    def active_fields(self) -> dict[str, str]:
        return {k: v for k, v in self.fields.items() if v}


def _require(raw: dict[str, Any], key: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise ConfigError(f"config.yaml is missing required key: '{key}'")
    return raw[key]


def load_config(config_path: str = "config.yaml", env_path: str = ".env") -> Config:
    """Read config.yaml and .env, returning a validated Config.

    Secrets come from the environment (loaded from .env if present); they are
    never read from config.yaml.
    """
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path.resolve()}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # Load .env if it exists; real secrets may also come straight from the
    # environment (e.g. GitHub Actions secrets), so a missing .env is fine.
    if Path(env_path).exists():
        load_dotenv(env_path)
    else:
        load_dotenv()  # no-op if there is nothing to load

    target_url = _require(raw, "target_url")
    dedupe_key = _require(raw, "dedupe_key")

    fields = raw.get("fields") or {}
    if not isinstance(fields, dict):
        raise ConfigError("config.yaml 'fields' must be a mapping of name -> selector")

    pag_raw = raw.get("pagination") or {}
    pagination = PaginationConfig(
        mode=(pag_raw.get("mode") or "none").lower(),
        query_param=pag_raw.get("query_param") or "page",
        start_page=int(pag_raw.get("start_page") or 1),
        next_selector=pag_raw.get("next_selector") or None,
    )
    if pagination.mode not in ("none", "query", "link"):
        raise ConfigError(
            f"pagination.mode must be one of none|query|link, got '{pagination.mode}'"
        )

    out_raw = raw.get("output") or {}
    output = OutputConfig(
        csv_path=out_raw.get("csv_path") or "./out/listings.csv",
        google_sheet_id=out_raw.get("google_sheet_id") or "",
        sheet_tab=out_raw.get("sheet_tab") or "listings",
    )

    secrets = Secrets(
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
    )

    return Config(
        target_url=target_url,
        rate_limit_seconds=float(raw.get("rate_limit_seconds", 2)),
        max_pages=int(raw.get("max_pages", 1)),
        dedupe_key=dedupe_key,
        fields=fields,
        row_selector=raw.get("row_selector") or None,
        pagination=pagination,
        output=output,
        user_agent=raw.get("user_agent") or "config-scraper/1.0",
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 10)),
        max_retries=int(raw.get("max_retries", 3)),
        jitter_min_seconds=float(raw.get("jitter_min_seconds", 0.5)),
        jitter_max_seconds=float(raw.get("jitter_max_seconds", 1.5)),
        secrets=secrets,
    )
