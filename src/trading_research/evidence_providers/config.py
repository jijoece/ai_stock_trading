"""Loads `config/evidence_providers.yaml` (docs/milestone-6.md Step 20).
Mirrors `research/configuration.py::load_research_config`'s pattern exactly:
fail closed on anything malformed or unrecognized, defaults to every
provider disabled except SEC (which needs no credential), and never reads an
environment variable to decide `enabled` — `.env` only ever supplies a
credential, never a capability decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..hashing import hash_config

DEFAULT_EVIDENCE_PROVIDERS_CONFIG_PATH = REPO_ROOT / "config" / "evidence_providers.yaml"

KNOWN_MARKET_DATA_PROVIDERS = ("alpaca",)
KNOWN_NEWS_PROVIDERS = ("alpaca_news",)


class EvidenceProviderConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SecProviderConfig:
    enabled: bool
    user_agent_contact: str
    request_timeout_seconds: int
    max_attempts: int
    min_request_interval_seconds: float


@dataclass(frozen=True)
class MarketDataProviderConfig:
    enabled: bool
    provider: str | None
    request_timeout_seconds: int
    max_attempts: int
    min_request_interval_seconds: float


@dataclass(frozen=True)
class NewsProviderConfig:
    enabled: bool
    provider: str | None
    request_timeout_seconds: int
    max_attempts: int


@dataclass(frozen=True)
class SentimentProviderConfig:
    enabled: bool


@dataclass(frozen=True)
class RedditFreeProviderConfig:
    enabled: bool
    provider_class: str
    cache_ttl_minutes: int
    user_agent: str
    subreddits: tuple[str, ...]
    max_posts_per_symbol: int
    request_timeout_seconds: int
    max_attempts: int
    min_request_interval_seconds: float
    max_requests_per_endpoint_hour: int


@dataclass(frozen=True)
class EvidenceProviderConfiguration:
    version: int
    sec: SecProviderConfig
    market_data: MarketDataProviderConfig
    news: NewsProviderConfig
    sentiment: SentimentProviderConfig
    reddit_free: RedditFreeProviderConfig
    config_hash: str
    raw: dict


def load_evidence_provider_config(path: str | Path | None = None) -> EvidenceProviderConfiguration:
    config_path = Path(path) if path else DEFAULT_EVIDENCE_PROVIDERS_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise EvidenceProviderConfigError(f"cannot read evidence-provider config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise EvidenceProviderConfigError(f"invalid YAML in evidence-provider config at {config_path}: {exc}") from exc

    providers = raw.get("providers")
    if not isinstance(providers, dict):
        raise EvidenceProviderConfigError("evidence-provider config missing top-level 'providers' section")

    required_provider_keys = {"sec", "market_data", "news", "sentiment", "reddit_free"}
    missing = required_provider_keys - providers.keys()
    if missing:
        raise EvidenceProviderConfigError(f"evidence-provider config missing provider sections: {sorted(missing)}")

    sec_raw = providers["sec"]
    md_raw = providers["market_data"]
    news_raw = providers["news"]
    sentiment_raw = providers["sentiment"]
    reddit_free_raw = providers["reddit_free"]

    expected_reddit_free_class = "trading_research.evidence_providers.reddit_free.RedditFreeProvider"
    if reddit_free_raw.get("provider_class") != expected_reddit_free_class:
        raise EvidenceProviderConfigError(
            f"providers.reddit_free.provider_class must be {expected_reddit_free_class!r}"
        )
    subreddits = reddit_free_raw.get("subreddits")
    if not isinstance(subreddits, list) or not subreddits or not all(
        isinstance(value, str) and value.strip() for value in subreddits
    ):
        raise EvidenceProviderConfigError("providers.reddit_free.subreddits must be a non-empty list of names")
    if float(reddit_free_raw.get("min_request_interval_seconds", 0)) < 2.0:
        raise EvidenceProviderConfigError("providers.reddit_free.min_request_interval_seconds must be at least 2")
    max_per_hour = int(reddit_free_raw.get("max_requests_per_endpoint_hour", 0))
    if not 1 <= max_per_hour <= 30:
        raise EvidenceProviderConfigError("providers.reddit_free.max_requests_per_endpoint_hour must be in [1, 30]")
    max_attempts = int(reddit_free_raw.get("max_attempts", 0))
    if not 1 <= max_attempts <= 3:
        raise EvidenceProviderConfigError("providers.reddit_free.max_attempts must be in [1, 3]")

    if md_raw.get("provider") is not None and md_raw["provider"] not in KNOWN_MARKET_DATA_PROVIDERS:
        raise EvidenceProviderConfigError(
            f"providers.market_data.provider {md_raw['provider']!r} is not one of {KNOWN_MARKET_DATA_PROVIDERS} — fails closed"
        )
    if bool(md_raw.get("enabled")) and md_raw.get("provider") is None:
        raise EvidenceProviderConfigError("providers.market_data.enabled=true requires an explicit provider name")

    if news_raw.get("provider") is not None and news_raw["provider"] not in KNOWN_NEWS_PROVIDERS:
        raise EvidenceProviderConfigError(
            f"providers.news.provider {news_raw['provider']!r} is not one of {KNOWN_NEWS_PROVIDERS} — fails closed"
        )
    if bool(news_raw.get("enabled")) and news_raw.get("provider") is None:
        raise EvidenceProviderConfigError("providers.news.enabled=true requires an explicit provider name")

    return EvidenceProviderConfiguration(
        version=raw.get("version", 1),
        sec=SecProviderConfig(
            enabled=bool(sec_raw["enabled"]), user_agent_contact=str(sec_raw["user_agent_contact"]),
            request_timeout_seconds=int(sec_raw["request_timeout_seconds"]), max_attempts=int(sec_raw["max_attempts"]),
            min_request_interval_seconds=float(sec_raw["min_request_interval_seconds"]),
        ),
        market_data=MarketDataProviderConfig(
            enabled=bool(md_raw["enabled"]), provider=md_raw.get("provider"),
            request_timeout_seconds=int(md_raw["request_timeout_seconds"]), max_attempts=int(md_raw["max_attempts"]),
            min_request_interval_seconds=float(md_raw["min_request_interval_seconds"]),
        ),
        news=NewsProviderConfig(
            enabled=bool(news_raw["enabled"]), provider=news_raw.get("provider"),
            request_timeout_seconds=int(news_raw["request_timeout_seconds"]), max_attempts=int(news_raw["max_attempts"]),
        ),
        sentiment=SentimentProviderConfig(enabled=bool(sentiment_raw["enabled"])),
        reddit_free=RedditFreeProviderConfig(
            enabled=bool(reddit_free_raw["enabled"]),
            provider_class=str(reddit_free_raw["provider_class"]),
            cache_ttl_minutes=int(reddit_free_raw["cache_ttl_minutes"]),
            user_agent=str(reddit_free_raw["user_agent"]),
            subreddits=tuple(value.strip() for value in subreddits),
            max_posts_per_symbol=int(reddit_free_raw["max_posts_per_symbol"]),
            request_timeout_seconds=int(reddit_free_raw["request_timeout_seconds"]),
            max_attempts=max_attempts,
            min_request_interval_seconds=float(reddit_free_raw["min_request_interval_seconds"]),
            max_requests_per_endpoint_hour=max_per_hour,
        ),
        config_hash=hash_config(raw), raw=raw,
    )
