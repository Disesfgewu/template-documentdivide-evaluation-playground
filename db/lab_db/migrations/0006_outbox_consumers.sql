-- 0006_outbox_consumers.sql
-- Per-consumer idempotent outbox consumption tracking (SPEC 10.2).

CREATE TABLE outbox_consumptions (
    consumer     text        NOT NULL CHECK (consumer ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    event_id     uuid        NOT NULL REFERENCES outbox_events(event_id) ON DELETE RESTRICT,
    consumed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);
