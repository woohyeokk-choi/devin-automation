"""Qualification of real browser telemetry into admissible product evidence.

The monitor (`portal.monitor`) drives its own managed browser through an
ordinary user route and writes down what the *product* logged. That raw list is
untrusted data: it is page-controlled text, it is noisy, and most of it is not a
product defect. This module is the seam that decides, on the server side, which
of it may ever become an incident.

Three outcomes, all of them visible:

``qualified``
    The entry matches a registered :class:`BrowserSignature` — a named product
    defect with the severity the product really emits. Only these can become an
    incident.
``needs_attention``
    A warning or error nobody has registered. Recorded and surfaced so an
    operator sees it, never dispatched: an unrecognised log line is not a
    diagnosed defect.
``ignored``
    Deployment noise on a known list (a missing service worker, a preload hint)
    and ordinary info/log chatter.

Nothing here asserts an expected value. The runtime signal is the product's own
console output; correctness assertions live in the validator and are reported
separately, so a passing or failing replay can never manufacture a trigger.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .redaction import scrub, scrub_text

#: Severities the product can emit that are worth reasoning about at all.
ATTENTION_SEVERITIES = frozenset({"warning", "error"})

#: Bounds on one scan. A page can log without limit; the store may not.
MAX_ENTRIES_PER_SCAN = 200
MAX_TEXT = 1500
MAX_STACK_FRAMES = 5

QUALIFIED, NEEDS_ATTENTION, IGNORED = "qualified", "needs_attention", "ignored"


@dataclass(frozen=True)
class BrowserSignature:
    """One registered, reproduced product defect as the browser reports it.

    `severity` is the severity the product *actually* emits. An entry whose
    severity differs is not this defect: labelling a warning as an error (or the
    reverse) would misreport the symptom, so it falls through to
    ``needs_attention`` instead of matching.
    """

    family: str
    scenario: str
    severity: str
    #: Every fragment must appear in the message. Deliberately the product's own
    #: wording plus the underlying error, so a similar-looking unrelated failure
    #: does not borrow this family's diagnosis.
    contains: tuple[str, ...]
    symptom: str
    #: Fragments that must appear in the captured stack/module context, if any.
    module_contains: tuple[str, ...] = ()


#: Registered signatures. Adding one is a deliberate act that says "this
#: symptom has been reproduced on the baseline and is understood well enough to
#: ask for a repair".
SIGNATURES: tuple[BrowserSignature, ...] = (
    BrowserSignature(
        family="bigint_number_format_not_applied",
        scenario="B1",
        # Reproduced three times on 394bca55: the table plugin catches the
        # formatter's TypeError and falls back to the raw value, so the product
        # emits a warning. It is not an uncaught exception and not a crash.
        severity="warning",
        contains=(
            "Formatter failed, falling back to raw value",
            "Cannot convert a BigInt value to a number",
        ),
        symptom=(
            "A table column with a memory number format shows raw digits for "
            "values beyond the JavaScript safe-integer range, while the same "
            "format renders correctly for smaller values."
        ),
    ),
)

#: Console output that says something about the deployment, not the product.
#: Matching here is what keeps the needs-attention list small enough to read.
KNOWN_NOISE: tuple[re.Pattern[str], ...] = (
    re.compile(r"service-?worker", re.I),
    re.compile(r"was preloaded using link preload but not used", re.I),
    # The browser reports the failed service-worker fetch twice, once as a
    # registration failure and once as this bare line.
    re.compile(r"bad HTTP response code \(404\) was received when fetching the script", re.I),
    re.compile(r"favicon", re.I),
    re.compile(r"DevTools failed to load", re.I),
)


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _module_context(entry: Mapping[str, Any]) -> str:
    """Where the log came from, without the query string that carries state.

    Asset URLs are kept (the chunk name is genuinely useful when reading a
    stack) but everything after `?` is dropped: exploration keys and cache
    busters are not diagnostic and do not belong in stored evidence.
    """
    location = entry.get("location")
    url = ""
    if isinstance(location, Mapping):
        url = _clip(location.get("url") or "", 300)
    return url.split("?", 1)[0]


def _stack_summary(text: str) -> list[str]:
    """The top frames of a stack, function and module only."""
    frames = []
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped.startswith("at "):
            continue
        frames.append(scrub_text(stripped.split("?", 1)[0])[:200])
        if len(frames) >= MAX_STACK_FRAMES:
            break
    return frames


def match(severity: str, message: str, module: str = "") -> BrowserSignature | None:
    for signature in SIGNATURES:
        if severity != signature.severity:
            continue
        if not all(fragment in message for fragment in signature.contains):
            continue
        if signature.module_contains and not all(
            fragment in module for fragment in signature.module_contains
        ):
            continue
        return signature
    return None


def signature_for(family: str) -> BrowserSignature | None:
    return next((s for s in SIGNATURES if s.family == family), None)


@dataclass(frozen=True)
class Finding:
    """One consolidated group of console entries from a single scan.

    A chart re-render logs the same warning once per affected cell, so the
    monitor would otherwise report "8 defects" for one broken column. The burst
    is folded here, before persistence: one finding, with the count it was seen.
    """

    classification: str
    severity: str
    message: str
    module: str
    stack: tuple[str, ...]
    count: int
    family: str = ""
    scenario: str = ""
    symptom: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "severity": self.severity,
            "message": self.message,
            "module": self.module,
            "stack": list(self.stack),
            "count": self.count,
            "family": self.family,
            "scenario": self.scenario,
            "symptom": self.symptom,
            "reason": self.reason,
        }


@dataclass
class ScanSummary:
    findings: list[Finding] = field(default_factory=list)
    entries_seen: int = 0
    entries_kept: int = 0
    rejected: list[str] = field(default_factory=list)

    @property
    def qualified(self) -> list[Finding]:
        return [f for f in self.findings if f.classification == QUALIFIED]

    @property
    def needs_attention(self) -> list[Finding]:
        return [f for f in self.findings if f.classification == NEEDS_ATTENTION]

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries_seen": self.entries_seen,
            "entries_kept": self.entries_kept,
            "rejected": self.rejected[:MAX_STACK_FRAMES],
            "findings": [f.as_dict() for f in self.findings],
            "qualified": len(self.qualified),
            "needs_attention": len(self.needs_attention),
        }


def _classify(severity: str, message: str, module: str) -> tuple[str, BrowserSignature | None, str]:
    signature = match(severity, message, module)
    if signature is not None:
        return QUALIFIED, signature, ""
    haystack = f"{message} {module}"
    if any(pattern.search(haystack) for pattern in KNOWN_NOISE):
        return IGNORED, None, "known deployment noise"
    if severity in ATTENTION_SEVERITIES:
        return NEEDS_ATTENTION, None, "no registered signature for this message"
    return IGNORED, None, f"severity:{severity}"


def qualify(entries: Iterable[Any]) -> ScanSummary:
    """Fold one scan's raw console entries into bounded, sanitized findings.

    Input is treated as hostile: anything that is not a well-formed entry is
    counted and dropped with a reason rather than stored, and the whole scan is
    capped so a chatty page cannot fill the store.
    """
    summary = ScanSummary()
    groups: dict[str, dict[str, Any]] = {}
    for raw in entries:
        summary.entries_seen += 1
        if summary.entries_kept >= MAX_ENTRIES_PER_SCAN:
            summary.rejected.append("scan entry cap reached")
            continue
        if not isinstance(raw, Mapping):
            summary.rejected.append("entry is not an object")
            continue
        severity = _clip(raw.get("severity") or "", 32).strip().lower()
        text = raw.get("text")
        if not severity or not isinstance(text, str) or not text.strip():
            summary.rejected.append("entry without a severity or message")
            continue
        summary.entries_kept += 1

        module = scrub_text(_module_context(raw))
        # `scrub` first, clip second: the credential patterns must run over the
        # whole string before truncation can split one in half.
        message = scrub_text(_clip(text))
        headline = message.splitlines()[0][:400]
        stack = tuple(_stack_summary(message))
        classification, signature, reason = _classify(severity, message, module)

        key = "|".join([classification, severity, headline, module])
        group = groups.setdefault(
            key,
            {
                "classification": classification,
                "severity": severity,
                "message": headline,
                "module": module,
                "stack": stack,
                "count": 0,
                "family": signature.family if signature else "",
                "scenario": signature.scenario if signature else "",
                "symptom": signature.symptom if signature else "",
                "reason": reason,
            },
        )
        group["count"] += 1

    summary.findings = [Finding(**{**g, "stack": tuple(g["stack"])}) for g in groups.values()]
    return summary


def occurrence_fingerprint(family: str, page: Mapping[str, Any]) -> str:
    """Identity of a repeated observation: the defect and where it was seen.

    Scan ids and timestamps are deliberately absent, so tomorrow's scan of the
    same chart is the same occurrence group rather than a new defect.
    """
    material = "\n".join(
        [family, str(page.get("chart") or ""), str(page.get("route") or "")]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def telemetry_event(
    finding: Finding,
    *,
    page: Mapping[str, Any],
    requests: Iterable[Mapping[str, Any]] = (),
    scan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The sanitized `telemetry` event body an incident can be built from.

    Request context is deliberately thin — method, path without its query
    string, and status — because the point is to record that the page's data
    call succeeded (this defect is a rendering failure, not an HTTP failure),
    not to keep a HAR.
    """
    context = [
        {
            "method": _clip(item.get("method") or "", 8),
            "path": _clip(str(item.get("path") or "").split("?", 1)[0], 200),
            "status": int(item.get("status") or 0),
        }
        for item in list(requests)[:10]
        if isinstance(item, Mapping)
    ]
    return scrub(
        {
            "severity": finding.severity,
            "family": finding.family,
            "scenario": finding.scenario,
            "classification": finding.classification,
            "symptom": finding.symptom,
            "diagnostic": finding.message,
            "stack": list(finding.stack),
            "module": finding.module,
            "occurrences_in_scan": finding.count,
            "page": {
                "chart": _clip(page.get("chart") or "", 200),
                # The route keeps its `slice_id`: which chart was open is the
                # identity of the observation, not incidental query state.
                "route": _clip(page.get("route") or "", 200),
                "visible_values": _clip(page.get("visible_values") or "", 400),
            },
            "requests": context,
            "scan": {
                "monitor": _clip((scan or {}).get("monitor") or "", 64),
                "scan_id": _clip((scan or {}).get("scan_id") or "", 64),
                "read_only": True,
            },
        }
    )
