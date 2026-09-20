"""The presentation scenario has to stay a demonstration, and stay honest."""

from __future__ import annotations

from portal.visual import INTENDED_ROW_LIMIT, observe


def scenario(before: tuple[int, int], after: tuple[int, int], groups: int = 60):
    reads = iter([before, after])
    return observe(
        "PREVIEW (unmerged)",
        read=lambda: next(reads),
        sort_only=lambda: None,
        available_groups=groups,
    )


def test_a_lost_limit_that_grows_the_table_is_what_the_demo_shows() -> None:
    seen = scenario((INTENDED_ROW_LIMIT, 10), (1000, 60))
    verdict = seen.verdict(repaired=False)
    assert verdict["outcome"] == "demonstrated"
    assert verdict["observed"]["after"]["rendered_rows"] == 60
    assert "not a registered verification case" in verdict["scope"]


def test_a_lost_limit_nobody_can_see_is_not_a_demonstration() -> None:
    """The canonical chart's four regions are exactly this case."""
    seen = scenario((INTENDED_ROW_LIMIT, 4), (1000, 4), groups=4)
    assert seen.verdict(repaired=False)["outcome"] == "inconclusive"


def test_a_narrow_dataset_is_inconclusive_rather_than_passing() -> None:
    seen = scenario((INTENDED_ROW_LIMIT, 10), (10, 10), groups=10)
    assert seen.verdict(repaired=True)["outcome"] == "inconclusive"


def test_a_merged_deployment_has_to_hold_both_the_limit_and_the_table() -> None:
    held = scenario((INTENDED_ROW_LIMIT, 10), (INTENDED_ROW_LIMIT, 10))
    assert held.verdict(repaired=True)["outcome"] == "stable"

    slipped = scenario((INTENDED_ROW_LIMIT, 10), (INTENDED_ROW_LIMIT, 60))
    assert slipped.verdict(repaired=True)["outcome"] == "regressed"


def test_the_baseline_verdict_never_reads_as_a_pass_on_the_merged_question() -> None:
    """A demonstration of the bug must not be mistaken for an accepted fix."""
    seen = scenario((INTENDED_ROW_LIMIT, 10), (1000, 60))
    assert seen.verdict(repaired=False)["outcome"] != "stable"
    assert seen.verdict(repaired=True)["outcome"] == "regressed"
