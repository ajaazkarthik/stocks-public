"""
Configuration loaded from .env file.
Copy .env.example to .env and fill in your values before running.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── IBKR connection ────────────────────────────────────────────────────────
    # TWS Paper:  7497  |  TWS Live:  7496
    # Gateway Paper: 4001 | Gateway Live: 4002
    IB_HOST: str = "127.0.0.1"
    IB_PORT: int = 7497          # default = TWS paper trading
    IB_CLIENT_ID: int = 1

    # ── Webhook security ───────────────────────────────────────────────────────
    # Embed this token in your TradingView webhook URL:
    #   http://yourserver/webhook?token=YOUR_SECRET
    WEBHOOK_SECRET: Optional[str] = None

    # ── Market defaults (ASX) ─────────────────────────────────────────────────
    EXCHANGE: str = "ASX"
    CURRENCY: str = "AUD"
    DEFAULT_QUANTITY: float = 100
    MAX_QUANTITY: float = 10_000

    # Optional comma-separated symbol whitelist, e.g. "BHP,CBA,NAB"
    # Leave empty to allow any symbol.
    ALLOWED_SYMBOLS: Optional[str] = None

    # ── Log level ─────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    @property
    def allowed_symbols_set(self) -> Optional[set]:
        if not self.ALLOWED_SYMBOLS:
            return None
        return {s.strip().upper() for s in self.ALLOWED_SYMBOLS.split(",")}


settings = Settings()
