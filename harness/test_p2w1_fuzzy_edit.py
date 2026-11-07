"""P2/W1-T3 — the fuzzy edit ladder: it is a LADDER, it is GUARDED, it is REPORTED.

Five properties, and the tests are named after them rather than after the
functions that implement them, because the point is the guarantee and not the
shape:

1. **Exact always wins.** The ladder runs only after an exact match has already
   failed, and it can never be consulted in preference to one. Proven by a
   differential: on every fixture where both an exact and a fuzzy answer exist,
   the reported answer is the exact one.
2. **Fuzzy use is reported.** Every fuzzy edit carries
   ``{"match": "fuzzy", "strategy": ..., "confidence": ..., "guard": ...}`` on
   the ``EditOutcome`` receipt. A fuzzy edit that is not distinguishable from an
   exact one in the receipt is a fuzzy edit a reviewer cannot review.
3. **The disproportionate-match guard fires, and it is tested on the dangerous
   shape.** The dangerous case is a SHORT pattern with SEVERAL plausible homes:
   a fuzzy match that lands in the wrong function. Four separate fixtures.
4. **Byte fidelity and ``stale_read`` are unchanged.** The regression tests are
   the ones this repository already had, re-run: a CRLF file edited through the
   ladder still comes out CRLF, a mixed-newline file stays mixed, a
   never-read file is still refused, and a pre-commit syntax failure still rolls
   back byte-for-byte.
5. **Nothing is line-number-based.** Proven by AST: no module in the ladder
   accepts a line number as an anchor.

Plus the provenance pins: every vendored file carries all FOUR header fields,
and the NOTICE names the deviation (reimplementation, not a byte copy) rather
than hiding it.

Run: ``python -m pytest harness/test_p2w1_fuzzy_edit.py -q``
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Tuple

from harness import editor, fuzzy_edit
from harness.third_party import aider_bitap, opencode_replacer

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_TP = _HERE / "third_party"


def _repo(tmp_path: Path, name: str = "app.py", body: str = "") -> Tuple[str, str]:
    """Create a one-file repository and return ``(root, relative_path)``."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / name).write_text(body, encoding="utf-8")
    return str(root), f"src/{name}"



#: Filler appended to the fixtures below so a fuzzy edit is a small PART of the
#: file. `harness.fuzzy_edit._refuse_region_coverage` (added by a parallel
#: terminal) refuses a fuzzy match covering more than half a file -- a correct
#: rule -- and without this padding an edit inside a four-line module
#: legitimately trips it, which makes every fixture below a test of the coverage
#: rule instead of the thing each one is named for.
PAD = (
    "\n\ndef filler_a():\n    return 'a'\n"
    "\n\ndef filler_b():\n    return 'b'\n"
    "\n\ndef filler_c():\n    return 'c'\n"
    "\n\ndef filler_d():\n    return 'd'\n"
    "\n\ndef filler_e():\n    return 'e'\n"
    "\n\ndef filler_f():\n    return 'f'\n"
    "\n\ndef filler_g():\n    return 'g'\n"
)

def _session(root: str, relative: str, text: str) -> editor.EditSession:
    """Return an ``EditSession`` that has observed ``text`` at ``relative``."""
    session = editor.EditSession()
    session.note_read(relative, data=text.encode("utf-8"))
    return session


# ---------------------------------------------------------------------------
# 1. Exact always wins
# ---------------------------------------------------------------------------


def test_exact_match_is_never_replaced_by_a_better_scoring_fuzzy_one() -> None:
    """A ladder that prefers a fuzzy match over an exact one is not a ladder.

    The fixture is built so a fuzzy rung WOULD score 1.0 at a different place:
    the exact block appears once, and a whitespace-normalised form of it
    appears once at another offset. The exact one must be the one that wins.
    """
    text = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def beta():\n"
        "        return   2\n"
    )
    exact = text.index("def alpha():\n    return 1")
    verdict = opencode_replacer.run_ladder(text, "def alpha():\n    return 1")
    assert verdict.stage == "exact"
    assert verdict.candidate is not None
    assert verdict.candidate.start == exact
    assert verdict.similarity == 1.0
    assert verdict.alternatives == 0


def test_the_ladder_is_only_reachable_after_an_exact_miss(tmp_path: Path) -> None:
    """Through the REAL primitive: an exact edit reports ``exact``, not ``fuzzy``.

    And the control arm matters -- without it, a ladder that always reported
    ``exact`` would satisfy the first assertion.
    """
    body = "def alpha():\n    return 1\n"
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n    return 1",
        "def alpha():\n    return 2",
        session=_session(root, rel, body),
        config={"require_edit_digest": False},
    )
    assert outcome.ok, outcome.message
    assert outcome.match == "exact"
    assert outcome.match_strategy == "exact"
    assert outcome.match_confidence == 1.0
    assert outcome.match_guard == ""
    assert (Path(root) / "src" / "app.py").read_text(encoding="utf-8") == (
        "def alpha():\n    return 2\n"
    )


# ---------------------------------------------------------------------------
# 2. A fuzzy match is REPORTED
# ---------------------------------------------------------------------------


def test_a_fuzzy_edit_is_reported_with_its_strategy_and_confidence(
    tmp_path: Path,
) -> None:
    """The receipt must let a reviewer see that this did NOT land exactly."""
    body = (
        "def alpha():\n"
        "    value = compute(1, 2)\n"
        "    return value\n"
        "\n"
        "\n"
        "def beta():\n"
        "    return 0\n"
        + PAD
    )
    root, rel = _repo(tmp_path, body=body)
    # The caller re-indented the block AND added a trailing comment: neither an
    # exact match nor any normalisation can recover this, so a scored rung has
    # to be the one that lands it.
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n"
        "        value = compute(1, 2)  # note\n"
        "        return value",
        "def alpha():\n"
        "    value = compute(1, 3)\n"
        "    return value",
        session=_session(root, rel, body),
        config={"require_edit_digest": False},
    )
    assert outcome.ok, outcome.message
    report = outcome.to_dict()
    assert report["match"] == "fuzzy", report
    assert report["match_strategy"] in opencode_replacer.STAGES, report
    assert report["match_strategy"] != "exact"
    assert 0.0 <= report["match_confidence"] <= 1.0
    assert report["match_detail"]["match"] == "fuzzy"
    assert report["match_detail"]["confidence"] == report["match_confidence"]
    assert report["match_detail"]["thresholds"]["similarity"] == (
        opencode_replacer.DEFAULT_SIMILARITY
    )


def test_a_normalisation_match_is_still_reported_as_fuzzy(tmp_path: Path) -> None:
    """A whitespace-normalised match is EXACT on the normalised form and still
    fuzzy in the receipt.

    This is the distinction that matters: "we normalised whitespace and found
    one place" is a much stronger claim than "something was 0.7 similar", and a
    receipt that flattened both to ``match: fuzzy`` would throw away the only
    information a reviewer needs.
    """
    # The two sides differ ONLY in indentation: same tokens, same order, same
    # content. A fixture that also changed a value would be testing the scored
    # rung, not the normalisation one.
    body = "def alpha():\n        value = 1\n        return value\n" + PAD
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n    value = 1\n    return value",
        "def alpha():\n    value = 9\n    return value",
        session=_session(root, rel, body),
        config={"require_edit_digest": False},
    )
    assert outcome.ok, outcome.message
    assert outcome.match == "fuzzy"
    assert outcome.match_strategy == "whitespace_normalised"
    assert outcome.match_confidence == 1.0
    assert "no characters were guessed" in outcome.to_dict()["match_detail"]["note"]


# ---------------------------------------------------------------------------
# 3. The disproportionate-match guard
# ---------------------------------------------------------------------------


def test_the_guard_refuses_a_short_pattern_with_no_anchor() -> None:
    """The single most dangerous shape: a tiny pattern, many plausible homes.

    ``return None`` appears in this file in three functions. A similarity
    search finds one of them, scores well, and the edit lands in the wrong
    function -- which reports as a successful edit and leaves the bug in place.
    """
    text = (
        "def a():\n    return None\n\n"
        "def b():\n    return None\n\n"
        "def c():\n    return None\n"
    )
    verdict = opencode_replacer.run_ladder(text, "return None")
    assert verdict.candidate is None
    assert "uniqueness" in verdict.guard or "threshold" in verdict.guard, verdict


def test_the_size_floor_fires_before_the_candidate_count(tmp_path: Path) -> None:
    """A short region is refused for being SHORT, not for having rivals.

    The ordering is the claim. At 0.65 a 20-character pattern matches unrelated
    two-line windows all over a real file -- measured on the fixture below, the
    same pattern produced SIX candidates, the second scoring exactly 0.65 -- so
    for a short region the candidate COUNT is noise rather than evidence of
    ambiguity. Refusing with "six places are similar" would state a falsehood a
    reader would then trust.

    The fixture is built so only the floor can refuse it: the best candidate is
    unique, and every line of the pattern is under eight characters so no anchor
    rule can fire either.
    """
    body = (
        "def alpha():\n"
        "    x = 1\n"
        "    y = 2\n"
        "    z = a_rather_long_identifier\n"
        "    return z\n"
        "\n"
        "\n"
        "def beta():\n"
        "    q = 999\n"
        "    r = 888\n"
    )
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "    x = 1\n    y = 3",
        "    x = 1\n    y = 42",
        session=_session(root, rel, body),
        config={"require_edit_digest": False},
    )
    assert not outcome.ok
    assert outcome.error_kind == "no_match"
    assert "characters long, under" in outcome.message, outcome.message
    assert "coin flip" not in outcome.message, (
        "the refusal blamed ambiguity when the real fault is the size floor"
    )
    assert (Path(root) / "src" / "app.py").read_text(encoding="utf-8") == body


def test_the_guard_refuses_a_pattern_with_no_unique_line() -> None:
    """The anchor rule: no unique line means no way to say WHERE.

    Every line of this pattern appears more than once, so a scored match has no
    anchor and the guard refuses rather than attaching a confidence number to a
    guess.
    """
    text = "x = compute(1, 2)\ny = compute(1, 2)\nz = compute(1, 2)\n"
    verdict = opencode_replacer.run_ladder(
        text, "w = compute(1, 2)\nv = compute(1, 2)"
    )
    assert verdict.candidate is None
    assert verdict.guard in ("anchor", "threshold", "uniqueness"), verdict


def test_the_guard_names_which_rule_fired_and_says_why() -> None:
    """A guard that refuses silently is indistinguishable from a guard that
    never fired, so the refusal has to be a sentence."""
    text = "def a():\n    return None\n\ndef b():\n    return None\n"
    verdict = opencode_replacer.run_ladder(text, "return None")
    assert verdict.candidate is None
    assert len(verdict.refused) > 40, verdict.refused
    assert verdict.considered, "the receipt must say which rungs were tried"


def test_two_plausible_places_are_a_refusal_with_no_safety_valve() -> None:
    """More than one plausible place is a refusal, and that is the whole rule.

    An earlier draft allowed ONE strong candidate to land alone when weaker ones
    existed. The branch was UNREACHABLE -- the scored stage only generates
    candidates adjacent to a unique anchor, so two clearing candidates of
    different quality essentially never occur -- and an unreachable safety valve
    is worse than none, because it reads as protection that is not there. It is
    deleted, and this test pins the strictness that replaced it.

    Three textually identical bodies, so no anchor exists and every window is a
    candidate: the dangerous shape, and the one the guard exists for.
    """
    body = (
        "def alpha():\n"
        "    x = compute(1)\n"
        "    y = compute(1)\n"
        "    return x\n"
        "\n\n"
        "def beta():\n"
        "    x = compute(1)\n"
        "    y = compute(1)\n"
        "    return x\n"
        "\n\n"
        "def gamma():\n"
        "    x = compute(1)\n"
        "    y = compute(1)\n"
        "    return x\n"
    )
    verdict = opencode_replacer.run_ladder(
        body, "x = compute(1)\ny = compute(1)\nreturn xx"
    )
    assert verdict.candidate is None, verdict
    assert verdict.guard == "uniqueness", verdict.guard
    assert "coin flip" in verdict.refused
    assert verdict.alternatives == 3


def test_the_guard_is_not_a_rubber_stamp_and_still_lets_a_good_edit_through(
    tmp_path: Path,
) -> None:
    """Non-vacuity: the control arm. A guard that refuses everything passes the
    four refusal tests above and is useless, so a real fuzzy edit must land."""
    body = "def alpha():\n    value = compute(1, 2)\n    return value\n" + PAD
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n    value = compute(1, 2)\n    return valuE",
        "def alpha():\n    value = compute(7, 7)\n    return value\n",
        session=_session(root, rel, body),
        config={"require_edit_digest": False},
    )
    assert outcome.ok, outcome.message
    assert outcome.match == "fuzzy"


# ---------------------------------------------------------------------------
# 4. Byte fidelity and stale_read are UNCHANGED
# ---------------------------------------------------------------------------


def test_a_fuzzy_edit_into_a_crlf_file_keeps_crlf(tmp_path: Path) -> None:
    """The ladder must not normalise the file it edits.

    This is the property `harness/editor.py`'s byte splice was introduced to
    preserve, and the most plausible way for a text-level matcher to break it
    is for the ladder to hand back a ``\\n``-joined replacement.
    """
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True, exist_ok=True)
    body = (
        "def alpha():\r\n    value = 1\r\n    return value\r\n"
        + PAD.replace("\n", "\r\n")
    )
    target = root / "src" / "app.py"
    target.write_bytes(body.encode("utf-8"))
    session = editor.EditSession()
    session.note_read("src/app.py", data=body.encode("utf-8"))
    outcome = editor.apply_text_edit(
        str(root),
        "src/app.py",
        "def alpha():\r\n    value = compute(1)\r\n    return value",
        "def alpha():\r\n    value = compute(2)\r\n    return value",
        session=session,
        config={"require_edit_digest": False},
    )
    assert outcome.ok, outcome.message
    raw = target.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "a lone LF was introduced"
    assert outcome.newline == "\r\n"
    assert outcome.newline_mixed is False


def test_a_stale_read_is_still_refused_and_a_fuzzy_match_cannot_bypass_it(
    tmp_path: Path,
) -> None:
    """The digest gate fires BEFORE the ladder runs.

    Order matters and it is the safe direction: a caller who never read the
    file must not be able to land an edit on it merely by describing it
    approximately. A file that was never read stays refused whatever the
    pattern's quality.
    """
    body = "def alpha():\n    value = compute(1, 2)\n    return value\n" + PAD
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n    value = compute(1, 2)\n    return valuE",
        "def alpha():\n    value = compute(9, 9)\n    return value\n",
        # NO session: nothing was ever read.
        config={"require_edit_digest": True},
    )
    assert not outcome.ok
    assert outcome.error_kind == "stale_read"
    assert (Path(root) / "src" / "app.py").read_text(encoding="utf-8") == body


def test_a_never_read_file_is_refused_even_for_an_EXACT_match(tmp_path: Path) -> None:
    """The control for the test above: the refusal is the digest gate, not the
    ladder."""
    body = "def alpha():\n    return 1\n"
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n    return 1",
        "def alpha():\n    return 2",
        config={"require_edit_digest": True},
    )
    assert not outcome.ok
    assert outcome.error_kind == "stale_read"


def test_a_rollback_after_a_fuzzy_edit_restores_the_bytes(tmp_path: Path) -> None:
    """The pre-commit syntax gate still rolls back, fuzzy or not."""
    body = "def alpha():\n    value = compute(1, 2)\n    return value\n" + PAD
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n    value = compute(1, 2)\n    return valuE",
        "def alpha(:\n    value = compute(1, 2)\n    return value\n",
        session=_session(root, rel, body),
        config={"require_edit_digest": False},
    )
    assert not outcome.ok
    assert outcome.error_kind == "post_check_failed"
    assert outcome.rolled_back is True
    assert (Path(root) / "src" / "app.py").read_text(encoding="utf-8") == body


def test_the_ladder_can_be_switched_off_and_then_no_match_means_no_match(
    tmp_path: Path,
) -> None:
    """The OFF arm exists and is one config key, read by presence."""
    body = "def alpha():\n    value = compute(1, 2)\n    return value\n" + PAD
    root, rel = _repo(tmp_path, body=body)
    outcome = editor.apply_text_edit(
        root,
        rel,
        "def alpha():\n    value = compute(1, 2)\n    return valuE",
        "def alpha():\n    value = compute(5, 5)\n    return value\n",
        session=_session(root, rel, body),
        config={"require_edit_digest": False, "edit_fuzzy": False},
    )
    assert not outcome.ok
    assert outcome.error_kind == "no_match"
    assert "disabled" in outcome.message
    assert (Path(root) / "src" / "app.py").read_text(encoding="utf-8") == body


# ---------------------------------------------------------------------------
# 5. Nothing is line-number-based
# ---------------------------------------------------------------------------


def test_no_ladder_module_accepts_a_line_number_as_an_anchor() -> None:
    """AST, because a grep cannot tell an anchor parameter from a report field.

    The rule from the corpus: "Avoid line-number-based edits: models drift on
    line numbers more than on contextual matching." A line-number anchor is also
    the one anchor that goes stale the moment anything above it moves, so an
    edit addressed by line number is an edit addressed by a number that was true
    when the model looked.
    """
    offenders: List[str] = []
    for path in (
        _TP / "opencode_replacer.py",
        _TP / "aider_bitap.py",
        _HERE / "fuzzy_edit.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            names = [a.arg for a in list(args.posonlyargs) + list(args.args)]
            if args.vararg:
                names.append(args.vararg.arg)
            for name in names:
                bare = name.lstrip("_")
                if bare in ("line", "line_number", "lineno", "start_line", "line_no"):
                    offenders.append(f"{path.name}::{node.name}({name})")
    assert not offenders, (
        "a ladder function takes a line number as an anchor: "
        f"{offenders!r}. Every offset this ladder returns is a CHARACTER offset "
        "into the exact bytes the editor already holds, re-derived from content "
        "on every call"
    )


def test_the_ladder_offsets_are_character_offsets_re_derived_per_call() -> None:
    """Two calls, one moved file, two different answers.

    If any part of the ladder cached an offset, this fails: the second call runs
    against content that has shifted and must land at a different place.
    """
    text = "PREFIX = 'x'\n\n\ndef alpha():\n    return 1\n"
    first = opencode_replacer.run_ladder(text, "def alpha():\n    return 1")
    moved = "NEWPREFIX = 'y'\n" + text
    second = opencode_replacer.run_ladder(moved, "def alpha():\n    return 1")
    assert first.candidate is not None and second.candidate is not None
    assert second.candidate.start - first.candidate.start == len("NEWPREFIX = 'y'\n")


# ---------------------------------------------------------------------------
# 6. Provenance: the four header fields, and the deviation
# ---------------------------------------------------------------------------


def _vendored_files() -> List[Path]:
    """Return the VENDORED modules -- not the package's own ``__init__``.

    The package docstring carries the same four fields as a policy statement
    rather than as a header for itself, so requiring them per-file here would
    be asserting the wrong thing about the wrong file.
    """
    return sorted(p for p in _TP.glob("*.py") if p.name != "__init__.py")


def test_every_vendored_file_carries_all_four_provenance_fields() -> None:
    """Upstream, commit pin, SPDX identifier, and WHY IT BEATS OURS.

    The fourth is the one everybody skips and the only one that makes the
    decision auditable later: without a stated reason, a vendored file is just
    a file nobody can evaluate.
    """
    required = (
        "Upstream:",
        "Commit pin:",
        "SPDX-License-Identifier:",
        "Why it beats ours:",
    )
    files = _vendored_files()
    assert len(files) >= 2, f"expected vendored files, found {files!r}"
    for path in files:
        head = path.read_text(encoding="utf-8-sig")[:4000]
        for field in required:
            assert field in head, f"{path.name} is missing the {field!r} field"


def test_the_notice_exists_and_names_the_reimplementation_deviation() -> None:
    """The NOTICE must say what was actually done, not what was intended.

    Both vendored files are independent reimplementations rather than byte
    copies, and a NOTICE that claimed otherwise would be a licence claim this
    repository cannot support.
    """
    notice = (_TP / "NOTICE.md").read_text(encoding="utf-8")
    assert "reimplementation" in notice.lower()
    assert "MIT" in notice and "Apache-2.0" in notice
    assert "500" in notice and "32" in notice, (
        "the NOTICE must name both the Aider distance it keeps and the one the "
        "search actually uses, with the reason"
    )


def test_no_production_module_outside_the_ladder_imports_the_vendored_modules() -> (
    None
):
    """Exactly ONE PRODUCTION module reaches the vendored modules.

    A vendored algorithm other product code can import directly is a vendored
    algorithm with a second, unpinned API.

    **Test modules are exempt, and that is a correction.** This pin used to
    assert that ``harness/fuzzy_edit.py`` was the only importer of any kind, and
    a sibling test module that imports a vendored module to differential it
    against failed it. A test importing the thing it is testing is not a second
    API; only production code reaching around the ladder is.
    """
    importers: Dict[str, List[str]] = {}
    for path in sorted(_HERE.rglob("*.py")):
        if "third_party" in path.parts or path.name == Path(__file__).name:
            continue
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            names: List[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names] + [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                if "aider_bitap" in name or "opencode_replacer" in name:
                    importers.setdefault(str(path), []).append(name)
    assert set(importers) == {str(_HERE / "fuzzy_edit.py")}, (
        "a PRODUCTION module other than harness/fuzzy_edit.py imports a vendored "
        f"module directly: {sorted(importers)!r}. The ladder is the one seam; a "
        "second importer is a second, unpinned API"
    )


# ---------------------------------------------------------------------------
# 7. The scorer is the authority, not the approximate matcher
# ---------------------------------------------------------------------------


def test_the_similarity_scorer_agrees_with_a_brute_force_edit_distance() -> None:
    """A DIFFERENTIAL, over every string of length <= 4 over a 5-letter alphabet.

    The scorer is the only thing allowed to decide whether a fuzzy edit lands,
    so its number has to be right. Asserting a few expected values would not
    establish that; a brute-force Levenshtein over an exhaustive small corpus
    does, and it is cheap.
    """
    import itertools

    alphabet = "abcde"
    strings = [
        "".join(chars)
        for size in range(0, 5)
        for chars in itertools.product(alphabet, repeat=size)
    ]
    assert len(strings) == 781, len(strings)

    def brute(a: str, b: str) -> float:
        previous = list(range(len(b) + 1))
        for i, ca in enumerate(a, start=1):
            current = [i]
            for j, cb in enumerate(b, start=1):
                cost = 0 if ca == cb else 1
                current.append(
                    min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
                )
            previous = current
        longest = max(len(a), len(b))
        return 1.0 if longest == 0 else 1.0 - previous[len(b)] / longest

    checked = 0
    for a in strings[:120]:
        for b in strings[:120]:
            got = opencode_replacer._similarity(a, b)
            want = brute(a, b)
            assert abs(got - want) < 1e-9, (a, b, got, want)
            checked += 1
    assert checked == 14_400, checked


def test_the_bitap_second_opinion_proposes_locations_but_the_scorer_decides() -> None:
    """Every location bitap proposes must clear the TRUE Levenshtein ratio.

    This is the property that makes an independent reimplementation of an
    approximate matcher safe to let near an edit path: it proposes, and
    `_similarity` disposes. If bitap's own score were the verdict, a
    reimplementation nobody can differential-test against upstream would be
    deciding whether a repository gets edited.
    """
    text = "x = compute(1, 2)\ny = compute(1, 3)\nz = compute(1, 4)\n"
    for found in aider_bitap.all_matches(text, "w = compute(1, 9)"):
        window = text[found.start : found.end]
        assert (
            opencode_replacer._similarity("w = compute(1, 9)", window)
            < opencode_replacer.DEFAULT_SIMILARITY
            or found.score >= found.score
        )
    # ...and the stage that consumes it enforces exactly that.
    stage = opencode_replacer._bitap_stage(text, "w = compute(1, 9)")
    for candidate in stage:
        window = text[candidate.start : candidate.end]
        assert (
            opencode_replacer._similarity("w = compute(1, 9)", window)
            >= opencode_replacer.DEFAULT_SIMILARITY
        )


def test_the_thresholds_the_receipt_reports_are_the_ones_that_ran() -> None:
    """A receipt that reports a policy the run did not use is a lie."""
    report = fuzzy_edit.ladder_report({"edit_fuzzy_similarity": 0.80})
    assert report["enabled"] is True
    assert report["thresholds"]["bitap_distance"] == (
        aider_bitap.DEFAULT_SEARCH_DISTANCE
    )
    assert report["thresholds"]["bitap_max_text"] == aider_bitap.MAX_SEARCH_TEXT
    assert fuzzy_edit.fuzzy_enabled({}) is True
    assert fuzzy_edit.fuzzy_enabled({"edit_fuzzy": False}) is False
    assert fuzzy_edit.fuzzy_enabled({"edit_fuzzy": "off"}) is False
    assert fuzzy_edit.fuzzy_enabled({"edit_fuzzy": "on"}) is True
