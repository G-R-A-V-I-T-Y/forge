-- Migration 001: Add OHLCV snapshot and fingerprint columns to trades table.
-- SQLite ALTER TABLE ADD COLUMN is safe to re-run; each column is added only
-- if it does not already exist (checked at application level in store/db.py).

ALTER TABLE trades ADD COLUMN ohlcv_15m_40_blob BLOB;
ALTER TABLE trades ADD COLUMN ohlcv_1h_20_blob BLOB;
ALTER TABLE trades ADD COLUMN ohlcv_4h_10_blob BLOB;
ALTER TABLE trades ADD COLUMN funding_history_blob BLOB;
ALTER TABLE trades ADD COLUMN oi_data_json TEXT;
ALTER TABLE trades ADD COLUMN liquidation_data_json TEXT;
ALTER TABLE trades ADD COLUMN regime TEXT;
ALTER TABLE trades ADD COLUMN expected_value_text TEXT;
