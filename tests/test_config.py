import pytest

from core.config import Settings, TradingMode


def test_defaults_to_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    assert Settings.from_env().trading_mode is TradingMode.PAPER
    assert not Settings.from_env().live_enabled


def test_live_mode_requires_independent_enable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    with pytest.raises(ValueError, match="ENABLE_LIVE_TRADING"):
        Settings.from_env()
