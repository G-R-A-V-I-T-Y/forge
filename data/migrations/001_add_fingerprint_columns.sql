-- Migration 001: Add OHLCV snapshot and fingerprint columns to trades table.
-- Applied idempotently by store/db.py._migrate_trades_columns() using PRAGMA
-- table_info checks. This file documents the full column set for reference.
-- All ALTER TABLE ADD COLUMN statements will fail harmlessly if the column
-- already exists (the migration code skips them).

ALTER TABLE trades ADD COLUMN ohlcv_15m_40_blob BLOB;
ALTER TABLE trades ADD COLUMN ohlcv_1h_20_blob BLOB;
ALTER TABLE trades ADD COLUMN ohlcv_4h_10_blob BLOB;
ALTER TABLE trades ADD COLUMN funding_history_blob BLOB;
ALTER TABLE trades ADD COLUMN oi_data_json TEXT;
ALTER TABLE trades ADD COLUMN liquidation_data_json TEXT;
ALTER TABLE trades ADD COLUMN regime TEXT;
ALTER TABLE trades ADD COLUMN expected_value_text TEXT;
ALTER TABLE trades ADD COLUMN funding_rate_current REAL;
ALTER TABLE trades ADD COLUMN open_interest_24h_change_pct REAL;
