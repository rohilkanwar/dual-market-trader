    return value.strip().lower() in {"1", "true", "yes", "on"}


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
