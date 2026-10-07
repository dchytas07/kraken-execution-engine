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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

from .models import CanonicalAsset, TradingMode

# ---------------------------------------------------------------------------
# GOLD/XAU: pin LIFTED 2026-07-31 by explicit, repeated operator decision.
#
# Kraken lists no spot-gold perpetual. PF_XAUTUSD is Tether Gold (XAUT), a
# wrapped token, and the signal channel quotes entry/TP/SL against SPOT gold.
# The gap between the two was measured twice and FAILED both times:
#
#   Study A (2026-07-29, 490 overlapping hours): p99 basis 0.836% against a
#     0.5% tolerance; max intra-hour basis change 1.417% against 0.25%.
#   Live spot check (2026-07-31): XAUT mark 4031.16 vs COMEX GC=F 4107.00,
#     a basis of -1.85% -- larger than the whole default 1.5% stale-entry
#     budget on its own.
#
# The operator was shown both figures and chose to enable gold anyway. That is
# their call to make; this comment exists so nobody later mistakes it for an
# oversight. The mitigations that remain in force:
#
#   * ALLOW_XAUT_FOR_GOLD must still be explicitly true in .env -- lifting the
#     pin re-arms that switch, it does not enable gold by itself.
#   * Startup logs "GOLD/XAU -> PF_XAUTUSD (Tether Gold, NOT spot XAU)".
#   * Every gold trade is tagged as a substitute instrument in the audit trail
#     (is_substitute / substitute_note, set in exchange/specs.py).
#   * A gold-specific entry-deviation allowance (see GOLD_ENTRY_DEVIATION_
#     PERCENT below) so the measured basis does not silently consume the
#     staleness budget meant for genuine staleness.
#
# Re-pinning is a one-line change back to True.
# ---------------------------------------------------------------------------
# Every trade runs at exactly this leverage: not the signal's (they ask for 5x
# to 75x), not "whatever the stop allows". A far stop is REFUSED
# (STOP_BEYOND_LIQUIDATION), never answered by quietly lowering leverage, so the
# account's leverage is a constant the operator can reason about and the
# reconciler can re-assert without a per-trade lookup.
FIXED_LEVERAGE = Decimal(10)

V1_GOLD_PIN_ACTIVE = False

# Gold trades against a proxy whose basis to spot gold was measured at -1.85%
# live and 0.836% at Study A's p99. The default 1.5% stale-entry budget is
# meant to catch SIGNALS THAT AGED, but for gold the basis alone can exceed
# it, so every gold signal would be refused as stale for a reason that has
# nothing to do with staleness.
#
# 3.5% = roughly the worst observed basis (~1.85%) plus the normal 1.5%
# staleness allowance, rounded up slightly. This is deliberately a WIDENING,
# not a tightening: it is the price of trading a proxy, and it means a gold
# signal can be up to ~3.5% away from the mark before it is refused. Any
# tighter and gold cannot trade at all; any looser and genuine staleness stops
# being caught.
GOLD_ENTRY_DEVIATION_PERCENT = Decimal("3.5")

LIVE_ACK_PHRASE = "I_UNDERSTAND_THIS_IS_REAL_MONEY"

# A channel id, with or without Telegram's -100 supergroup/channel prefix.
_NUMERIC_CHANNEL = re.compile(r"-?\d+")

# Assets tradeable in v1. XAU is deliberately absent while the pin holds.
V1_TRADEABLE_ASSETS = frozenset(
    {
        CanonicalAsset.BTC,
        CanonicalAsset.ETH,
        CanonicalAsset.SOL,
        # Added 2026-07-31 after live spec verification (docs/KRAKEN_NOTES.md
        # 2). Unlike XAU, PF_BNBUSD is a direct perpetual, not a substitute.
        CanonicalAsset.BNB,
        # Added 2026-07-31 after the same live verification as BNB.
        CanonicalAsset.XRP,
        CanonicalAsset.DOGE,
    }
)


class ConfigError(Exception):
    """Configuration is unusable. Always fatal — never caught and defaulted."""


@dataclass(frozen=True, slots=True)
class Config:
    trading_mode: TradingMode
    trade_margin_usd: Decimal
    max_leverage: Decimal
    max_entry_deviation_percent: Decimal
    max_sizing_deviation_percent: Decimal
    # Stop-vs-liquidation guard: at the fixed leverage the stop must trigger at
    # least this many percentage points before the estimated liquidation price
    # (2% maintenance); otherwise the signal is refused, never re-levered.
    stop_liquidation_buffer_percent: Decimal
    # Same-direction trades allowed to share one symbol. Kraken nets positions
    # per symbol, so each additional trade shares the exchange position; every
    # order is tagged per trade and sized to that trade's own quantity.
    max_trades_per_symbol: int
    # Ceiling on margin committed across ALL live trades (including ones still
    # waiting for entry). None = not configured, and a missing limit FAILS
    # CLOSED: every new trade is refused until it is set. (It is not a startup
    # error: that would crash-loop the service, and protection of positions
    # already open must keep running.)
    max_total_open_margin: Decimal | None
    entry_order_expiry_hours: int
    # A NEW_SIGNAL whose Telegram post time is older than this (or unknown) is
    # recorded and alerted, never traded: a backfill after downtime or a delayed
    # delivery must not open a trade on a stale call.
    max_signal_age_minutes: int
    reconciliation_interval_seconds: int
    # The daily summary goes out at this hour (0-23) in this timezone, every day,
    # instead of "24h after the last one" (which drifted from 20:13 to 16:56 over
    # three months of restarts).
    daily_summary_hour: int
    daily_summary_tz: ZoneInfo

    telegram_api_id: int | None
    telegram_api_hash: str | None
    # int for a numeric channel id, str for an @username. The distinction is
    # load-bearing, not cosmetic — see _parse_channel.
    telegram_channel: str | int | None
    telegram_session_path: Path

    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    # Optional dead-man's-switch URL (HEALTHCHECK_URL). None = heartbeat off.
    healthcheck_url: str | None

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
    def fixed_leverage(self) -> Decimal:
        """The leverage every trade runs at (see FIXED_LEVERAGE)."""
        return FIXED_LEVERAGE

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
            f"margin=${self.trade_margin_usd} leverage={FIXED_LEVERAGE}x fixed "
            f"(cap {self.max_leverage}x)",
            f"tradeable: {', '.join(sorted(a.value for a in self.tradeable_assets))}",
            f"kill_switch={self.kill_switch_path} db={self.db_path}",
            f"max {self.max_trades_per_symbol} same-direction trade(s) per symbol; "
            + (f"total open margin cap ${self.max_total_open_margin}"
               if self.max_total_open_margin is not None
               else "MAX_TOTAL_OPEN_MARGIN NOT SET: ALL NEW TRADES WILL BE "
                    "REFUSED until it is configured"),
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


def default_timezone_name() -> str:
    """The host's IANA timezone name, falling back to UTC.

    $TZ, then /etc/timezone (Debian/Ubuntu/WSL), then the /etc/localtime
    symlink. Anything unrecognised falls through to UTC rather than guessing.
    """
    candidates: list[str] = []
    if os.environ.get("TZ"):
        candidates.append(os.environ["TZ"].lstrip(":"))
    try:
        candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return "UTC"


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

    if max_leverage < FIXED_LEVERAGE:
        raise ConfigError(
            f"MAX_LEVERAGE={max_leverage} is below the fixed {FIXED_LEVERAGE}x "
            "every trade runs at; the cap would refuse every signal"
        )

    max_entry_deviation_percent = _positive(
        "MAX_ENTRY_DEVIATION_PERCENT",
        _decimal(env, "MAX_ENTRY_DEVIATION_PERCENT", "1.5"),
    )
    max_sizing_deviation_percent = _positive(
        "MAX_SIZING_DEVIATION_PERCENT",
        _decimal(env, "MAX_SIZING_DEVIATION_PERCENT", "5.0"),
    )

    daily_summary_hour = _int(env, "DAILY_SUMMARY_HOUR", "20")
    if not 0 <= daily_summary_hour <= 23:
        raise ConfigError(
            f"DAILY_SUMMARY_HOUR={daily_summary_hour} must be between 0 and 23"
        )
    tz_name = _raw(env, "DAILY_SUMMARY_TZ") or default_timezone_name()
    try:
        daily_summary_tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"DAILY_SUMMARY_TZ={tz_name!r} is not a known IANA timezone"
        ) from exc
    max_trades_per_symbol = _int(env, "MAX_TRADES_PER_SYMBOL", "2")
    if not 1 <= max_trades_per_symbol <= 4:
        raise ConfigError(
            f"MAX_TRADES_PER_SYMBOL={max_trades_per_symbol} must be between 1 and 4"
        )
    max_total_open_margin = None
    if _raw(env, "MAX_TOTAL_OPEN_MARGIN"):
        max_total_open_margin = _positive(
            "MAX_TOTAL_OPEN_MARGIN", _decimal(env, "MAX_TOTAL_OPEN_MARGIN", "0")
        )
        if max_total_open_margin < trade_margin_usd:
            raise ConfigError(
                f"MAX_TOTAL_OPEN_MARGIN={max_total_open_margin} is below one "
                f"trade's margin ({trade_margin_usd}); no trade could ever open"
            )
    stop_liquidation_buffer_percent = _decimal(env, "STOP_LIQ_BUFFER_PCT", "1.0")
    if stop_liquidation_buffer_percent < 0:
        raise ConfigError(
            f"STOP_LIQ_BUFFER_PCT must be >= 0, got {stop_liquidation_buffer_percent}"
        )

    entry_order_expiry_hours = _int(env, "ENTRY_ORDER_EXPIRY_HOURS", "24")
    if entry_order_expiry_hours <= 0:
        raise ConfigError("ENTRY_ORDER_EXPIRY_HOURS must be > 0")

    max_signal_age_minutes = _int(env, "MAX_SIGNAL_AGE_MINUTES", "30")
    if max_signal_age_minutes <= 0:
        raise ConfigError("MAX_SIGNAL_AGE_MINUTES must be > 0")

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
    # Optional and never fatal: monitoring must not be able to stop the bot.
    healthcheck_url = _raw(env, "HEALTHCHECK_URL")

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
        max_entry_deviation_percent=max_entry_deviation_percent,
        max_sizing_deviation_percent=max_sizing_deviation_percent,
        stop_liquidation_buffer_percent=stop_liquidation_buffer_percent,
        max_trades_per_symbol=max_trades_per_symbol,
        max_total_open_margin=max_total_open_margin,
        entry_order_expiry_hours=entry_order_expiry_hours,
        max_signal_age_minutes=max_signal_age_minutes,
        reconciliation_interval_seconds=reconciliation_interval_seconds,
        daily_summary_hour=daily_summary_hour,
        daily_summary_tz=daily_summary_tz,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_channel=telegram_channel,
        telegram_session_path=telegram_session_path,
        alert_telegram_bot_token=alert_telegram_bot_token,
        alert_telegram_chat_id=alert_telegram_chat_id,
        healthcheck_url=healthcheck_url,
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
        "HEALTHCHECK_URL",
    )
    return {v.strip() for k in keys if (v := source.get(k)) and len(v.strip()) >= 8}
