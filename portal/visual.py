"""The presentation scenario: the same defect, in Superset's own chart.

The canonical S1 incident is about a saved setting — `row_limit` 137 becoming
1000 — and it is verified by reading the chart back. It groups by `region`,
which has four values, so a customer watching the table sees nothing move: the
regression is real and invisible. This module runs the same omitted-row-limit
path against a chart built to make it visible: a raw-records table over
`synthetic_orders` whose saved limit of 10 turns into the whole table.

Raw records, not an aggregate top-N, for a reason. In aggregate mode the
table plugin rebuilds `orderby` from the metric (`buildQuery` uses
`timeseries_limit_metric`/`order_desc`, and the control panel only offers
`order_by_cols` in raw mode), so the sort the caller asked for never reaches
the rendered chart. That is a separate, unrepaired mapping mismatch, and
demonstrating the row-limit defect on top of it would imply this pull request
fixes it. In raw mode the requested ordering does apply, so the only thing
the demonstration attributes to the defect is the lost limit.

It is deliberately separate from `portal.scenarios`: the verdict here is about
what a demonstration shows, and it must never stand in for the registered
cases a candidate is accepted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

#: The presentation chart's intended shape. `row_limit` is small enough that
#: losing it is unmistakable, and the table is long enough to prove it.
CHART_NAME = "Order revenue - 10-row view (presentation)"
QUERY_MODE = "raw"
COLUMNS = ("id", "region", "channel", "product", "revenue")
SORT_COLUMN = "revenue"
INTENDED_ROW_LIMIT = 10

#: Below this the demonstration proves nothing: the table has to be able to
#: grow past its limit for a lost limit to be visible at all.
MIN_ROWS = INTENDED_ROW_LIMIT + 1


class Chart(Protocol):
    """The three chart facts this scenario reads back."""

    def row_limit(self) -> int: ...

    def rendered_rows(self) -> int: ...

    def ascending(self) -> bool | None: ...


@dataclass
class VisualObservation:
    """What a viewer could actually see, before and after the sort action."""

    label: str
    before_row_limit: int
    before_rows: int
    after_row_limit: int
    after_rows: int
    available_rows: int
    requested_ascending: bool
    after_ascending: bool | None
    notes: list[str] = field(default_factory=list)

    @property
    def visible(self) -> bool:
        """Whether the difference is one a customer can see in the table."""
        return self.after_rows != self.before_rows

    @property
    def sort_applied(self) -> bool:
        """Whether the ordering the caller asked for is the one rendered."""
        return self.after_ascending == self.requested_ascending

    def verdict(self, *, repaired: bool) -> dict[str, Any]:
        """A labelled verdict, never phrased as a registered case result.

        `repaired` says which deployment this ran against, and the two
        deployments have opposite expectations: on a baseline the row limit
        is expected to be lost, and on a merged fix it is expected to hold
        while the requested ordering still applies.
        """
        if self.available_rows < MIN_ROWS:
            outcome = "inconclusive"
            detail = (
                f"the table offers {self.available_rows} rows, "
                f"so a limit of {INTENDED_ROW_LIMIT} cannot visibly overflow"
            )
        elif repaired:
            held = (
                self.after_row_limit == self.before_row_limit == INTENDED_ROW_LIMIT
                and self.after_rows == self.before_rows == INTENDED_ROW_LIMIT
                and self.sort_applied
            )
            outcome = "stable" if held else "regressed"
            detail = (
                "the saved limit and the rendered table survived the sort-only "
                "update, and the requested order applied"
                if held
                else (
                    f"limit {self.before_row_limit} -> {self.after_row_limit}, "
                    f"rows {self.before_rows} -> {self.after_rows}, "
                    f"requested order applied: {self.sort_applied}"
                )
            )
        else:
            shown = self.before_row_limit != self.after_row_limit and self.visible
            outcome = "demonstrated" if shown else "not_demonstrated"
            detail = (
                f"limit {self.before_row_limit} -> {self.after_row_limit}, "
                f"rows {self.before_rows} -> {self.after_rows}"
            )
        return {
            "check": "visual_presentation_scenario",
            "scope": "demonstration only — not a registered verification case",
            "chart": CHART_NAME,
            "query_mode": QUERY_MODE,
            "intended_row_limit": INTENDED_ROW_LIMIT,
            "available_rows": self.available_rows,
            "outcome": outcome,
            "detail": detail,
            "label": self.label,
            "observed": {
                "before": {
                    "row_limit": self.before_row_limit,
                    "rendered_rows": self.before_rows,
                },
                "after": {
                    "row_limit": self.after_row_limit,
                    "rendered_rows": self.after_rows,
                    "ascending": self.after_ascending,
                },
                "requested_ascending": self.requested_ascending,
                "requested_order_applied": self.sort_applied,
            },
            "notes": list(self.notes),
        }


def observe(
    label: str,
    read: Callable[[], tuple[int, int, bool | None]],
    sort_only: Callable[[], None],
    available_rows: int,
    requested_ascending: bool,
    notes: list[str] | None = None,
) -> VisualObservation:
    """Read the chart, apply the sort-only change, and read it back.

    `read` returns the saved row limit, the rendered row count and the
    rendered sort direction. `sort_only` must send no row limit at all:
    supplying one would be a different user action, and the defect under
    demonstration is what the server does with the setting the caller left
    out.
    """
    before_limit, before_rows, _ = read()
    sort_only()
    after_limit, after_rows, after_ascending = read()
    return VisualObservation(
        label=label,
        before_row_limit=before_limit,
        before_rows=before_rows,
        after_row_limit=after_limit,
        after_rows=after_rows,
        available_rows=available_rows,
        requested_ascending=requested_ascending,
        after_ascending=after_ascending,
        notes=list(notes or []),
    )
