"""Configuration loading and startup validation.

Every value is validated at load time and the process refuses to start on
anything questionable. There is no "sensible fallback" for a risk parameter —
brief 40 requires failing closed, so a malformed TRADE_MARGIN_USD is a startup
error, not a silent default.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

from .models import CanonicalAsset, TradingMode

# ---------------------------------------------------------------------------
# v1 pin: GOLD/XAU is implemented but must not trade.
#
# Kraken has no spot-gold perpetual. The only candidate is PF_XAUTUSD (Tether
# Gold), and the signal channel quotes entry/TP/SL against SPOT gold. Those two
# series can diverge under ordinary thin-liquidity conditions (~711 OI), which
# would corrupt the stale-entry guard and put stops at the wrong effective
# distance. Gating the flag does not fix a units mismatch — it has to be
# measured first (docs/PLAN.md 4.4.1, Stage 4 Study A).
#
# Flipping this to False is a reviewed code change, not a config edit, and is
# permitted only once Study A passes AND BTC/ETH/SOL have run cleanly.
# ---------------------------------------------------------------------------
V1_GOLD_PIN_ACTIVE = True

LIVE_ACK_PHRASE = "I_UNDERSTAND_THIS_IS_REAL_MONEY"

# A channel id, with or without Telegram's -100 supergroup/channel prefix.
_NUMERIC_CHANNEL = re.compile(r"-?\d+")

# Assets tradeable in v1. XAU is deliberately absent while the pin holds.
V1_TRADEABLE_ASSETS = frozenset({CanonicalAsset.BTC, CanonicalAsset.ETH, CanonicalAsset.SOL})


class ConfigError(Exception):
    """Configuration is unusable. Always fatal — never caught and defaulted."""


@dataclass(frozen=True, slots=True)
class Config:
    trading_mode: TradingMode
    trade_margin_usd: Decimal
    max_leverage: Decimal
    default_leverage: Decimal
    max_entry_deviation_percent: Decimal
    max_sizing_deviation_percent: Decimal
    entry_order_expiry_hours: int
    reconciliation_interval_seconds: int

    telegram_api_id: int | None
    telegram_api_hash: str | None
    # int for a numeric channel id, str for an @username. The distinction is
    # load-bearing, not cosmetic — see _parse_channel.
    telegram_channel: str | int | None
    telegram_session_path: Path

    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None

    kill_switch_path: Path
    db_path: Path
    log_path: Path
    log_level: str
    kraken_cli_path: str
    kraken_cli_timeout_seconds: int

    allow_xaut_for_gold: bool
    gold_pin_active: bool
    gold_pin_overridden_request: bool

    tradeable_assets: frozenset[CanonicalAsset] = field(default=V1_TRADEABLE_ASSETS)

    # ---- derived helpers -------------------------------------------------

    @property
    def entry_allocation_usd(self) -> Decimal:
        """Margin per entry order — two entries, 50/50 (brief 11)."""
        return self.trade_margin_usd / Decimal(2)

    def is_tradeable(self, asset: CanonicalAsset) -> bool:
        return asset in self.tradeable_assets

    def startup_banner(self) -> list[str]:
        """Lines emitted at every startup. Mode is always visible (brief 26)."""
        lines = [
            f"TRADING MODE: {self.trading_mode.value.upper()}",
            f"margin=${self.trade_margin_usd} max_leverage={self.max_leverage}x "
            f"default_leverage={self.default_leverage}x",
            f"tradeable: {', '.join(sorted(a.value for a in self.tradeable_assets))}",
            f"kill_switch={self.kill_switch_path} db={self.db_path}",
        ]
        if self.gold_pin_active:
            reason = (
                "operator set ALLOW_XAUT_FOR_GOLD=true but the v1 pin overrides it"
                if self.gold_pin_overridden_request
                else "not enabled"
            )
            lines.append(
                f"GOLD/XAU: DISABLED ({reason}) — no spot-gold perpetual exists on "
                "Kraken; PF_XAUTUSD substitution is pinned off pending the Stage 4 "
                "tracking study (docs/PLAN.md 4.4)"
            )
        elif self.allow_xaut_for_gold:
            lines.append(
                "GOLD/XAU -> PF_XAUTUSD (Tether Gold, NOT spot XAU) [operator-enabled]"
            )
        return lines


# ---------------------------------------------------------------------------
# primitive readers
# ---------------------------------------------------------------------------


def _raw(env: dict[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require(env: dict[str, str], key: str) -> str:
    value = _raw(env, key)
    if value is None:
        raise ConfigError(f"{key} is required and must not be empty")
    return value


def _decimal(env: dict[str, str], key: str, default: str | None = None) -> Decimal:
    raw = _raw(env, key)
    if raw is None:
        if default is None:
            raise ConfigError(f"{key} is required")
        raw = default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{key}={raw!r} is not a valid number") from exc
    if not value.is_finite():
        raise ConfigError(f"{key}={raw!r} must be finite")
    return value


def _int(env: dict[str, str], key: str, default: str | None = None) -> int:
    raw = _raw(env, key)
    if raw is None:
        if default is None:
            raise ConfigError(f"{key} is required")
        raw = default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not a valid integer") from exc


def _parse_channel(raw: str | None) -> str | int | None:
    """Coerce a numeric TELEGRAM_CHANNEL to int; leave @usernames as str.

    This is not cosmetic. Telethon resolves a digits-only STRING as a
    username via ResolveUsernameRequest, which fails with

        ValueError: Cannot find any entity corresponding to "1000000001"

    even when the channel is correctly cached in the session. The same id
    passed as an INT resolves fine from cache. Measured against the live API:
    "1000000001" and "-1001000000001" both fail; 1000000001 and
    -1001000000001 both work, so the -100 prefix is irrelevant and the type
    is everything.

    The failure mode this prevents is worse than the backfill crash that
    exposed it: `events.NewMessage(chats=...)` given an unresolvable value
    matches nothing, so the bot logs "running; watching for signals" and is
    silently deaf.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if _NUMERIC_CHANNEL.fullmatch(value):
        return int(value)
    return value


def _bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    raw = _raw(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}={raw!r} must be a boolean (true/false)")


def _positive(name: str, value: Decimal) -> Decimal:
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {value}")
    return value


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_config(
    env: dict[str, str] | None = None, *, dotenv_path: str | Path | None = ".env"
) -> Config:
    """Build and validate a Config.

    `env` is injectable so tests never touch the real process environment.
    """
    if env is None:
        if dotenv_path is not None and Path(dotenv_path).exists():
            load_dotenv(dotenv_path, override=False)
        env = dict(os.environ)

    # -- trading mode: required, explicit, and live needs a second key -------
    mode_raw = _require(env, "TRADING_MODE").lower()
    try:
        trading_mode = TradingMode(mode_raw)
    except ValueError:
        valid = ", ".join(m.value for m in TradingMode)
        raise ConfigError(
            f"TRADING_MODE={mode_raw!r} is invalid; expected one of: {valid}"
        ) from None

    if trading_mode.is_live:
        ack = _raw(env, "LIVE_TRADING_ACK")
        if ack != LIVE_ACK_PHRASE:
            raise ConfigError(
                "TRADING_MODE=live requires LIVE_TRADING_ACK to be set to exactly "
                f"{LIVE_ACK_PHRASE!r}. Refusing to start: a single forgotten "
                "variable must never be enough to send real orders."
            )

    # -- risk parameters ----------------------------------------------------
    trade_margin_usd = _positive(
        "TRADE_MARGIN_USD", _decimal(env, "TRADE_MARGIN_USD", "30")
    )
    max_leverage = _positive("MAX_LEVERAGE", _decimal(env, "MAX_LEVERAGE", "10"))
    if max_leverage > 10:
        raise ConfigError(
            f"MAX_LEVERAGE={max_leverage} exceeds the hard internal cap of 10x "
            "(brief 9). This limit is not configurable upward."
        )

    default_leverage = _positive(
        "DEFAULT_LEVERAGE", _decimal(env, "DEFAULT_LEVERAGE", "10")
    )
    if default_leverage > max_leverage:
        raise ConfigError(
            f"DEFAULT_LEVERAGE={default_leverage} exceeds MAX_LEVERAGE={max_leverage}"
        )

    max_entry_deviation_percent = _positive(
        "MAX_ENTRY_DEVIATION_PERCENT",
        _decimal(env, "MAX_ENTRY_DEVIATION_PERCENT", "1.5"),
    )
    max_sizing_deviation_percent = _positive(
        "MAX_SIZING_DEVIATION_PERCENT",
        _decimal(env, "MAX_SIZING_DEVIATION_PERCENT", "5.0"),
    )

    entry_order_expiry_hours = _int(env, "ENTRY_ORDER_EXPIRY_HOURS", "24")
    if entry_order_expiry_hours <= 0:
        raise ConfigError("ENTRY_ORDER_EXPIRY_HOURS must be > 0")

    reconciliation_interval_seconds = _int(
        env, "RECONCILIATION_INTERVAL_SECONDS", "60"
    )
    if reconciliation_interval_seconds <= 0:
        raise ConfigError("RECONCILIATION_INTERVAL_SECONDS must be > 0")

    # -- gold pin -----------------------------------------------------------
    requested_gold = _bool(env, "ALLOW_XAUT_FOR_GOLD", default=False)
    gold_pin_overridden_request = requested_gold and V1_GOLD_PIN_ACTIVE
    allow_xaut_for_gold = requested_gold and not V1_GOLD_PIN_ACTIVE

    tradeable = (
        V1_TRADEABLE_ASSETS
        if not allow_xaut_for_gold
        else V1_TRADEABLE_ASSETS | {CanonicalAsset.XAU}
    )

    # -- telegram -----------------------------------------------------------
    telegram_api_id_raw = _raw(env, "TELEGRAM_API_ID")
    telegram_api_id = _int(env, "TELEGRAM_API_ID") if telegram_api_id_raw else None
    telegram_api_hash = _raw(env, "TELEGRAM_API_HASH")
    telegram_channel = _parse_channel(_raw(env, "TELEGRAM_CHANNEL"))

    # -- alerting -------------------------------------------------------------
    # A separate send-only Bot API credential, distinct from the telegram_*
    # user-account listener fields above. Optional: alerting is best-effort
    # and must never gate startup, so an absent value degrades to "no alert
    # sent, logged as a warning" rather than a ConfigError.
    alert_telegram_bot_token = _raw(env, "ALERT_TELEGRAM_BOT_TOKEN")
    alert_telegram_chat_id = _raw(env, "ALERT_TELEGRAM_CHAT_ID")

    # -- paths --------------------------------------------------------------
    kill_switch_path = Path(_raw(env, "KILL_SWITCH_PATH") or "./KILL_SWITCH")
    db_path = Path(_raw(env, "DB_PATH") or "./data/bot.db")
    log_path = Path(_raw(env, "LOG_PATH") or "./logs/bot.jsonl")
    telegram_session_path = Path(
        _raw(env, "TELEGRAM_SESSION_PATH") or "./secrets/telegram.session"
    )

    log_level = (_raw(env, "LOG_LEVEL") or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"LOG_LEVEL={log_level!r} is not a valid level")

    kraken_cli_path = _raw(env, "KRAKEN_CLI_PATH") or "kraken"
    kraken_cli_timeout_seconds = _int(env, "KRAKEN_CLI_TIMEOUT_SECONDS", "30")
    if kraken_cli_timeout_seconds <= 0:
        raise ConfigError("KRAKEN_CLI_TIMEOUT_SECONDS must be > 0")

    return Config(
        trading_mode=trading_mode,
        trade_margin_usd=trade_margin_usd,
        max_leverage=max_leverage,
        default_leverage=default_leverage,
        max_entry_deviation_percent=max_entry_deviation_percent,
        max_sizing_deviation_percent=max_sizing_deviation_percent,
        entry_order_expiry_hours=entry_order_expiry_hours,
        reconciliation_interval_seconds=reconciliation_interval_seconds,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_channel=telegram_channel,
        telegram_session_path=telegram_session_path,
        alert_telegram_bot_token=alert_telegram_bot_token,
        alert_telegram_chat_id=alert_telegram_chat_id,
        kill_switch_path=kill_switch_path,
        db_path=db_path,
        log_path=log_path,
        log_level=log_level,
        kraken_cli_path=kraken_cli_path,
        kraken_cli_timeout_seconds=kraken_cli_timeout_seconds,
        allow_xaut_for_gold=allow_xaut_for_gold,
        gold_pin_active=V1_GOLD_PIN_ACTIVE,
        gold_pin_overridden_request=gold_pin_overridden_request,
        tradeable_assets=frozenset(tradeable),
    )


def collect_secrets(env: dict[str, str] | None = None) -> set[str]:
    """Secret values to scrub from logs. Read directly from the environment and
    never stored on Config, so a Config repr can't leak them."""
    source = env if env is not None else os.environ
    keys = (
        "KRAKEN_API_KEY",
        "KRAKEN_API_SECRET",
        "KRAKEN_FUTURES_API_KEY",
        "KRAKEN_FUTURES_API_SECRET",
        "TELEGRAM_API_HASH",
        "LIVE_TRADING_ACK",
        "ALERT_TELEGRAM_BOT_TOKEN",
    )
    return {v.strip() for k in keys if (v := source.get(k)) and len(v.strip()) >= 8}
