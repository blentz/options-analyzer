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
