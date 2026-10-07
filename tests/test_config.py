"""Config validation — every one of these is a fail-closed guarantee."""

from decimal import Decimal

import pytest

from bot.config import (
    LIVE_ACK_PHRASE,
    V1_GOLD_PIN_ACTIVE,
    ConfigError,
    collect_secrets,
    load_config,
)
from bot.models import CanonicalAsset, TradingMode

BASE = {
    "TRADING_MODE": "paper",
    "TRADE_MARGIN_USD": "30",
    "MAX_LEVERAGE": "10",
    "DEFAULT_LEVERAGE": "10",
}


def cfg(**overrides):
    return load_config({**BASE, **overrides}, dotenv_path=None)


# -- trading mode ----------------------------------------------------------


def test_trading_mode_is_required():
    with pytest.raises(ConfigError, match="TRADING_MODE is required"):
        load_config({}, dotenv_path=None)


def test_invalid_trading_mode_rejected():
    with pytest.raises(ConfigError, match="invalid"):
        cfg(TRADING_MODE="production")


def test_live_requires_second_acknowledgement():
    """A single forgotten variable must never be enough to trade real money."""
    with pytest.raises(ConfigError, match="LIVE_TRADING_ACK"):
        cfg(TRADING_MODE="live")


def test_live_rejects_wrong_acknowledgement():
    with pytest.raises(ConfigError, match="LIVE_TRADING_ACK"):
        cfg(TRADING_MODE="live", LIVE_TRADING_ACK="yes")


def test_live_accepted_with_exact_phrase():
    c = cfg(TRADING_MODE="live", LIVE_TRADING_ACK=LIVE_ACK_PHRASE)
    assert c.trading_mode is TradingMode.LIVE
    assert c.trading_mode.is_live


def test_paper_needs_no_acknowledgement():
    assert cfg(TRADING_MODE="paper").trading_mode is TradingMode.PAPER


def test_dry_run_does_not_touch_exchange():
    assert cfg(TRADING_MODE="dry_run").trading_mode.touches_exchange is False
    assert cfg(TRADING_MODE="paper").trading_mode.touches_exchange is True


# -- leverage --------------------------------------------------------------


def test_max_leverage_cannot_exceed_hard_cap():
    with pytest.raises(ConfigError, match="hard internal cap"):
        cfg(MAX_LEVERAGE="20")


def test_leverage_is_fixed_at_ten():
    assert cfg().fixed_leverage == Decimal(10)


def test_max_leverage_below_the_fixed_leverage_is_fatal():
    """The cap would refuse every signal; better to refuse to start."""
    with pytest.raises(ConfigError, match="below the fixed 10x"):
        cfg(MAX_LEVERAGE="5")


def test_retired_leverage_knobs_are_ignored_not_honoured():
    """DEFAULT_LEVERAGE / MIN_LEVERAGE existed before leverage was fixed. A .env
    that still sets them must not change anything (or crash)."""
    c = cfg(DEFAULT_LEVERAGE="3", MIN_LEVERAGE="2")
    assert c.fixed_leverage == Decimal(10)
    assert not hasattr(c, "default_leverage") and not hasattr(c, "min_leverage")


# -- numeric validation ----------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-5"])
def test_margin_must_be_positive(value):
    with pytest.raises(ConfigError, match="TRADE_MARGIN_USD"):
        cfg(TRADE_MARGIN_USD=value)


def test_garbage_numeric_is_fatal_not_defaulted():
    with pytest.raises(ConfigError, match="not a valid number"):
        cfg(TRADE_MARGIN_USD="thirty")


def test_nan_margin_rejected():
    with pytest.raises(ConfigError, match="finite"):
        cfg(TRADE_MARGIN_USD="NaN")


def test_bad_boolean_is_fatal():
    with pytest.raises(ConfigError, match="boolean"):
        cfg(ALLOW_XAUT_FOR_GOLD="maybe")


def test_bad_log_level_rejected():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        cfg(LOG_LEVEL="LOUD")


def test_zero_reconciliation_interval_rejected():
    with pytest.raises(ConfigError, match="RECONCILIATION_INTERVAL"):
        cfg(RECONCILIATION_INTERVAL_SECONDS="0")


# -- entry allocation ------------------------------------------------------


def test_entry_allocation_is_half_the_margin():
    """Brief 11: $30 margin -> $15 per entry order."""
    assert cfg(TRADE_MARGIN_USD="30").entry_allocation_usd == Decimal("15")


# -- gold pin --------------------------------------------------------------


def test_gold_is_off_by_default():
    c = cfg()
    assert c.allow_xaut_for_gold is False
    assert not c.is_tradeable(CanonicalAsset.XAU)


def test_the_gold_pin_mechanism_still_overrides_the_env_flag(monkeypatch):
    """The pin was LIFTED 2026-07-31 by operator decision (KRAKEN_NOTES 1B.1),
    so this no longer asserts the pin is on. It asserts the pin still WORKS,
    because re-pinning is the documented way to switch gold back off."""
    monkeypatch.setattr("bot.config.V1_GOLD_PIN_ACTIVE", True)
    c = cfg(ALLOW_XAUT_FOR_GOLD="true")
    assert c.allow_xaut_for_gold is False
    assert c.gold_pin_overridden_request is True
    assert not c.is_tradeable(CanonicalAsset.XAU)
    banner = "\n".join(c.startup_banner())
    assert "GOLD/XAU: DISABLED" in banner
    assert "pin overrides it" in banner


def test_gold_pin_is_currently_lifted():
    """Records the current state explicitly, so flipping it back is a visible
    test change rather than a silent one."""
    assert V1_GOLD_PIN_ACTIVE is False


def test_btc_eth_sol_always_tradeable():
    c = cfg()
    for asset in (CanonicalAsset.BTC, CanonicalAsset.ETH, CanonicalAsset.SOL):
        assert c.is_tradeable(asset)


# -- banner ----------------------------------------------------------------


def test_banner_always_states_the_mode():
    assert cfg(TRADING_MODE="paper").startup_banner()[0] == "TRADING MODE: PAPER"
    live = cfg(TRADING_MODE="live", LIVE_TRADING_ACK=LIVE_ACK_PHRASE)
    assert live.startup_banner()[0] == "TRADING MODE: LIVE"


# -- secrets ---------------------------------------------------------------


def test_secrets_are_collected_from_env_not_config():
    secrets = collect_secrets(
        {"KRAKEN_API_SECRET": "s" * 40, "TELEGRAM_API_HASH": "h" * 32}
    )
    assert "s" * 40 in secrets
    assert "h" * 32 in secrets


def test_short_values_are_not_treated_as_secrets():
    """Avoids masking innocuous strings that merely share a name."""
    assert collect_secrets({"KRAKEN_API_KEY": "abc"}) == set()


def test_config_repr_contains_no_credentials():
    c = cfg(KRAKEN_API_SECRET="s" * 40)
    assert "s" * 40 not in repr(c)


# -- telegram channel identity ---------------------------------------------
#
# Regression: a numeric channel id read as a STRING made the bot silently
# deaf. Telethon resolves a digits-only string as a username, which fails
# even when the channel is cached, so events.NewMessage(chats=...) matched
# nothing while the bot logged "running; watching for signals".


def test_numeric_channel_id_is_an_int_not_a_string():
    c = cfg(TELEGRAM_CHANNEL="1000000001")
    assert c.telegram_channel == 1000000001
    assert isinstance(c.telegram_channel, int), (
        "Telethon cannot resolve a digits-only string; it must be an int"
    )


def test_marked_channel_id_keeps_its_negative_prefix():
    c = cfg(TELEGRAM_CHANNEL="-1001000000001")
    assert c.telegram_channel == -1001000000001
    assert isinstance(c.telegram_channel, int)


def test_username_channel_stays_a_string():
    c = cfg(TELEGRAM_CHANNEL="@example_channel")
    assert c.telegram_channel == "@example_channel"
    assert isinstance(c.telegram_channel, str)


def test_channel_whitespace_is_stripped():
    assert cfg(TELEGRAM_CHANNEL="  1000000001  ").telegram_channel == 1000000001


def test_blank_channel_is_none_not_empty_string():
    assert cfg(TELEGRAM_CHANNEL="").telegram_channel is None


# -- Gold override (operator decision 2026-07-31) ---------------------------


def test_gold_pin_is_lifted_but_still_requires_the_env_flag():
    """Lifting the pin re-arms ALLOW_XAUT_FOR_GOLD; it must not enable gold
    on its own."""
    from bot.models import CanonicalAsset
    off = cfg(ALLOW_XAUT_FOR_GOLD="false")
    assert off.is_tradeable(CanonicalAsset.XAU) is False
    on = cfg(ALLOW_XAUT_FOR_GOLD="true")
    assert on.is_tradeable(CanonicalAsset.XAU) is True


def test_enabled_gold_discloses_the_substitution_at_startup():
    banner = "\n".join(cfg(ALLOW_XAUT_FOR_GOLD="true").startup_banner())
    assert "PF_XAUTUSD" in banner
    assert "NOT spot XAU" in banner
