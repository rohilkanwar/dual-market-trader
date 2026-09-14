from decimal import Decimal

import pytest

from core.config import Settings, TradingMode, live_environment_requested, require_paper_only


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRADING_MODE",
        "ENABLE_LIVE_TRADING",
        "MAX_NOTIONAL_PER_ORDER",
        "MAX_POSITION_PER_MARKET",
        "MAX_DAILY_LOSS",
        "KILL_SWITCH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_paper() -> None:
    assert Settings.from_env().trading_mode is TradingMode.PAPER
    assert not Settings.from_env().live_enabled
    assert not live_environment_requested()
    require_paper_only("test")  # does not raise


def test_live_mode_requires_independent_enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    with pytest.raises(ValueError, match="ENABLE_LIVE_TRADING"):
        Settings.from_env()


def test_enable_flag_without_live_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(ValueError, match="inconsistent"):
        Settings.from_env()
    assert live_environment_requested()
    with pytest.raises(ValueError, match="paper-only"):
        require_paper_only("test")


def test_live_mode_requires_explicit_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(ValueError, match="MAX_NOTIONAL_PER_ORDER, MAX_POSITION_PER_MARKET, MAX_DAILY_LOSS"):
        Settings.from_env()

    monkeypatch.setenv("MAX_NOTIONAL_PER_ORDER", "10")
    monkeypatch.setenv("MAX_POSITION_PER_MARKET", "20")
    with pytest.raises(ValueError, match="MAX_DAILY_LOSS"):
        Settings.from_env()

    monkeypatch.setenv("MAX_DAILY_LOSS", "5")
    settings = Settings.from_env()
    assert settings.live_enabled
    assert settings.risk_limits.max_daily_loss == Decimal("5")


def test_invalid_mode_and_caps_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "yolo")
    with pytest.raises(ValueError, match="TRADING_MODE"):
        Settings.from_env()
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("MAX_DAILY_LOSS", "-1")
    with pytest.raises(ValueError, match="max_daily_loss"):
        Settings.from_env()
    monkeypatch.setenv("MAX_DAILY_LOSS", "abc")
    with pytest.raises(ValueError, match="valid decimal"):
        Settings.from_env()
