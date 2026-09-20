"""One-time synthetic fixture for B1: a saved table chart of large integers.

Run this once, inside the Superset container, with the app context available:

    docker compose exec superset python3 - < scenarios/b1_fixture.py
    # or, from a checkout mounted in the container:
    python3 -m scenarios.b1_fixture

It creates, under uniquely named synthetic resources only:

* schema `repro44007_metrics` with one small table of byte counters, holding
  two values beyond the JavaScript safe-integer range and two well inside it;
* a physical dataset over that table;
* a saved Table chart whose `MAX(big_bytes)` column already carries the
  `MEMORY_BINARY` number format the user chose.

The format is saved *here*, once, precisely so that monitoring can be
read-only: a scan opens this chart and looks, it never sets a format or
presses Save. Nothing existing is modified — no stock role, no other dataset,
chart or user — and there is no customer data anywhere in it.

The values are chosen to make both sides of the symptom visible in one chart:
`9007199254740993` is `Number.MAX_SAFE_INTEGER + 2`, while `4096`/`8192`
format correctly as `4KiB`/`8KiB` and are the control.
"""

from __future__ import annotations

import json
import os
from typing import Any

SCHEMA = os.environ.get("B1_SCHEMA", "repro44007_metrics")
TABLE = os.environ.get("B1_TABLE", "byte_counters")
DATABASE = os.environ.get("B1_DATABASE", "examples")
SLICE_NAME = os.environ.get("B1_SLICE_NAME", "repro44007 — byte counters (Table)")
#: The same format over the same column type, with every value inside the safe
#: range. Scanning this chart is the control: it must produce no warning.
CONTROL_SLICE_NAME = os.environ.get(
    "B1_CONTROL_SLICE_NAME", "repro44007 — safe byte counters (control Table)"
)

#: Beyond 2**53, so the formatter's conversion to a JavaScript number is the
#: failure, and safely below it, so the same format demonstrably works.
LARGE_VALUES = (1425300509404304697, 9007199254740993)
SAFE_VALUES = (4096, 8192)

BIG_METRIC = "MAX(big_bytes)"
SMALL_METRIC = "MAX(small_bytes)"


def chart_params(dataset_id: int, *, control: bool = False) -> dict[str, Any]:
    """A plain aggregate Table chart with the memory format already applied."""
    if control:
        return {
            "datasource": f"{dataset_id}__table",
            "viz_type": "table",
            "query_mode": "aggregate",
            "groupby": ["bucket"],
            "metrics": [
                {
                    "expressionType": "SIMPLE",
                    "column": {"column_name": "small_bytes", "type": "BIGINT"},
                    "aggregate": "MAX",
                    "label": SMALL_METRIC,
                    "optionName": "metric_repro44007_small",
                }
            ],
            "row_limit": 10,
            "adhoc_filters": [],
            "column_config": {SMALL_METRIC: {"d3NumberFormat": "MEMORY_BINARY"}},
        }
    return {
        "datasource": f"{dataset_id}__table",
        "viz_type": "table",
        "query_mode": "aggregate",
        "groupby": ["bucket"],
        "metrics": [
            {
                "expressionType": "SIMPLE",
                "column": {"column_name": "big_bytes", "type": "BIGINT"},
                "aggregate": "MAX",
                "label": BIG_METRIC,
                "optionName": "metric_repro44007_big",
            },
            {
                "expressionType": "SIMPLE",
                "column": {"column_name": "small_bytes", "type": "BIGINT"},
                "aggregate": "MAX",
                "label": SMALL_METRIC,
                "optionName": "metric_repro44007_small",
            },
        ],
        "row_limit": 10,
        "adhoc_filters": [],
        # What a user picks in Customize columns → Number formatting. Saved
        # once here; the monitor only ever reads it back.
        "column_config": {
            BIG_METRIC: {"d3NumberFormat": "MEMORY_BINARY"},
            SMALL_METRIC: {"d3NumberFormat": "MEMORY_BINARY"},
        },
    }


def main() -> int:
    # Superset is importable only where this runs: inside the web container,
    # against that stack's own metadata database.
    from superset.app import create_app  # type: ignore[import-not-found]

    app = create_app()
    with app.app_context():
        from superset import db  # type: ignore[import-not-found]
        from superset.connectors.sqla.models import (  # type: ignore[import-not-found]
            SqlaTable,
        )
        from superset.models.core import Database  # type: ignore[import-not-found]
        from superset.models.slice import Slice  # type: ignore[import-not-found]

        database = db.session.query(Database).filter_by(database_name=DATABASE).one()
        with database.get_sqla_engine() as engine, engine.begin() as conn:
            conn.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            conn.exec_driver_sql(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} "
                "(bucket text, big_bytes bigint, small_bytes bigint)"
            )
            conn.exec_driver_sql(f"DELETE FROM {SCHEMA}.{TABLE}")
            conn.exec_driver_sql(
                f"INSERT INTO {SCHEMA}.{TABLE} (bucket, big_bytes, small_bytes) VALUES "
                f"('cold_archive', {LARGE_VALUES[0]}, {SAFE_VALUES[0]}), "
                f"('warm_tier', {LARGE_VALUES[1]}, {SAFE_VALUES[1]})"
            )

        dataset = (
            db.session.query(SqlaTable)
            .filter_by(table_name=TABLE, schema=SCHEMA, database_id=database.id)
            .one_or_none()
        )
        if dataset is None:
            dataset = SqlaTable(
                table_name=TABLE,
                schema=SCHEMA,
                catalog=database.get_default_catalog(),
                database=database,
            )
            db.session.add(dataset)
            db.session.flush()
            dataset.fetch_metadata()

        charts: dict[str, int] = {}
        for key, name, control in (
            ("chart_id", SLICE_NAME, False),
            ("control_chart_id", CONTROL_SLICE_NAME, True),
        ):
            chart = db.session.query(Slice).filter_by(slice_name=name).one_or_none()
            params = json.dumps(chart_params(dataset.id, control=control))
            if chart is None:
                chart = Slice(
                    slice_name=name,
                    viz_type="table",
                    datasource_type="table",
                    datasource_id=dataset.id,
                    params=params,
                )
                db.session.add(chart)
            else:
                chart.params = params
            db.session.flush()
            charts[key] = chart.id
        db.session.commit()

        print(
            json.dumps(
                {
                    "dataset_id": dataset.id,
                    **charts,
                    "route": f"/explore/?slice_id={charts['chart_id']}",
                    "control_route": f"/explore/?slice_id={charts['control_chart_id']}",
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
