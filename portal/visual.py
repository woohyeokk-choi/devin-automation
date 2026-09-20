"""The presentation scenario: the same defect, in Superset's own chart.

The canonical S1 incident is about a saved setting — `row_limit` 137 becoming
1000 — and it is verified by reading the chart back. It groups by `region`,
which has four values, so a customer watching the table sees nothing move: the
regression is real and invisible. This module runs the same omitted-row-limit
path against a chart built to make it visible, a top-N table over
region x channel x product, where a lost limit of 10 turns into every group.

It is deliberately separate from `portal.scenarios`: the verdict here is about
what a demonstration shows, and it must never stand in for the registered
cases a candidate is accepted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

#: The presentation chart's intended shape. `row_limit` is small enough that
#: losing it is unmistakable, and the grouping is wide enough to prove it.
CHART_NAME = "Top 10 sales segments (presentation)"
DIMENSIONS = ("region", "channel", "product")
METRIC = "SUM(revenue)"
INTENDED_ROW_LIMIT = 10

#: Below this the demonstration proves nothing: the table has to be able to
#: grow past its limit for a lost limit to be visible at all.
MIN_GROUPS = INTENDED_ROW_LIMIT + 1


class Chart(Protocol):
    """The two chart facts this scenario reads back."""

    def row_limit(self) -> int: ...

    def rendered_rows(self) -> int: ...


@dataclass
class VisualObservation:
    """What a viewer could actually see, before and after the sort action."""

    label: str
    before_row_limit: int
    before_rows: int
    after_row_limit: int
    after_rows: int
    available_groups: int
    notes: list[str] = field(default_factory=list)

    @property
    def visible(self) -> bool:
        """Whether the difference is one a customer can see in the table."""
        return self.after_rows != self.before_rows

    def verdict(self, *, repaired: bool) -> dict[str, Any]:
        """A labelled verdict, never phrased as a registered case result.

        `repaired` says which deployment this ran against, and the two
        deployments have opposite expectations: on a baseline the row limit
        is expected to be lost, and on a merged fix it is expected to hold.
        """
        if self.available_groups < MIN_GROUPS:
            outcome = "inconclusive"
            detail = (
                f"the dataset offers {self.available_groups} groups, "
                f"so a limit of {INTENDED_ROW_LIMIT} cannot visibly overflow"
            )
        elif repaired:
            held = (
                self.after_row_limit == self.before_row_limit == INTENDED_ROW_LIMIT
                and self.after_rows == self.before_rows == INTENDED_ROW_LIMIT
            )
            outcome = "stable" if held else "regressed"
            detail = (
                "the saved limit and the rendered table were unchanged by the "
                "sort-only update"
                if held
                else (
                    f"limit {self.before_row_limit} -> {self.after_row_limit}, "
                    f"rows {self.before_rows} -> {self.after_rows}"
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
            "intended_row_limit": INTENDED_ROW_LIMIT,
            "available_groups": self.available_groups,
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
                },
            },
            "notes": list(self.notes),
        }


def observe(
    label: str,
    read: Callable[[], tuple[int, int]],
    sort_only: Callable[[], None],
    available_groups: int,
    notes: list[str] | None = None,
) -> VisualObservation:
    """Read the chart, apply the sort-only change, and read it back.

    `sort_only` must send no row limit at all: supplying one would be a
    different user action, and the defect under demonstration is what the
    server does with the setting the caller left out.
    """
    before_limit, before_rows = read()
    sort_only()
    after_limit, after_rows = read()
    return VisualObservation(
        label=label,
        before_row_limit=before_limit,
        before_rows=before_rows,
        after_row_limit=after_limit,
        after_rows=after_rows,
        available_groups=available_groups,
        notes=list(notes or []),
    )
