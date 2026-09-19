#!/usr/bin/env python3
"""Seed (or reset) the synthetic analytics data used by the portal scenarios.

Creates a deterministic `synthetic_orders` table in the Superset light stack's
Postgres, registers the database connection and a dataset in Superset, and
prints the resulting identifiers as JSON.

Idempotent: the table is created if missing and the Superset database/dataset
records are reused. `--reset` additionally drops and repopulates the rows, so a
scenario run always starts from the same 600 deterministic rows.

Usage:
    python3 scripts/seed_synthetic.py [--reset]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from superset_client import SupersetClient

DB_CONTAINER = "superset-db-light-1"
DB_NAME = "superset_light"
DB_USER = "superset"
DATABASE_NAME = "Synthetic Analytics"
SQLALCHEMY_URI = f"postgresql+psycopg2://superset:superset@db-light:5432/{DB_NAME}"
TABLE_NAME = "synthetic_orders"
SCHEMA = "public"

ROW_COUNT = 600

DDL = f"""
DROP TABLE IF EXISTS {SCHEMA}.{TABLE_NAME};
CREATE TABLE {SCHEMA}.{TABLE_NAME} (
    id serial PRIMARY KEY,
    order_ts timestamp NOT NULL,
    region text NOT NULL,
    channel text NOT NULL,
    product text NOT NULL,
    units integer NOT NULL,
    revenue numeric(10, 2) NOT NULL
);
INSERT INTO {SCHEMA}.{TABLE_NAME} (order_ts, region, channel, product, units, revenue)
SELECT
    timestamp '2026-01-01 00:00:00' + (g % 180) * interval '1 day',
    (array['north', 'south', 'east', 'west'])[1 + (g % 4)],
    (array['web', 'retail', 'partner'])[1 + (g % 3)],
    (array['alpha', 'beta', 'gamma', 'delta', 'epsilon'])[1 + (g % 5)],
    1 + (g % 9),
    round((((g * 37) % 500) + 10)::numeric, 2)
FROM generate_series(1, {ROW_COUNT}) AS g;
"""

TABLE_EXISTS_SQL = (
    f"SELECT to_regclass('{SCHEMA}.{TABLE_NAME}') IS NOT NULL;"  # noqa: S608
)


def run_sql(sql: str) -> str:
    return subprocess.run(
        ["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="recreate table rows")
    args = parser.parse_args()

    table_exists = "t" in run_sql(TABLE_EXISTS_SQL).splitlines()[2]
    if args.reset or not table_exists:
        run_sql(DDL)
    row_count = run_sql(
        f"SELECT count(*) FROM {SCHEMA}.{TABLE_NAME};"
    ).strip().splitlines()[2].strip()

    client = SupersetClient()
    client.login()

    database_id = client.find_database(DATABASE_NAME)
    if database_id is None:
        database_id = client.create_database(DATABASE_NAME, SQLALCHEMY_URI)

    dataset_id = client.find_dataset(TABLE_NAME)
    if dataset_id is None:
        dataset_id = client.create_dataset(database_id, SCHEMA, TABLE_NAME)

    print(
        json.dumps(
            {
                "rows": int(row_count),
                "database_id": database_id,
                "dataset_id": dataset_id,
                "table": f"{SCHEMA}.{TABLE_NAME}",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
