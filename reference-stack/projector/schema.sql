-- Projector state (database `projections`). All statements idempotent: safe to run at every start.

-- Where we are in the topic. One row per topic-partition. Updated in the SAME transaction as the read model.
CREATE TABLE IF NOT EXISTS projector_checkpoint (
  projector   text   NOT NULL,
  topic       text   NOT NULL,
  partition   int    NOT NULL,
  next_offset bigint NOT NULL,                 -- next offset to consume (last processed + 1)
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (projector, topic, partition)
);

-- Event ids already applied. The reason at-least-once delivery becomes effectively-once.
-- Unbounded by design here; production would prune below the checkpoint horizon or partition by month.
CREATE TABLE IF NOT EXISTS processed_events (
  projector    text NOT NULL,
  event_id     text NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (projector, event_id)
);

-- Read model 1: current content documents (from Content* events).
CREATE TABLE IF NOT EXISTS rm_content_docs (
  doc_id         text PRIMARY KEY,
  title          text,
  section        text,
  doc_type       text,
  url            text,
  hash           text,
  removed        boolean NOT NULL DEFAULT false,
  stream_version bigint,
  last_event_id  text NOT NULL,
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Read model 2: per-algorithm search counters (from Search* events). Counters are the case where
-- double-applying an event would silently corrupt the model, hence the processed_events set.
CREATE TABLE IF NOT EXISTS rm_algorithm_stats (
  algorithm  text PRIMARY KEY,
  submitted  bigint NOT NULL DEFAULT 0,
  opened     bigint NOT NULL DEFAULT 0,
  overridden_from bigint NOT NULL DEFAULT 0,   -- agent proposed it, person chose another
  overridden_to   bigint NOT NULL DEFAULT 0    -- person chose it over the proposal
);
