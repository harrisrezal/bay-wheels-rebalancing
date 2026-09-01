-- Bay Wheels rebalancing — Postgres schema (Vercel-hosted collection).
--
-- Differs from the local SQLite schema in two deliberate ways:
--   1. TIMESTAMPTZ instead of unix integers. Phase 1's 5-minute grid resample is
--      dramatically simpler with date_trunc/generate_series than with integer math.
--   2. Change-data-capture. Only ~16% of stations change between consecutive polls,
--      so storing every row costs ~6x for no extra information. poll_log records every
--      successful poll, which is what makes an absent row unambiguous: absence plus a
--      logged poll means "no change", never "we missed it".

CREATE TABLE IF NOT EXISTS station_info (
    station_id  TEXT NOT NULL,
    fetched_on  DATE NOT NULL,
    name        TEXT,
    short_name  TEXT,              -- join key to historical trip CSVs (99.2% of volume)
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    region_id   TEXT,
    capacity    INTEGER,
    address     TEXT,
    PRIMARY KEY (station_id, fetched_on)
);
CREATE INDEX IF NOT EXISTS idx_info_short_name ON station_info (short_name);

-- Only rows where a station's inventory state CHANGED. Reconstruct the full series by
-- forward-filling between rows, bounded by poll_log coverage.
CREATE TABLE IF NOT EXISTS station_status (
    station_id             TEXT        NOT NULL,
    observed_at            TIMESTAMPTZ NOT NULL,   -- our poll time, the sampling grid
    feed_last_updated      TIMESTAMPTZ,
    last_reported          TIMESTAMPTZ,            -- station's own report time (staleness)
    num_bikes_available    SMALLINT,               -- TOTAL vehicles, ebikes INCLUDED
    num_ebikes_available   SMALLINT,               -- classic = available - ebikes
    num_bikes_disabled     SMALLINT,
    num_docks_available    SMALLINT,
    num_docks_disabled     SMALLINT,
    is_installed           SMALLINT,
    is_renting             SMALLINT,
    is_returning           SMALLINT,
    PRIMARY KEY (station_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_status_observed ON station_status (observed_at);

-- Latest known state per station. Exists so change detection is one indexed read of
-- ~633 rows instead of a growing DISTINCT ON scan over station_status.
CREATE TABLE IF NOT EXISTS station_current (
    station_id             TEXT PRIMARY KEY,
    observed_at            TIMESTAMPTZ NOT NULL,
    num_bikes_available    SMALLINT,
    num_ebikes_available   SMALLINT,
    num_bikes_disabled     SMALLINT,
    num_docks_available    SMALLINT,
    num_docks_disabled     SMALLINT,
    is_installed           SMALLINT,
    is_renting             SMALLINT,
    is_returning           SMALLINT
);

-- Every poll attempt. This is what makes CDC unambiguous and what the gate metric
-- is computed from.
CREATE TABLE IF NOT EXISTS poll_log (
    polled_at         TIMESTAMPTZ PRIMARY KEY,
    status            TEXT NOT NULL,        -- ok | error
    feed_last_updated TIMESTAMPTZ,
    stations_seen     INTEGER,
    rows_written      INTEGER,              -- CHANGED rows written, not stations seen
    duration_ms       INTEGER,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_log_status ON poll_log (status, polled_at);
