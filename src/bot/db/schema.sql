-- kraken-signal-bot schema (brief 31).
--
-- Design rules encoded here rather than left to application discipline:
--   * (channel_id, message_id) is UNIQUE  -> Telegram idempotency layer 1
--   * client_order_id is UNIQUE           -> order idempotency (brief 30)
--   * intent_* and actual_* columns are separate and never conflated (brief 39)
--   * events is append-only; nothing updates or deletes from it

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Telegram messages: written BEFORE any parsing or execution is attempted, so
-- a crash mid-processing still leaves an auditable record of what arrived.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    received_at         TEXT    NOT NULL,
    message_timestamp   TEXT,
    raw_text            TEXT    NOT NULL,
    reply_to_message_id INTEGER,

    -- parser output
    parsed_type         TEXT,
    parsed_asset        TEXT,
    parsed_direction    TEXT,
    parsed_values_json  TEXT,
    parse_confidence    REAL,
    parse_warnings_json TEXT,
    parse_errors_json   TEXT,

    -- signal fingerprint for dedupe layer 2 (content-based, reformat-tolerant)
    signal_fingerprint  TEXT,

    -- decision: every message gets one; silence is never an outcome
    execution_decision  TEXT,
    rejection_reason    TEXT,
    rejection_detail    TEXT,
    processed_at        TEXT,

    UNIQUE (channel_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_fingerprint ON messages (signal_fingerprint);
CREATE INDEX IF NOT EXISTS idx_messages_decision    ON messages (execution_decision);
CREATE INDEX IF NOT EXISTS idx_messages_type        ON messages (parsed_type);

-- Every version of every message. `messages` holds the ORIGINAL (its UNIQUE
-- (channel_id, message_id) is the idempotency layer); an edit appends a row
-- here instead of overwriting anything. Telegram only ever hands us the latest
-- text of an edited message, so a version exists only if the bot saw it.
-- class/action/reason are what the bot decided and did for THAT version.
CREATE TABLE IF NOT EXISTS message_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_row_id      INTEGER NOT NULL REFERENCES messages (id),
    channel_id          INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    version             INTEGER NOT NULL,
    recorded_at         TEXT    NOT NULL,
    edit_date           TEXT,
    text                TEXT    NOT NULL,
    text_hash           TEXT    NOT NULL,
    media               TEXT,
    reply_to_message_id INTEGER,
    message_class       TEXT    NOT NULL,
    action              TEXT    NOT NULL,
    reason              TEXT    NOT NULL,
    trade_id            INTEGER REFERENCES trades (id),
    UNIQUE (channel_id, message_id, version)
);

CREATE INDEX IF NOT EXISTS idx_message_versions_class  ON message_versions (message_class);
CREATE INDEX IF NOT EXISTS idx_message_versions_action ON message_versions (action);

-- ---------------------------------------------------------------------------
-- Trades. INTENT columns come from the signal; ACTUAL columns come only from
-- exchange-confirmed fills. Nothing writes both from the same code path.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uid               TEXT    NOT NULL UNIQUE,
    origin_message_row_id   INTEGER REFERENCES messages (id),
    origin_channel_id       INTEGER NOT NULL,
    origin_message_id       INTEGER NOT NULL,

    asset                   TEXT    NOT NULL,
    symbol                  TEXT    NOT NULL,
    direction               TEXT    NOT NULL,

    -- flagged when the instrument stands in for the asset (XAUT for XAU)
    is_substitute_instrument INTEGER NOT NULL DEFAULT 0,
    substitute_note          TEXT,

    -- INTENT (from signal)
    intent_entry_prices_json TEXT,
    intent_take_profits_json TEXT,
    intent_stop_loss         TEXT,
    intent_margin_usd        TEXT,
    intent_leverage          TEXT,
    intent_quantity          TEXT,

    -- ACTUAL (from exchange)
    actual_quantity          TEXT    NOT NULL DEFAULT '0',
    actual_avg_entry_price   TEXT,
    actual_effective_leverage TEXT,
    realized_pnl             TEXT,

    -- protection
    active_stop_order_uid    TEXT,
    stop_trigger_price       TEXT,
    breakeven_active         INTEGER NOT NULL DEFAULT 0,
    breakeven_price          TEXT,

    state                    TEXT    NOT NULL,
    state_detail             TEXT,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL,
    closed_at                TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_state  ON trades (state);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);

-- This used to be a UNIQUE partial index enforcing one active trade per symbol
-- (docs/PLAN.md 4.1). Up to MAX_TRADES_PER_SYMBOL same-direction trades may now
-- share a symbol; Store.create_trade enforces the cap and the direction rule
-- atomically inside its IMMEDIATE transaction. The old index must be dropped or
-- it would still reject the second trade on an existing database.
DROP INDEX IF EXISTS idx_trades_one_active_per_symbol;

-- ---------------------------------------------------------------------------
-- Orders. A row is written with status INTENT *before* submission, so a crash
-- between "sent" and "recorded" still leaves a durable trace to reconcile
-- against (brief 30).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_uid           TEXT    NOT NULL UNIQUE,
    client_order_id     TEXT    NOT NULL UNIQUE,
    exchange_order_id   TEXT,
    trade_id            INTEGER NOT NULL REFERENCES trades (id),

    purpose             TEXT    NOT NULL,
    sequence            INTEGER NOT NULL DEFAULT 0,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    order_type          TEXT    NOT NULL,
    reduce_only         INTEGER NOT NULL DEFAULT 0,

    price               TEXT,
    trigger_price       TEXT,
    trigger_signal      TEXT,
    quantity            TEXT    NOT NULL,
    filled_quantity     TEXT    NOT NULL DEFAULT '0',
    avg_fill_price      TEXT,

    status              TEXT    NOT NULL,
    status_detail       TEXT,
    expires_at          TEXT,
    created_at          TEXT    NOT NULL,
    submitted_at        TEXT,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_trade    ON orders (trade_id);
CREATE INDEX IF NOT EXISTS idx_orders_status   ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_exchange ON orders (exchange_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_purpose  ON orders (trade_id, purpose);

-- ---------------------------------------------------------------------------
-- Append-only audit log. Never updated, never deleted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at     TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    trade_id        INTEGER REFERENCES trades (id),
    order_id        INTEGER REFERENCES orders (id),
    message_row_id  INTEGER REFERENCES messages (id),
    severity        TEXT    NOT NULL DEFAULT 'INFO',
    summary         TEXT    NOT NULL,
    detail_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_trade ON events (trade_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_time  ON events (occurred_at);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;
