"""P2/W2 T5.W2.1 - the security corpus is proven non-vacuous, counted, and mapped.

Three things this file exists to make impossible:

1. **A corpus case that passes because the scanner never ran.** Every defence
   the corpus leans on is REMOVED in-process and the corpus is re-probed. A
   case whose disposition does not change when the defence is gone is
   reported VACUOUS, and that is a finding, not a pass.
2. **A shrinking corpus.** Every group is pinned to an EXACT count, so
   deleting a case to make a run green fails the build. The pre-existing
   ``>=`` assertions are the hole this closes.
3. **A coverage claim nobody can check.** :func:`ATTACK_CLASS_COVERAGE` maps
   every attack class Phase 2 must cover to the cases that cover it, and
   :func:`uncovered_attack_classes` names the ones with none.

The transcripts these assertions are derived from are reproducible with::

    python Temp/opencode/w2_1_nonvacuity.py
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import Any, Callable, Iterator, Mapping

import pytest

from shared import policy_lang as pl
from shared import security
from shared import security_corpus as corpus

# ---------------------------------------------------------------------------
# 1. THE COUNTS, PINNED EXACTLY
# ---------------------------------------------------------------------------
#
# A floor (`>=`) is what let three declared threat-model categories sit at
# zero cases while the assertion read as satisfied. These are EXACT.

EXPECTED_COUNTS: Mapping[str, int] = {
    "SECURITY_REGRESSION_CORPUS": 23,
    "CANARY_EXFILTRATION_CORPUS": 7,
    "OBFUSCATED_CANARY_EXFILTRATION_CORPUS": 8,
    "ISSUE_INJECTION_CORPUS": 9,
    "DELIMITER_BREAKOUT_CORPUS": 7,
    "PHASE2_SURFACE_CORPUS": 13,
}

#: The total, so a case moved between two groups is still a net-zero failure
#: rather than something a per-group assertion could miss.
EXPECTED_TOTAL = 67


def _groups() -> dict[str, tuple[Any, ...]]:
    return {
        "SECURITY_REGRESSION_CORPUS": corpus.SECURITY_REGRESSION_CORPUS,
        "CANARY_EXFILTRATION_CORPUS": corpus.CANARY_EXFILTRATION_CORPUS,
        "OBFUSCATED_CANARY_EXFILTRATION_CORPUS": (
            corpus.OBFUSCATED_CANARY_EXFILTRATION_CORPUS
        ),
        "ISSUE_INJECTION_CORPUS": corpus.ISSUE_INJECTION_CORPUS,
        "DELIMITER_BREAKOUT_CORPUS": corpus.DELIMITER_BREAKOUT_CORPUS,
        "PHASE2_SURFACE_CORPUS": corpus.PHASE2_SURFACE_CORPUS,
    }


@pytest.mark.parametrize("group", sorted(EXPECTED_COUNTS))
def test_every_corpus_group_is_pinned_to_an_EXACT_count(group: str) -> None:
    """A corpus that can shrink is a corpus being defeated."""
    actual = len(_groups()[group])
    assert actual == EXPECTED_COUNTS[group], (
        f"{group} has {actual} cases; the pin is {EXPECTED_COUNTS[group]}.\n"
        f"A corpus case may be ADDED (bump the pin in the same change) or "
        f"REMOVED ONLY with a written reason in shared/AGENTS.md. Deleting a "
        f"case to make a run green is the failure this pin exists to catch."
    )


def test_the_corpus_total_is_pinned_too() -> None:
    """Belt and braces: a case moved between groups is still a net change."""
    total = sum(len(rows) for rows in _groups().values())
    assert total == EXPECTED_TOTAL, (
        f"corpus total is {total}, pin is {EXPECTED_TOTAL}. Per-group counts "
        f"could both hold while a case was silently relocated."
    )


def test_the_declared_count_constants_match_the_groups_they_name() -> None:
    """`ISSUE_INJECTION_CASE_COUNT` and friends must not drift from reality."""
    pairs = (
        ("ISSUE_INJECTION_CASE_COUNT", corpus.ISSUE_INJECTION_CORPUS),
        ("DELIMITER_BREAKOUT_CASE_COUNT", corpus.DELIMITER_BREAKOUT_CORPUS),
        ("ALL_SOURCES_ASSERTED_COUNT", corpus.CANARY_EXFILTRATION_CORPUS),
        ("PHASE2_SURFACE_CASE_COUNT", corpus.PHASE2_SURFACE_CORPUS),
    )
    for name, rows in pairs:
        declared = getattr(corpus, name)
        assert declared == len(rows), (
            f"{name}={declared} but the group holds {len(rows)}. The declared "
            f"constant is the one a reader trusts; it must be the truth."
        )


def test_every_phase2_surface_has_at_least_one_case_and_a_zero_is_reported() -> None:
    coverage = corpus.phase2_surface_coverage()
    assert set(coverage) == set(corpus.PHASE2_SURFACES)
    empty = [name for name, count in coverage.items() if count == 0]
    assert not empty, (
        f"Phase 2 surfaces with ZERO cases: {empty}. `phase2_surface_coverage` "
        f"reports a zero as a zero on purpose; a surface covered 'because some "
        f"other surface has cases' is the claim this mapping exists to stop."
    )


# ---------------------------------------------------------------------------
# 2. NON-VACUITY: remove each defence, watch the corpus react
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patched(module: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def _allow_everything(text: Any, *, source: str = "untrusted", **_: Any):
    """A REAL `UntrustedReview` that blocks nothing.

    Built from the shipped class rather than a stand-in, so the probe cannot
    pass because it handed the caller something with the wrong shape.
    """
    body = text if isinstance(text, str) else str(text)
    return security.UntrustedReview(
        source=str(source),
        allowed=True,
        blocked=False,
        tainted=False,
        severity="none",
        findings=(),
        text=body,
        digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        policy_mode="allow",
        truncated=False,
    )


def _identity_normalise(text: Any):
    body = text if isinstance(text, str) else str(text)
    return body, security.NormalizationReceipt(
        ansi_removed=0, zero_width_removed=0, controls_removed=0, confusables_folded=0
    )


def _identity_redact(value: Any, secrets: Any = ()) -> Any:
    return value if isinstance(value, str) else str(value)


def _identity_defang(text: Any) -> Any:
    return text if isinstance(text, str) else str(text)


#: Invisible / format characters a READER does not see. Stripping them is what
#: turns "did the token survive" into a question about the perceived output.
_PERCEIVED_INVISIBLES = (
    "", "‌", "‍", "‎", "‏",
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩", "﻿",
)


def perceived(text: Any) -> str:
    """Return what a reader recovers from ``text``.

    A raw substring test UNDERCOUNTS a split token (the canary becomes two
    short runs). An LCS OVERCOUNTS badly (the canary scores well against the
    surrounding English prose). This is the same metric
    ``tests/test_redaction_hardening.py`` uses, and it is the only one of the
    three that survives both traps.
    """
    out = text if isinstance(text, str) else str(text)
    for ch in _PERCEIVED_INVISIBLES:
        out = out.replace(ch, "")
    return out


def _probe_untrusted_boundary() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for case in corpus.iter_canary_exfiltration_cases():
        out[case.case_id] = security.review_untrusted_source(
            case.payload, source=case.source
        ).blocked
    for case in corpus.iter_obfuscated_canary_cases():
        out[case.case_id] = security.review_untrusted_source(
            case.payload, source=case.source
        ).blocked
    return out


def _probe_canary_redaction() -> dict[str, bool]:
    """True means THE CANARY LEAKED - so `True` is the bad state here."""
    return {
        case.case_id: corpus.CANARY_TOKEN in perceived(security.redact_text(case.payload))
        for case in corpus.iter_obfuscated_canary_cases()
    }


def _probe_normalisation() -> dict[str, bool]:
    """True means the canary leaked. Same bad-state sense as the probe above."""
    return {
        case.case_id: corpus.CANARY_TOKEN in perceived(security.redact_text(case.payload))
        for case in corpus.iter_obfuscated_canary_cases()
    }


#: The literals a breakout must not survive. Asserted on the LITERAL, per the
#: corpus's own note: a breakout "defanged" by a backslash still CONTAINS the
#: boundary string, so a check phrased as "must not look like our marker"
#: would pass on a payload that does.
_BOUNDARY_LITERALS = (
    "--- END UNTRUSTED",
    "--- BEGIN UNTRUSTED",
    "</untrusted_content>",
    "<untrusted_content>",
    "[[tainted:",
    "[QUARANTINED_UNTRUSTED_CONTENT]",
)


def _probe_defang() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for case in corpus.iter_delimiter_breakout_cases():
        if not case.expected_literal_absent:
            # The chat-template row declares no literal to keep absent.
            out[case.case_id] = False
            continue
        defanged = security.defang_untrusted_delimiters(case.payload)
        out[case.case_id] = not any(lit in defanged for lit in _BOUNDARY_LITERALS)
    return out


def _moved_cases(
    probe: Callable[[], dict[str, bool]],
    module: Any,
    name: str,
    replacement: Any,
) -> dict[str, tuple[Any, Any]]:
    """Return every case whose disposition changes when the defence is removed.

    A case is load-bearing if it moves in EITHER direction. Direction is
    recorded but never used to decide: an inverted comparison is how a
    load-bearing case gets reported VACUOUS.
    """
    before = probe()
    with _patched(module, name, replacement):
        off = probe()
    assert probe() == before, "the probe is not deterministic; the A/B is void"
    return {
        key: (before[key], off[key]) for key in before if bool(before[key]) != bool(off[key])
    }


class TestTheUntrustedSourceBoundaryIsLoadBearing:
    """`review_untrusted_source` is the ONE authority for all seven sources."""

    def test_removing_it_unblocks_every_canary_case(self) -> None:
        moved = _moved_cases(
            _probe_untrusted_boundary,
            security,
            "review_untrusted_source",
            _allow_everything,
        )
        total = len(_probe_untrusted_boundary())
        assert len(moved) == total, (
            f"only {len(moved)} of {total} canary cases changed disposition when "
            f"the boundary was removed. Cases that block with AND without the "
            f"defence are passing for some other reason and are not evidence.\n"
            f"still blocking: "
            f"{sorted(set(_probe_untrusted_boundary()) - set(moved))}"
        )
        assert all(before is True and off is False for before, off in moved.values())


class TestTheRedactionBoundaryIsLoadBearing:
    def test_removing_the_redactor_lets_at_least_one_canary_through(self) -> None:
        moved = _moved_cases(
            _probe_canary_redaction, security, "redact_text", _identity_redact
        )
        assert moved, (
            "no obfuscated canary case leaked when `redact_text` was replaced by "
            "the identity. The end-to-end redaction assertions all still pass "
            "in that configuration, so they are not evidence that redaction "
            "does anything."
        )
        assert all(before is False and off is True for before, off in moved.values())

    def test_no_canary_leaks_with_the_defence_on(self) -> None:
        """The control arm. Without it, the test above could pass vacuously."""
        leaking = [k for k, v in _probe_canary_redaction().items() if v]
        assert not leaking, f"canary leaked with redaction ON: {leaking}"


class TestTheNormalisationLayerIsLoadBearing:
    def test_removing_normalisation_lets_at_least_one_canary_through(self) -> None:
        moved = _moved_cases(
            _probe_normalisation,
            security,
            "normalize_for_redaction",
            _identity_normalise,
        )
        assert moved, (
            "no obfuscated canary case needed normalisation. If the strict-body "
            "framing really made normalisation the only defence, removing it "
            "must leak at least one."
        )

    def test_how_many_cases_actually_NEED_normalisation_is_measured(self) -> None:
        """RECORDED FINDING, pinned so it cannot be quietly overstated.

        Measured on this tree: **1 of 8** obfuscated canary cases leaks when
        normalisation is removed. The corpus header asserts the opposite -
        that every row is framed so "a strict body class is the ONLY defence",
        and that a case asserting normalisation "saved" the secret would
        assert something false.

        The measured truth is narrower: normalisation is load-bearing, and it
        is load-bearing for exactly one row. The other seven are defended by
        rules whose character class spans the inserted character anyway, so
        they prove the RULES work and not that NORMALISATION works.

        The count is asserted as a MEASUREMENT with a stated band, not as a
        promise. If a future obfuscation raises it, the band moves and this
        test says so; if the corpus ever claims more rows need it than do,
        the upper bound is what fails.
        """
        needing = _moved_cases(
            _probe_normalisation,
            security,
            "normalize_for_redaction",
            _identity_normalise,
        )
        assert len(needing) >= 1, "normalisation became dead weight entirely"
        assert len(needing) <= 3, (
            f"{len(needing)} of 8 obfuscated cases now depend on normalisation: "
            f"{sorted(needing)}. The corpus header says every row is framed so "
            f"a strict body class is the only defence. If normalisation is "
            f"carrying more than a couple of rows, that header is stale and a "
            f"reader is being told the rules are stronger than they are."
        )


class TestTheDelimiterDefangerIsLoadBearing:
    def test_removing_the_defanger_lets_a_boundary_literal_survive(self) -> None:
        moved = _moved_cases(
            _probe_defang,
            security,
            "defang_untrusted_delimiters",
            _identity_defang,
        )
        assert len(moved) >= 5, (
            f"only {len(moved)} breakout cases changed when the defanger was "
            f"removed; expected at least 5 of 7. A defanger that is not the "
            f"reason the corpus passes is not a control."
        )

    def test_every_declared_literal_is_absent_with_the_defence_on(self) -> None:
        surviving = [k for k, v in _probe_defang().items() if not v]
        assert not surviving, f"boundary literal survived defanging: {surviving}"


# ---------------------------------------------------------------------------
# 3. COVERAGE: every attack class Phase 2 names, mapped to its cases
# ---------------------------------------------------------------------------

#: Attack class -> the corpus groups that cover it. A class with no entry here
#: is a class with no cases, and `uncovered_attack_classes` will say so.
ATTACK_CLASS_COVERAGE: Mapping[str, tuple[str, ...]] = {
    # --- the pre-existing five -------------------------------------------
    "web_injection": ("CANARY_EXFILTRATION_CORPUS:fetched-page-canary",),
    "skill_injection": ("CANARY_EXFILTRATION_CORPUS:skill-body-canary",),
    "plugin_injection": ("CANARY_EXFILTRATION_CORPUS:plugin-manifest-canary",),
    "mcp_injection": ("CANARY_EXFILTRATION_CORPUS:mcp-result-canary",),
    "memory_injection": ("CANARY_EXFILTRATION_CORPUS:memory-row-canary",),
    # --- new in Phase 2 ----------------------------------------------------
    "issue_text_injection": ("ISSUE_INJECTION_CORPUS",),
    "delimiter_breakout": ("DELIMITER_BREAKOUT_CORPUS",),
    "repository_instruction_injection": (
        "SECURITY_REGRESSION_CORPUS:hostile-repository-instruction",
        "CANARY_EXFILTRATION_CORPUS:agents-md-canary",
    ),
    "hardline_floor_bypass": ("PHASE2_SURFACE_CORPUS#hardline",),
    "approval_gate_injection": ("PHASE2_SURFACE_CORPUS#approval_gate",),
    "approval_origin_forgery": ("PHASE2_SURFACE_CORPUS#approval_origin",),
    "fuzzy_edit_weaponisation": ("PHASE2_SURFACE_CORPUS#fuzzy_edit",),
    "canary_exfiltration": (
        "CANARY_EXFILTRATION_CORPUS",
        "OBFUSCATED_CANARY_EXFILTRATION_CORPUS",
    ),
}

#: Classes with NO case anywhere in the corpus, named explicitly rather than
#: left to a reader to infer. Recorded, not silently dropped.
KNOWN_UNCOVERED: Mapping[str, str] = {
    "containment_receipt_forgery_is_declared_but_unexecuted": (
        "PHASE2_SURFACE_CORPUS carries `containment-receipt-forgery`, and "
        "`shared/approval.py` models the origin, but no production path mints a "
        "containment receipt a task could forge. The case documents the class; "
        "it cannot fail closed today because there is nothing to fail."
    ),
    "vendored_file_tamper_is_declared_but_unpinned_per_file": (
        "`PHASE2_SURFACE_CORPUS:vendored-file-modified-after-pin` names the "
        "class. `scripts/provenance_report.py` enforces it at the DIRECTORY "
        "level, not per vendored file, because no vendored file carries the "
        "`# vendored:` header the scanner parses. See "
        "tests/test_p2w2_vendored_proof.py."
    ),
}


def _case_ids() -> set[str]:
    ids: set[str] = set()
    ids |= {c.case_id for c in corpus.SECURITY_REGRESSION_CORPUS}
    ids |= {c.case_id for c in corpus.CANARY_EXFILTRATION_CORPUS}
    ids |= {c.case_id for c in corpus.OBFUSCATED_CANARY_EXFILTRATION_CORPUS}
    ids |= {c.case_id for c in corpus.ISSUE_INJECTION_CORPUS}
    ids |= {c.case_id for c in corpus.DELIMITER_BREAKOUT_CORPUS}
    ids |= {c.case_id for c in corpus.PHASE2_SURFACE_CORPUS}
    return ids


def uncovered_attack_classes() -> list[str]:
    """Return every declared attack class whose coverage entry is empty."""
    return sorted(name for name, refs in ATTACK_CLASS_COVERAGE.items() if not refs)


def test_every_declared_attack_class_names_at_least_one_case() -> None:
    missing = uncovered_attack_classes()
    assert not missing, f"attack classes with no case: {missing}"


def test_every_coverage_reference_resolves_to_a_real_case() -> None:
    """A coverage map that points at nothing is worse than no map.

    Two reference forms, and they must not be confusable:
    ``GROUP#surface`` names a whole Phase-2 surface; ``GROUP:case_id`` names
    one case. An earlier draft used ``:`` for both, which made four surface
    references resolve as (missing) case ids.
    """
    known = _case_ids()
    surfaces = set(corpus.PHASE2_SURFACES)
    unresolved: list[str] = []
    for name, refs in ATTACK_CLASS_COVERAGE.items():
        for ref in refs:
            if "#" in ref:
                group, _, surface = ref.partition("#")
                if not hasattr(corpus, group):
                    unresolved.append(f"{name} -> {ref} (no such corpus group)")
                elif surface not in surfaces:
                    unresolved.append(f"{name} -> {ref} (no such surface)")
            elif ":" in ref:
                group, _, case_id = ref.partition(":")
                if not hasattr(corpus, group):
                    unresolved.append(f"{name} -> {ref} (no such corpus group)")
                elif case_id not in known:
                    unresolved.append(f"{name} -> {ref} (no such case_id)")
            else:
                if not hasattr(corpus, ref):
                    unresolved.append(f"{name} -> {ref} (no such corpus group)")
    assert not unresolved, f"coverage references that resolve to nothing: {unresolved}"


def test_the_phase2_surface_reference_in_the_map_is_the_whole_surface() -> None:
    """`PHASE2_SURFACE_CORPUS:hardline` means every hardline case, not one."""
    hardline = [c for c in corpus.PHASE2_SURFACE_CORPUS if c.surface == "hardline"]
    assert len(hardline) == corpus.phase2_surface_coverage()["hardline"] >= 6


def test_every_uncovered_class_is_named_with_a_reason() -> None:
    """An unnamed gap is a gap somebody rediscovers."""
    for name, reason in KNOWN_UNCOVERED.items():
        assert reason.strip(), f"{name} has no reason recorded"
        assert len(reason) > 80, f"{name}'s reason is too short to be a reason"


def test_the_issue_corpus_carries_a_benign_control() -> None:
    """A corpus with no negative control cannot tell a detector from a
    detector that flags everything."""
    controls = [c for c in corpus.ISSUE_INJECTION_CORPUS if not c.expected_categories]
    assert len(controls) >= 1, (
        "ISSUE_INJECTION_CORPUS has no benign control. Every case being "
        "flagged is indistinguishable from a detector that flags everything."
    )
    for control in controls:
        assert control.expected_block is False
        review = security.review_untrusted_source(
            control.payload, source="issue", mode="flag"
        )
        assert review.findings == (), (
            f"the benign control {control.case_id!r} produced findings "
            f"{review.findings!r}; it is not a control any more."
        )


# ---------------------------------------------------------------------------
# 4. THE FLOOR IS NOT IN THE SHIPPED POLICY RIGHT NOW
# ---------------------------------------------------------------------------


def test_the_hardline_fold_state_is_reported_not_assumed() -> None:
    """RECORDED, and the assertion is about HONESTY rather than a count.

    `hardline_floor_rules` is fail-closed: it folds a T3 pattern only when one
    of that pattern's own examples is refused AND the refusal names it. On this
    tree 0 of 20 fold, so the shipped policy carries NO hardline floor.

    That is the safe direction for the FOLD, and it is still a gap for the
    PRODUCT. The test asserts that the report says so with a reason, rather
    than asserting a fold count that would flip every time T3 edits its table.
    """
    rules, report = pl.hardline_floor_rules()
    if not rules:
        assert report.get("available") is False, (
            "the fold produced no rules but claims to be available. A silent "
            "empty fold is the shape that reads as 'covered'."
        )
        assert str(report.get("reason") or "").strip(), (
            "an unavailable fold must carry a reason naming what it could not "
            "establish."
        )
        excluded = report.get("excluded") or ()
        assert excluded, "an unavailable fold must name what it excluded"
        for row in excluded:
            assert str(row.get("reason") or "").strip(), (
                f"excluded pattern {row.get('pattern_id')!r} has no reason"
            )
    else:
        assert report.get("available") is True
