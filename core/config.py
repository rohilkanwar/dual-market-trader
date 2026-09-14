"""Environment-driven settings. Paper is the default and the only armed mode.

Live trading requires *all* of the following, otherwise ``Settings.from_env``
raises and nothing starts:

* ``TRADING_MODE=live``
* ``ENABLE_LIVE_TRADING=true`` (independent second flag)
* explicit ``MAX_NOTIONAL_PER_ORDER``, ``MAX_POSITION_PER_MARKET`` and
  ``MAX_DAILY_LOSS`` values in the environment (defaults are not accepted for
  live)

Even when all three hold, venue adapters in this repository do not implement
live order routing; ``place_order`` on a non-paper client raises. See
``docs/ASSUMPTIONS.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from core.risk import RiskLimits

CAP_ENV_VARS = ("MAX_NOTIONAL_PER_ORDER", "MAX_POSITION_PER_MARKET", "MAX_DAILY_LOSS")
DEFAULT_CAPS = {
    "MAX_NOTIONAL_PER_ORDER": Decimal("100"),
    "MAX_POSITION_PER_MARKET": Decimal("500"),
    "MAX_DAILY_LOSS": Decimal("250"),
}


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _decimal_env(name: str, default: Decimal) -> Decimal:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{name}={value!r} is not a valid decimal") from exc


def live_environment_requested() -> bool:
    """Return whether process environment requests any live-trading path."""
    return (
        os.getenv("TRADING_MODE", "paper").strip().lower() == "live"
        or _bool_env("ENABLE_LIVE_TRADING")
    )


def require_paper_only(context: str) -> None:
    """Fail closed when a paper-only process inherits live settings."""
    if live_environment_requested():
        raise ValueError(
            f"{context} is paper-only; refusing TRADING_MODE=live or "
            "ENABLE_LIVE_TRADING=true"
        )


@dataclass(frozen=True, slots=True)
class Settings:
    trading_mode: TradingMode = TradingMode.PAPER
    enable_live_trading: bool = False
    max_notional_per_order: Decimal = DEFAULT_CAPS["MAX_NOTIONAL_PER_ORDER"]
    max_position_per_market: Decimal = DEFAULT_CAPS["MAX_POSITION_PER_MARKET"]
    max_daily_loss: Decimal = DEFAULT_CAPS["MAX_DAILY_LOSS"]
    kill_switch: bool = False
    log_level: str = "INFO"
    kalshi_env: str = "demo"
    kalshi_access_key_id: str = ""
    kalshi_private_key_path: str = ""
    polymarket_funder_address: str = ""
    polymarket_signature_type: int = 0

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode is TradingMode.LIVE and self.enable_live_trading

    @property
    def risk_limits(self) -> RiskLimits:
        return RiskLimits(
            max_notional_per_order=self.max_notional_per_order,
            max_position_per_market=self.max_position_per_market,
            max_daily_loss=self.max_daily_loss,
        )

    @classmethod
    def from_env(cls) -> Settings:
        raw_mode = os.getenv("TRADING_MODE", "paper").strip().lower() or "paper"
        try:
            trading_mode = TradingMode(raw_mode)
        except ValueError as exc:
            raise ValueError(f"TRADING_MODE={raw_mode!r} must be 'paper' or 'live'") from exc
        enable_live = _bool_env("ENABLE_LIVE_TRADING")

        if trading_mode is TradingMode.LIVE and not enable_live:
            raise ValueError(
                "TRADING_MODE=live requires the independent ENABLE_LIVE_TRADING=true flag"
            )
        if trading_mode is TradingMode.PAPER and enable_live:
            raise ValueError(
                "ENABLE_LIVE_TRADING=true is inconsistent with TRADING_MODE=paper; "
                "refusing to start with ambiguous live settings"
            )
        if trading_mode is TradingMode.LIVE:
            missing = [name for name in CAP_ENV_VARS if not (os.getenv(name) or "").strip()]
            if missing:
                raise ValueError(
                    "live mode requires explicit capital caps; missing "
                    + ", ".join(missing)
                )

        caps = {name: _decimal_env(name, DEFAULT_CAPS[name]) for name in CAP_ENV_VARS}
        settings = cls(
            trading_mode=trading_mode,
            enable_live_trading=enable_live,
            max_notional_per_order=caps["MAX_NOTIONAL_PER_ORDER"],
            max_position_per_market=caps["MAX_POSITION_PER_MARKET"],
            max_daily_loss=caps["MAX_DAILY_LOSS"],
            kill_switch=_bool_env("KILL_SWITCH"),
            log_level=(os.getenv("LOG_LEVEL") or "INFO").strip() or "INFO",
            kalshi_env=(os.getenv("KALSHI_ENV") or "demo").strip().lower() or "demo",
            kalshi_access_key_id=(os.getenv("KALSHI_ACCESS_KEY_ID") or "").strip(),
            kalshi_private_key_path=(os.getenv("KALSHI_PRIVATE_KEY_PATH") or "").strip(),
            polymarket_funder_address=(os.getenv("POLYMARKET_FUNDER_ADDRESS") or "").strip(),
            polymarket_signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE") or "0"),
        )
        settings.risk_limits  # validates caps are positive
        return settings
