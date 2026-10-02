"""W1.1: the redaction authority normalises hostile characters, fails closed, stays linear.

Three properties, each named after the behaviour it pins:

* **Task A - normalisation inside the redactor.** A caller will eventually get
  the strip-before-redact order wrong. Defence in depth means the redactor
  normalises the bytes it is about to match, so an ordering mistake cannot
  decide whether a credential is caught. The REPORT is what keeps that honest:
  `redact_text_report` tells the caller its own ordering was wrong instead of
  silently compensating.
* **Task B - fail closed.** The historical behaviour degraded to `str(value)`
  on an internal exception, which turns a redactor bug into a disclosure. A
  withheld marker naming the reason is the only acceptable alternative.
* **Task C - linear.** R2-11 fixed a quadratic redactor that hung the journal
  for minutes. Normalisation must not reintroduce it, and the **non-matching**
  case is the one that hides a quadratic redactor.

Every payload here is inert fake test data. No real credential appears.
"""

from __future__ import annotations

import time

import pytest

from shared import security
from shared.security import (
    REDACTED_SECRET,
    NormalizationReceipt,
    normalize_for_redaction,
    redact_text,
    redact_text_report,
)
from shared.security_corpus import (
    iter_obfuscated_canary_cases,
    iter_security_cases,
)

#: The four obfuscation classes Task A names, as (label, payload) pairs built
#: from CODEPOINTS. Written as codepoints on purpose: a literal zero-width
#: character inside a test source file is invisible to the next reviewer, which
#: is the same blindness the first draft of the normaliser had.
ZWSP = "\u200b"  # ZERO WIDTH SPACE
ZWNJ = "\u200c"  # ZERO WIDTH NON-JOINER
ZWJ = "\u200d"  # ZERO WIDTH JOINER
LRM = "\u200e"  # LEFT-TO-RIGHT MARK
RLO = "\u202e"  # RIGHT-TO-LEFT OVERRIDE
LRI = "\u2066"  # LEFT-TO-RIGHT ISOLATE
PDI = "\u2069"  # POP DIRECTIONAL ISOLATE
BOM = "\ufeff"  # ZERO WIDTH NO-BREAK SPACE


def _framed(secret: str, key: str = "api_key") -> str:
    """Return the shape the rule table actually owns."""
    return f"{key}={secret}"


class TestNormalisationUndoesTheFourClasses:
    """Each class must be removed, and the removal must be COUNTED."""

    def test_ansi_escapes_are_stripped_and_a_split_secret_is_caught(self):
        payload = "api_key=sk-abc\x1b[35mabcdefghijklmnop\x1b[0m"
        # Without stripping, the rule sees `sk-` + escape + payload and misses.
        text, receipt = redact_text_report(payload)
        assert REDACTED_SECRET in text, payload
        assert receipt.ansi_removed == 2
        assert receipt.changed is True

    def test_osc_sequence_is_stripped_whole(self):
        # An OSC sequence runs to BEL or ST; matching only its `[...]` tail
        # would leave the title text behind as ordinary content.
        payload = "api_key=sk-abc\x1b]0;a secret title\x07defghijklmnop"
        normalized, receipt = normalize_for_redaction(payload)
        assert "\x1b" not in normalized
        assert "a secret title" not in normalized
        assert receipt.ansi_removed >= 1
        assert REDACTED_SECRET in redact_text(payload)

    def test_zero_width_space_is_removed_and_a_split_secret_is_caught(self):
        payload = f"api_key=sk-abc{ZWSP}defghijklmnop"
        text, receipt = redact_text_report(payload)
        assert REDACTED_SECRET in text
        assert receipt.zero_width_removed == 1

    @pytest.mark.parametrize("codepoint", [ZWSP, ZWNJ, ZWJ, LRM, RLO, LRI, PDI, BOM])
    def test_every_invisible_codepoint_in_the_set_is_removed(self, codepoint):
        payload = f"api_key=sk-abc{codepoint}defghijklmnop"
        normalized, receipt = normalize_for_redaction(payload)
        assert codepoint not in normalized
        assert receipt.zero_width_removed == 1
        assert REDACTED_SECRET in redact_text(payload)

    def test_the_invisible_set_covers_the_documented_ranges(self):
        """Non-vacuity: the set must contain every codepoint Task A names.

        The first draft wrote this set as literal characters and silently
        omitted U+200B, because a zero-width space renders as nothing and is
        therefore unreadable in source. This test is why that cannot recur.
        """
        for codepoint in (
            0x200B,
            0x200C,
            0x200D,
            0x200E,
            0x200F,  # U+200B-U+200F
            0x202A,
            0x202B,
            0x202C,
            0x202D,
            0x202E,  # U+202A-U+202E
            0x2066,
            0x2067,
            0x2068,
            0x2069,  # U+2066-U+2069
            0xFEFF,
        ):
            assert chr(codepoint) in security._ZERO_WIDTH_AND_BIDI_SET, (
                f"U+{codepoint:04X} is missing from the normalisation set; a "
                "secret split there would survive into the output"
            )

    def test_a_homoglyph_in_the_key_name_is_folded(self):
        # Cyrillic KA (U+043A) in place of the ASCII `k`.
        payload = "api_\u043aey=abcdefghijklmnop"
        normalized, receipt = normalize_for_redaction(payload)
        assert normalized == "api_key=abcdefghijklmnop"
        assert receipt.confusables_folded == 1
        assert REDACTED_SECRET in redact_text(payload)

    def test_other_control_characters_are_removed(self):
        payload = "api_key=sk-abc\x00\x07defghijklmnop"
        normalized, receipt = normalize_for_redaction(payload)
        assert "\x00" not in normalized
        assert receipt.controls_removed == 2
        assert REDACTED_SECRET in redact_text(payload)

    def test_normalisation_never_makes_a_secret_harder_to_match(self):
        """The direction is the safety argument, so it is asserted, not assumed.

        The first framing tried here was a QUOTED secret split by a zero-width
        space, on the reasoning that the closing quote would land where the rule
        does not expect it. Measured, that was wrong: the raw form was redacted
        anyway, because `_QUOTED_SECRET` and `_KEY_VALUE_SECRET` both match a
        value class that already spans the invisible character. A test built on
        it would have been asserting something false about the mechanism.

        What is measurably true is the BARE strict-body form: a `github_token`
        with a zero-width character inside its body defeats the rule's
        `[A-Za-z0-9_]{12,}` body class, so the raw output still shows the token
        and normalisation is the only thing that lets the rule reach it.
        """
        token = "ghp_abcdefghijklmnopqrstuvwxyz012345"
        raw = f"{token[:12]}{ZWSP}{token[12:]}"

        # Raw form: the rule cannot see through the split.
        assert _visible_run(_rules_only(raw), token) >= LEAK_RUN, (
            "the raw framing no longer defeats the strict-body rule, so this "
            "test has stopped testing what it claims"
        )
        # Normalised form: it can.
        normalized = normalize_for_redaction(raw)[0]
        assert normalized == token
        assert _visible_run(_rules_only(normalized), token) < LEAK_RUN


def _rules_only(text: str) -> str:
    """Apply the rule table WITHOUT normalisation - the pre-W1.1 behaviour.

    This is what makes the "normalisation helps" claim measurable rather than
    circular: the comparison is against the same rules with the normalisation
    step removed, not against a different expectation.
    """
    return security._redact_text_with_scan(security._redact_explicit(text, ()))[0]


class TestTheReceiptMakesACallersOrderingMistakeVisible:
    """A caller's bug must stay the caller's bug, visibly."""

    def test_a_clean_call_site_reports_no_change(self):
        _text, receipt = redact_text_report("api_key=abcdefghijklmnop")
        assert receipt.changed is False
        assert receipt.raw_would_have_leaked is False
        assert receipt.to_dict()["changed"] is False

    def test_an_unstripped_call_site_is_told(self):
        _text, receipt = redact_text_report(f"api_key=sk-abc{ZWSP}defghijklmnop")
        assert receipt.raw_would_have_leaked is True

    def test_the_summary_names_the_class(self):
        _text, receipt = redact_text_report(
            "api_key=sk-\x1b[35mabcdefghijklmnop\x1b[0m"
        )
        assert "ansi=2" in receipt.summary()
        _text, clean = redact_text_report("nothing hostile here")
        assert "no hostile characters" in clean.summary()

    def test_counts_are_counts_never_defaulted_zeros(self):
        """A receipt that says 0 for work it did is the dishonesty this
        repo exists to prevent."""
        _text, receipt = redact_text_report(f"api_key=sk-abc{ZWSP}defghijklmnop")
        assert receipt.zero_width_removed == 1
        assert receipt.ansi_removed == 0  # genuinely absent, not unknown
        payload = receipt.to_dict()
        assert payload["zero_width_removed"] == 1
        assert payload["ansi_removed"] == 0

    def test_the_receipt_is_json_safe(self):
        import json

        _text, receipt = redact_text_report(f"api_key=sk-abc{ZWSP}defghijklmnop")
        assert json.loads(json.dumps(receipt.to_dict()))["changed"] is True

    def test_a_clean_input_returns_the_same_object(self):
        """The common case must cost no allocation."""
        original = "an ordinary log line with no secrets at all"
        normalized, receipt = normalize_for_redaction(original)
        assert normalized is original
        assert receipt == NormalizationReceipt()

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_and_none_stay_empty(self, empty):
        normalized, receipt = normalize_for_redaction(empty)
        assert normalized == ""
        assert receipt.changed is False


class TestFailClosed:
    """A redactor bug must become a withheld marker, never a disclosure."""

    def test_a_poisoned_pattern_pass_withholds_instead_of_disclosing(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("injected redactor fault")

        monkeypatch.setattr(security, "_redact_text_with_scan", boom)
        out = redact_text("api_key=sk-SUPERSECRETVALUE1234567890")
        assert "SUPERSECRET" not in out
        assert "withheld" in out.lower()
        assert "RuntimeError" in out

    def test_the_withheld_marker_names_the_reason(self):
        def boom(*_args, **_kwargs):
            raise ValueError("secondary detail")

        original = security._redact_text_with_scan
        try:
            security._redact_text_with_scan = boom
            out = redact_text("anything")
        finally:
            security._redact_text_with_scan = original
        assert out.startswith("(detail withheld: ")
        assert "ValueError" in out

    def test_a_value_that_cannot_be_rendered_is_named_not_silently_empty(self):
        """An empty string on a display surface reads as "nothing to show"."""

        class Hostile:
            def __str__(self):
                raise RuntimeError("cannot render")

        out = redact_text(Hostile())
        assert "withheld" in out.lower()
        assert "cannot render" not in out  # the reason is the class, not the payload

    def test_genuine_emptiness_still_renders_empty(self):
        """The withheld marker must not swallow the honest empty case."""
        assert redact_text("") == ""
        assert redact_text(None) == ""

    def test_a_poisoned_explicit_secrets_pass_withholds(self, monkeypatch):
        def boom(_text, _secrets):
            raise RuntimeError("secret substitution fault")

        monkeypatch.setattr(security, "_redact_explicit", boom)
        out = redact_text("api_key=sk-abcdefghijklmnop")
        assert "abcdefghijklmnop" not in out
        assert "withheld" in out.lower()

    def test_redact_text_report_also_fails_closed(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("report path fault")

        monkeypatch.setattr(security, "_redact_text_with_scan", boom)
        text, receipt = redact_text_report("api_key=sk-abcdefghijklmnop")
        assert "withheld" in text.lower()
        assert receipt.changed is False  # an honest receipt, not a fake one

    def test_redact_text_scanned_also_fails_closed(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("scanned path fault")

        monkeypatch.setattr(security, "_redact_text_with_scan", boom)
        text, scan = security.redact_text_scanned("api_key=sk-abcdefghijklmnop")
        assert "withheld" in text.lower()
        assert scan.text_length == 0

    def test_the_marker_cannot_carry_a_terminal_control_character(self):
        """A reason arriving from an exception is DATA, and data reaches a
        terminal. The marker must not become an injection vector."""
        marker = security._withheld_text("bad\x1b[31mreason\x07here")
        assert "\x1b" not in marker
        assert "\x07" not in marker

    def test_the_marker_is_bounded(self):
        assert len(security._withheld_text("x" * 5000)) < 200

    def test_the_marker_survives_an_empty_reason(self):
        assert "reason not reported" in security._withheld_text("")


class TestTheNormalisationIsLinear:
    """R2-11 fixed a quadratic redactor. Normalisation must not reintroduce it.

    The NON-MATCHING case is the one that matters: a quadratic redactor is
    usually fast on input it matches and slow on input it does not, so a suite
    that only measures the matching case measures nothing.
    """

    @staticmethod
    def _time(payload: str) -> float:
        start = time.perf_counter()
        redact_text(payload)
        return time.perf_counter() - start

    @pytest.mark.parametrize(
        ("label", "payload"),
        [
            ("matching-class-run", None),
            ("non-matching-run", None),
            ("invisible-char-flood", None),
            ("confusable-flood", None),
            ("ansi-flood", None),
        ],
    )
    def test_each_shape_stays_sub_second_at_400k(self, label, payload):
        shapes = {
            "matching-class-run": "abcdefgh" * 50000,
            "non-matching-run": "a" * 400000,
            "invisible-char-flood": ("a" + ZWSP) * 200000,
            "confusable-flood": "\u0430" * 400000,
            "ansi-flood": ("a\x1b[0m") * 200000,
        }
        text = shapes[label]
        elapsed = self._time(text)
        assert elapsed < 1.0, (
            f"{label} took {elapsed:.3f}s for {len(text)} chars; the redactor "
            "is on every journal write, so a super-linear shape is a denial "
            "of service against the whole harness"
        )

    @pytest.mark.parametrize(
        ("label", "builder"),
        [
            ("non-matching-run", lambda n: "a" * n),
            ("matching-class-run", lambda n: ("abcdefgh" * (n // 8 + 1))[:n]),
        ],
    )
    def test_a_fourfold_input_does_not_cost_sixteen_times_the_time(
        self, label, builder
    ):
        """4x input must cost roughly 4x time. A 16x cost is quadratic."""
        small = self._time(builder(100_000))
        large = self._time(builder(400_000))
        growth = large / max(small, 1e-9)
        assert growth < 8.0, (
            f"{label}: 4x the input cost {growth:.1f}x the time ({small:.4f}s -> "
            f"{large:.4f}s). That is the quadratic shape R2-11 removed."
        )

    def test_the_three_required_measurements_are_all_sub_second(self):
        """The three measurements the task names, as assertions.

        `shared/AGENTS.md` records the R2-11 curve; these are the same three
        inputs so a regression cannot hide behind a changed benchmark.
        """
        for label, payload in (
            ("40k", "y" * 40_000),
            ("400k", "y" * 400_000),
            ("400k-nonmatch", "a" * 400_000),
        ):
            elapsed = self._time(payload)
            assert elapsed < 1.0, f"{label} took {elapsed:.3f}s"

    def test_the_scan_receipt_still_works_through_normalisation(self):
        """The reporting path must keep its contract after normalisation."""
        _text, scan = security.redact_text_scanned("y" * 40_000)
        assert scan.text_length == 40_000
        assert scan.cap_reached is True
        # The contract here is "statistics stay honest about being capped".
        # It is NOT expressible as `scanned_chars < text_length`: spans overlap
        # by `span_overlap` to avoid missing a match on a boundary, so on a
        # capped line scanned_chars legitimately EXCEEDS text_length (45632 vs
        # 40000, measured). The fields that actually carry the warning are
        # asserted below.
        assert scan.scanned_chars > scan.text_length, (
            "on a capped line the overlapping spans should over-count; if this "
            "ever drops to == text_length the overlap is gone and boundary "
            "matches can be missed"
        )
        assert scan.capped_lines == 1
        # A saturated maximum is not a measured maximum, and must not read as
        # one - this is the field a caller would otherwise quote as fact.
        assert scan.max_repeated_run == scan.span_cap
        assert scan.skipped_rules, (
            "a capped scan must name what it skipped rather than reporting "
            "complete statistics"
        )


class TestTheCorpusCoversTheObfuscationClasses:
    """Task D: the corpus must contain the cases, and they must be real."""

    def test_at_least_seven_new_obfuscation_cases_exist(self):
        cases = [c for c in iter_security_cases() if c.category == "obfuscation"]
        assert len(cases) >= 7, (
            f"only {len(cases)} obfuscation cases in the corpus; the task "
            "requires at least 7"
        )

    def test_every_obfuscation_case_is_actually_redacted(self):
        for case in iter_security_cases():
            if case.category != "obfuscation":
                continue
            out = redact_text(case.payload)
            assert REDACTED_SECRET in out, (
                f"corpus case {case.case_id!r} is not redacted: {out!r}. A "
                "corpus case that cannot fail is worse than no case."
            )

    def test_the_obfuscation_cases_cover_all_four_classes(self):
        summaries = []
        for case in iter_security_cases():
            if case.category == "obfuscation":
                _text, receipt = redact_text_report(case.payload)
                summaries.append(receipt)
        assert any(r.ansi_removed for r in summaries), "no ANSI case"
        assert any(r.zero_width_removed for r in summaries), "no zero-width case"
        assert any(r.controls_removed for r in summaries), "no control case"
        assert any(r.confusables_folded for r in summaries), "no homoglyph case"

    def test_the_corpus_reaches_both_lanes(self):
        from shared.security_corpus import (
            iter_canary_exfiltration_cases,
            iter_obfuscated_canary_cases,
        )

        assert len(list(iter_canary_exfiltration_cases())) >= 7
        assert len(list(iter_obfuscated_canary_cases())) >= 7

    @pytest.mark.parametrize(
        "case", list(iter_obfuscated_canary_cases()), ids=lambda c: c.case_id
    )
    def test_no_obfuscated_canary_survives_into_the_output(self, case):
        """The worst case: a secret that survives normalisation into a network call.

        Asserts against the PERCEIVED output rather than a substring search of
        the raw bytes, because an invisible character does not hide a secret
        from the person or system reading it.
        """
        secret = _secret_in(case.payload)
        assert secret is not None, f"{case.case_id}: no known secret located"
        run = _visible_run(redact_text(case.payload), secret)
        assert run < LEAK_RUN, (
            f"{case.case_id}: {run} verbatim characters of the secret are still "
            f"readable in the output: {redact_text(case.payload)!r}"
        )

    @pytest.mark.parametrize(
        "case", list(iter_obfuscated_canary_cases()), ids=lambda c: c.case_id
    )
    def test_every_obfuscated_canary_carries_obfuscation(self, case):
        """Non-vacuity for the corpus itself: a case with no obfuscation in it
        cannot test normalisation."""
        _normalized, receipt = normalize_for_redaction(case.payload)
        assert receipt.changed is True, (
            f"{case.case_id} contains no obfuscation; it cannot test normalisation"
        )

    @pytest.mark.parametrize(
        "case", list(iter_obfuscated_canary_cases()), ids=lambda c: c.case_id
    )
    def test_normalisation_reaches_a_rule_that_cannot_see_through_it(self, case):
        """Normalising must hand the rules something they can match.

        Asserted for EVERY case rather than only the ones where it is the sole
        defence, because "normalisation happens" is true for all of them and a
        regression that silently disabled it would otherwise show up only as a
        vaguer failure elsewhere.
        """
        secret = _secret_in(case.payload)
        assert secret is not None, f"{case.case_id}: no known secret located"

        raw_run = _visible_run(_rules_only(case.payload), secret)
        norm_run = _visible_run(
            _rules_only(normalize_for_redaction(case.payload)[0]), secret
        )
        assert norm_run < LEAK_RUN, (
            f"{case.case_id}: after normalisation the rules still cannot see "
            f"the secret ({norm_run} verbatim characters), so normalisation is "
            "not reaching any rule that owns this shape"
        )
        assert norm_run <= raw_run, (
            f"{case.case_id}: normalisation made the secret MORE matchable "
            f"({raw_run} -> {norm_run} visible characters). That is the one "
            "direction this whole mechanism must never move in."
        )

    def test_at_least_five_cases_are_load_bearing_for_normalisation(self):
        """The corpus must actually exercise normalisation, not just contain it.

        This is the non-vacuity gate. Measured on this tree, 5 of the 7 cases are
        load-bearing - the RAW output shows a readable run of the secret and only
        normalisation hides it. The other two are defended raw as well, by a
        rule whose character class already spans the invisible character; they
        are kept because they prove the rules do not misfire, but a corpus whose
        load-bearing set emptied out would stop testing the mechanism entirely
        while every end-to-end assertion still passed.
        """
        load_bearing = []
        for case in iter_obfuscated_canary_cases():
            secret = _secret_in(case.payload)
            if secret is None:
                continue
            raw_run = _visible_run(_rules_only(case.payload), secret)
            norm_run = _visible_run(
                _rules_only(normalize_for_redaction(case.payload)[0]), secret
            )
            if raw_run >= LEAK_RUN and norm_run < LEAK_RUN:
                load_bearing.append(case.case_id)
        assert len(load_bearing) >= 5, (
            f"only {len(load_bearing)} of {len(list(iter_obfuscated_canary_cases()))} "
            f"obfuscated cases are load-bearing ({load_bearing}); the corpus no "
            "longer demonstrates that normalisation defends the strict-body rules"
        )

    def test_the_homoglyph_case_is_saved_by_folding_the_key_name(self):
        """The second mechanism, asserted separately so it is not implied.

        Measured: this case is NOT load-bearing on its own - `_KEY_VALUE_SECRET`
        also matches the raw form, because its value class spans the Cyrillic
        character. What it does prove is the other direction: that a homoglyph
        in the KEY NAME is folded, so the rule that owns `ghp_token` can reach
        the value at all.
        """
        case = next(
            c
            for c in iter_obfuscated_canary_cases()
            if c.case_id.endswith("homoglyph-key")
        )
        secret = _secret_in(case.payload)
        assert secret is not None
        normalized, receipt = normalize_for_redaction(case.payload)
        assert receipt.confusables_folded >= 1
        assert "\u04bb" not in normalized
        # The KEY is what hid the secret from the key-name rule.
        assert "\u04bbp_token" in case.payload
        assert "ghp_token" in normalized
        assert _visible_run(redact_text(case.payload), secret) < LEAK_RUN

    def test_every_cyrillic_lowercase_lookalike_is_folded(self):
        """`api_key` and `secret` are spelled entirely from the Cyrillic set.

        A table missing `к` (U+043A) leaves the single most important credential
        key name unfoldable, and the failure is invisible: the raw key name
        simply reads as `api_ky`. This is the guard for that.
        """
        latin_to_cyrillic = {
            "a": 0x0430,
            "b": 0x0431,
            "c": 0x0441,
            "e": 0x0435,
            "h": 0x04BB,
            "i": 0x0456,
            "j": 0x0458,
            "k": 0x043A,
            "m": 0x043C,
            "o": 0x043E,
            "p": 0x0440,
            "s": 0x0455,
            "t": 0x0442,
            "x": 0x0445,
            "y": 0x0443,
        }
        for latin, codepoint in latin_to_cyrillic.items():
            normalized, receipt = normalize_for_redaction(chr(codepoint))
            assert normalized == latin, (
                f"U+{codepoint:04X} should fold to {latin!r} but normalised to "
                f"{normalized!r}"
            )
            assert receipt.confusables_folded == 1

    def test_the_homoglyph_table_is_not_a_universal_translation_table(self):
        """Folding non-ASCII prose into ASCII would silently rewrite user text.

        Guards the narrowness that makes the table safe to apply to every
        payload: a character with no security-relevant look-alike must pass
        through untouched.
        """
        for text in (
            "\u3053\u3093\u306b\u3061\u306f",
            "\u00e9t\u00e9",
            "\u05e9\u05dc\u05d5\u05dd",
        ):
            normalized, receipt = normalize_for_redaction(text)
            assert normalized == text
            assert receipt.confusables_folded == 0


def _secret_in(payload: str) -> str | None:
    """Return the corpus secret embedded in ``payload``, or ``None``.

    Reads the tokens from the corpus module rather than hardcoding them, so a
    corpus edit cannot leave this helper asserting about a token that is no
    longer there.
    """
    from shared import security_corpus

    known = [
        security_corpus.CANARY_TOKEN,
        security_corpus._GITHUB_TOKEN,
        security_corpus._AWS_TOKEN,
        security_corpus._JWT_TOKEN,
    ]
    for secret in known:
        if _visible_run(payload, secret) >= 8:
            return secret
    return None


def _visible_run(text: str, secret: str) -> int:
    """Longest contiguous run of ``secret`` a READER can recover from ``text``.

    The invisible characters are stripped from ``text`` first, because that is
    the honest question. Three metrics were tried, and two of them were wrong:

    * Longest run on the RAW output undercounts. `AKIAIOSFOD<ZWSP>NN7EXAMPLE1`
      has two 10-character runs, so it passes any threshold - yet a reader
      cannot see the zero-width character and recovers the whole token.
    * Longest common subsequence over the whole string overcounts. The
      canary `VEXCANARY0000notarealsecret0000` scored 14 against a fully
      redacted line purely from the surrounding English prose.

    Contiguous run length against the PERCEIVED text has neither failure mode.
    """
    seen = normalize_for_redaction(text)[0]
    best = 0
    for start in range(len(secret)):
        for end in range(len(secret), start, -1):
            if end - start > best and secret[start:end] in seen:
                best = end - start
                break
        if best >= LEAK_RUN:
            break
    return best


#: A run of this many verbatim secret characters counts as a leak. Below it,
#: what remains is a rule's own framing rather than the secret.
LEAK_RUN = 12


class TestNormalisationIsNotASecretLaunderingMachine:
    """Guard the direction that would make normalisation unsafe."""

    def test_normalisation_does_not_change_ordinary_text(self):
        for payload in (
            "a normal log line",
            "user@example.com did a thing at 12:00",
            "path/to/file.py:42: SyntaxError",
            "",
            "unicode prose: naïve café résumé",
        ):
            normalized, receipt = normalize_for_redaction(payload)
            assert normalized == payload
            assert receipt.changed is False

    def test_confusable_folding_is_narrow(self):
        """Folding must not rewrite legitimate non-ASCII prose.

        The table is deliberately small and ASCII-adjacent; a wider table would
        silently change what a caller sees.
        """
        payload = "café naïve résumé — ok"
        normalized, _receipt = normalize_for_redaction(payload)
        assert normalized == payload

    def test_the_confusable_table_is_documented_as_narrow(self):
        assert len(security._CONFUSABLES) < 100, (
            "the confusable table grew past the 'small and ASCII-adjacent' "
            "design; re-justify it before shipping a wider fold"
        )

    def test_normalisation_is_idempotent(self):
        """Running it twice must be a no-op the second time."""
        payload = f"api_key=sk-abc{ZWSP}\x1b[31mdefghijklmnop"
        once, first = normalize_for_redaction(payload)
        twice, second = normalize_for_redaction(once)
        assert once == twice
        assert first.changed is True
        assert second.changed is False

    def test_the_module_does_not_import_tracing(self):
        """`shared/tracing.py` imports this module, so importing it back would
        be a cycle. The R2-11 note records this; it is still true."""
        source = security.__file__ or ""
        assert source
        with open(source, encoding="utf-8-sig") as handle:
            text = handle.read()
        assert "import shared.tracing" not in text
        assert "from shared.tracing" not in text


class TestStripBeforeRedactIsRecommendedInTheDocstring:
    """The ordering advice must be IN the docstring, not only in a changelog."""

    def test_redact_text_recommends_strip_first(self):
        doc = redact_text.__doc__ or ""
        assert "strip" in doc.lower()
        assert "redact" in doc.lower()
        # The recommendation must come with its reason.
        assert "visibly contiguous" in doc or "escape" in doc.lower()

    def test_the_fail_closed_contract_is_documented(self):
        doc = redact_text.__doc__ or ""
        assert "CLOSED" in doc.upper()
        assert "withheld" in doc.lower()
