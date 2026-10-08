\set ON_ERROR_STOP on
BEGIN;
\copy raw."bureau_balance" ("SK_ID_BUREAU","MONTHS_BALANCE","STATUS") FROM 'data/raw/bureau_balance.csv' WITH (FORMAT CSV, HEADER TRUE, NULL '')
COMMIT;
