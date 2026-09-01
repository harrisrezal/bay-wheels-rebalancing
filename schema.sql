-- Bay Wheels rebalancing — Phase 0 storage schema.
-- GBFS v2.3 (https://gbfs.lyft.com/gbfs/2.3/bay/gbfs.json), 633 stations, feed ttl 60s.

-- Slowly-changing station metadata. Refetched daily; one row per station per fetch date so
-- station openings/closures/capacity changes stay visible rather than being overwritten.
CREATE TABLE IF NOT EXISTS station_info (
    station_id  TEXT NOT NULL,
    fetched_on  TEXT NOT NULL,          -- YYYY-MM-DD, local date of fetch
    name        TEXT,
    short_name  TEXT,                   -- e.g. "SJ-Q11"; join key candidate for historical trip CSVs
    lat         REAL,
    lon         REAL,
    region_id   TEXT,
    capacity    INTEGER,
    address     TEXT,
    PRIMARY KEY (station_id, fetched_on)
);

-- One row per station per poll. Every poll is stored, including unchanged states: the point of
-- Phase 0 is an auditable observation series, and change-data-capture would make gap analysis
-- ambiguous (an absent row could mean "no change" or "we missed it").
CREATE TABLE IF NOT EXISTS station_status (
    station_id             TEXT NOT NULL,
    observed_at            INTEGER NOT NULL,  -- unix; OUR poll time, the sampling grid
    feed_last_updated      INTEGER,           -- unix; feed-level last_updated
    last_reported          INTEGER,           -- unix; station's own last report (staleness check)
    num_bikes_available    INTEGER,           -- TOTAL vehicles, ebikes INCLUDED
    num_ebikes_available   INTEGER,           -- ebike subset; classic = available - ebikes
    num_bikes_disabled     INTEGER,
    num_docks_available    INTEGER,
    num_docks_disabled     INTEGER,
    is_installed           INTEGER,
    is_renting             INTEGER,
    is_returning           INTEGER,
    PRIMARY KEY (station_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_status_observed ON station_status (observed_at);
CREATE INDEX IF NOT EXISTS idx_status_station  ON station_status (station_id, observed_at);

-- Every poll attempt, successful or not. Gaps must be explained, never silent (PRD F0.4).
CREATE TABLE IF NOT EXISTS poll_log (
    polled_at         INTEGER PRIMARY KEY,
    status            TEXT NOT NULL,     -- ok | error
    feed_last_updated INTEGER,
    stations_seen     INTEGER,
    rows_written      INTEGER,
    duration_ms       INTEGER,
    error             TEXT
);
