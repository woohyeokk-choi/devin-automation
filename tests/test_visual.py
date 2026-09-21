"""The presentation scenario has to stay a demonstration, and stay honest."""

from __future__ import annotations

from portal.visual import INTENDED_ROW_LIMIT, observe

DESCENDING = False


def scenario(
    before: tuple[int, int, bool | None],
    after: tuple[int, int, bool | None],
    rows: int = 600,
    requested_ascending: bool = DESCENDING,
):
    reads = iter([before, after])
    return observe(
        "PREVIEW (unmerged)",
        read=lambda: next(reads),
        sort_only=lambda: None,
        available_rows=rows,
        requested_ascending=requested_ascending,
    )


def test_a_lost_limit_that_grows_the_table_is_what_the_demo_shows() -> None:
    seen = scenario((INTENDED_ROW_LIMIT, 10, True), (1000, 600, False))
    verdict = seen.verdict(repaired=False)
    assert verdict["outcome"] == "demonstrated"
    assert verdict["observed"]["after"]["rendered_rows"] == 600
    assert verdict["observed"]["requested_order_applied"] is True
    assert "not a registered verification case" in verdict["scope"]


def test_a_lost_limit_nobody_can_see_is_not_a_demonstration() -> None:
    """The canonical chart's four regions are exactly this case."""
    seen = scenario((INTENDED_ROW_LIMIT, 4, True), (1000, 4, False), rows=4)
    assert seen.verdict(repaired=False)["outcome"] == "inconclusive"


def test_a_short_table_is_inconclusive_rather_than_passing() -> None:
    seen = scenario((INTENDED_ROW_LIMIT, 10, True), (10, 10, False), rows=10)
    assert seen.verdict(repaired=True)["outcome"] == "inconclusive"


def test_a_merged_deployment_has_to_hold_both_the_limit_and_the_table() -> None:
    held = scenario((INTENDED_ROW_LIMIT, 10, True), (INTENDED_ROW_LIMIT, 10, False))
    assert held.verdict(repaired=True)["outcome"] == "stable"

    slipped = scenario((INTENDED_ROW_LIMIT, 10, True), (INTENDED_ROW_LIMIT, 600, False))
    assert slipped.verdict(repaired=True)["outcome"] == "regressed"


def test_a_merged_deployment_that_ignores_the_requested_order_is_not_stable() -> None:
    """Holding the limit is only half of it: the user asked for a sort."""
    ignored = scenario((INTENDED_ROW_LIMIT, 10, True), (INTENDED_ROW_LIMIT, 10, True))
    verdict = ignored.verdict(repaired=True)
    assert verdict["outcome"] == "regressed"
    assert verdict["observed"]["requested_order_applied"] is False


def test_the_baseline_verdict_never_reads_as_a_pass_on_the_merged_question() -> None:
    """A demonstration of the bug must not be mistaken for an accepted fix."""
    seen = scenario((INTENDED_ROW_LIMIT, 10, True), (1000, 600, False))
    assert seen.verdict(repaired=False)["outcome"] != "stable"
    assert seen.verdict(repaired=True)["outcome"] == "regressed"
