"""Configuration loading (config.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "config.toml"


@dataclass
class CollectorConfig:
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    coins: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "HYPE"])
    ping_interval_s: float = 30.0
    # Reconnect if nothing (not even a pong) arrived for this long.
    stale_timeout_s: float = 90.0
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    # Fetch recentTrades via REST after each (re)connect to narrow gaps.
    backfill_on_connect: bool = True
    flush_interval_s: float = 0.25
    flush_max_rows: int = 2000


@dataclass
class AggregationConfig:
    window_ms: int = 500
    absorption_enabled: bool = True
    # A deep trade is an absorption candidate from this notional (USD) upward.
    absorption_min_notional: float = 250_000.0
    absorption_min_notional_per_coin: dict[str, float] = field(default_factory=dict)
    # Look at the price this many seconds after the deep trade ended.
    absorption_horizon_s: float = 10.0
    # Price may exceed the deep trade's extreme by at most this (basis points).
    absorption_tolerance_bps: float = 0.0

    def absorption_threshold(self, coin: str) -> float:
        return self.absorption_min_notional_per_coin.get(coin, self.absorption_min_notional)


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    info_url: str = "https://api.hyperliquid.xyz/info"
    max_range_hours: float = 72.0
    live_poll_interval_s: float = 0.25
    cors_origins: list[str] = field(default_factory=list)
    serve_frontend: bool = True


@dataclass
class Config:
    db_path: str = "data/deeptrades.sqlite"
    retention_days: float = 30.0
    log_level: str = "INFO"
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    aggregation: AggregationConfig = field(default_factory=AggregationConfig)
    api: ApiConfig = field(default_factory=ApiConfig)


def _apply(dc, values: dict, section: str):
    for key, val in values.items():
        if not hasattr(dc, key):
            raise ValueError(f"Unknown config key [{section}] {key}")
        setattr(dc, key, val)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config from `path`, $DEEPTRADES_CONFIG or ./config.toml (defaults if missing)."""
    path = Path(path or os.environ.get("DEEPTRADES_CONFIG", DEFAULT_CONFIG_PATH))
    cfg = Config()
    if not path.exists():
        return cfg
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    sub = {"collector": cfg.collector, "aggregation": cfg.aggregation, "api": cfg.api}
    for key, val in raw.items():
        if key in sub:
            _apply(sub[key], val, key)
        elif hasattr(cfg, key):
            setattr(cfg, key, val)
        else:
            raise ValueError(f"Unknown config key {key}")
    cfg.collector.coins = [c.strip() for c in cfg.collector.coins if c.strip()]
    return cfg
