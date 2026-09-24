"""Application configuration using pydantic-settings."""

import os
from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    # Database
    database_path: str = "/app/data/options.db"
    
    # StockNear configuration
    stocknear_base_url: str = "https://stocknear.com"
    stocknear_browser_profile_path: str = ""  # Optional: Firefox/LibreWolf profile whose cookies are sent to StockNear
    stocknear_cache_ttl_seconds: int = 3600  # 1 hour cache

    # Contract-history sync. The source updates once per trading day; 12
    # hours fetches each day's data about once without needing a market
    # calendar. Sync is user-triggered, so this only suppresses redundant
    # re-downloads within a session of clicking.
    stocknear_history_ttl_seconds: int = 43200

    # StockNear MCP server. Symbol-level data (options overview, max pain,
    # stock quote, expirations) comes from here rather than the scraper.
    # The token is a credential — set it in .env, never commit it.
    stocknear_mcp_url: str = "https://mcp.stocknear.com/mcp"
    stocknear_mcp_token: str = ""
    stocknear_mcp_timeout: int = 30
    # Max simultaneous in-flight MCP requests. StockNear publishes no rate
    # limit, so this is a politeness bound as much as a protective one.
    stocknear_mcp_max_concurrency: int = 4
    
    # Price service
    price_cache_ttl_seconds: int = 60  # 1 minute cache for stock prices

    # Risk math defaults. These drive every Black-Scholes-based calculation
    # (assignment probability, BS option price for theta charts, exit
    # scenario bands, etc). Override via env if your rate/vol assumptions
    # differ — the 5% / 30% defaults are reasonable for SPY-like names but
    # wrong for high-IV individual stocks and any non-near-zero-rate regime.
    risk_free_rate: float = 0.05
    default_volatility: float = 0.30

    # Operational hardening
    # Max CSV upload size in MB. Fidelity exports are typically <2MB; the cap
    # exists to keep a malicious or accidental multi-GB upload from OOM'ing
    # the container.
    max_upload_mb: int = 25
    # API key required on all routes when set. Leave empty to disable auth
    # (only safe when bound to localhost on a trusted machine).
    api_key: str = ""
    # Debug endpoints (raw StockNear payloads, uncached quotes) leak internals
    # and session data. Off by default — set to true only for local dev.
    enable_debug_endpoints: bool = False
    # Logging format: "text" (default, human-readable) or "json" (one JSON
    # object per line for log aggregators).
    log_format: str = "text"
    
    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"
    
    @property
    def database_path_obj(self) -> Path:
        return Path(self.database_path)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Convenience accessor
settings = get_settings()
