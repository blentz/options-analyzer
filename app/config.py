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
    stocknear_browser_profile_path: str = ""  # Path to LibreWolf/Firefox profile with cookies
    stocknear_headless: bool = True
    stocknear_rate_limit_delay: float = 1.0  # Seconds between requests
    stocknear_cache_ttl_seconds: int = 3600  # 1 hour cache
    
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
    # Debug endpoints (raw scraped content, screenshots, etc) leak internals
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
