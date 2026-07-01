-- Migration 002: Add config_json column to agents table.
-- The config_json column was already present in the initial schema.sql
-- (`config_json TEXT NOT NULL DEFAULT '{}'`), so this migration is a
-- documentation placeholder. No ALTER TABLE is needed for fresh installs.
-- For local DBs created before this column existed (none should exist),
-- run: ALTER TABLE agents ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';

-- This migration documents that config_json is used to store per-agent
-- overrides such as {"wake_interval": 90}.
