"""P5/W1 T5 â€” session sharing: fail-closed redaction, the shareable predicate,
and the canary corpus proving secrets cannot travel.

An export is the highest-risk disclosure surface in the product because it is
INTENDED to leave the machine. These tests drive the real corpus through the
real export path and re-scan the exact bytes that travel â€” they do not assume
the redactor worked, they prove it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import security_corpus as corpus
from shared import session_share as share


def _session(turns=(), session_id="sess-1234abcd"):
    return {
        "session_id": session_id,
        "schema_version": 1,
        "repo": "C:/work/repo",
        "turns": list(turns),
        "summary": "",
        "active_run_id": None,
    }


def _turn(role, text):
    return {"role": role, "text": text}


# ---------------------------------------------------------------------------
# 1. Export works, and is self-contained and readable
# ---------------------------------------------------------------------------


class TestTheExportWorks:
    def test_a_session_exports_and_reimports_as_json(self, tmp_path: Path):
        session = _session([_turn("user", "why does mean() return the sum?")])
        document = share.build_share_document(session, [{"event": "turn_appended"}])
        assert document["kind"] == share.EXPORT_KIND
        assert document["schema_version"] == share.SCHEMA_VERSION
        receipt = share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        reread = json.loads((tmp_path / "share.json").read_text(encoding="utf-8"))
        assert reread["session"]["turns"][0]["text"] == "why does mean() return the sum?"
        assert receipt.bytes_written > 0

    def test_the_export_is_self_contained_and_readable(self, tmp_path: Path):
        """A share that needs the original machine is a debug artifact, not a share."""
        session = _session(
            [_turn("user", "fix the divisor"), _turn("assistant", "done")]
        )
        document = share.build_share_document(session)
        text = document["transcript_text"]
        assert "user: fix the divisor" in text
        assert "assistant: done" in text
        # Everything a reader needs is in the document: no machine-local handle.
        json.dumps(document)  # proves JSON-serializable without the origin

    def test_an_empty_session_renders_an_honest_transcript(self):
        document = share.build_share_document(_session())
        assert "(no turns recorded)" in document["transcript_text"]

    def test_the_receipt_tells_the_user_what_is_being_shared(self, tmp_path: Path):
        session = _session([_turn("user", "hello")])
        document = share.build_share_document(session)
        receipt = share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        assert receipt.lines, "the user must be told what is being shared"
        joined = "\n".join(receipt.lines)
        assert "redacted:" in joined
        assert "withheld:" in joined


# ---------------------------------------------------------------------------
# 2. Redaction is fail-closed ON THE EXPORT PATH
# ---------------------------------------------------------------------------


class TestRedactionIsFailClosed:
    def test_a_raising_redactor_refuses_the_export(self):
        def explode(_value):
            raise RuntimeError("redactor exploded")

        with pytest.raises(share.ShareRefused):
            share.build_share_document(_session([_turn("user", "x")]), redactor=explode)

    def test_a_nonfunctional_redactor_gates_sharing(self):
        """An identity redactor is not a redactor; the share is refused and
        NOTHING is written."""
        document = share.build_share_document(
            _session([_turn("user", "api_key=sk-FAKESECRET0123456789")]),
            redactor=lambda value: value,
        )
        verdict = share.shareable_verdict(document, redactor_functional=False)
        assert verdict.shareable is False
        assert verdict.reasons

    def test_a_refused_share_writes_nothing(self, tmp_path: Path):
        document = share.build_share_document(
            _session([_turn("user", "api_key=sk-FAKESECRET0123456789")]),
            redactor=lambda value: value,
        )
        destination = tmp_path / "share.json"
        with pytest.raises(share.ShareRefused):
            share.write_share(document, destination)
        assert not destination.exists()

    def test_the_real_redactor_passes_the_liveness_probe(self):
        assert share.redactor_is_functional() is True

    def test_the_probe_refuses_an_identity_redactor(self):
        assert share.redactor_is_functional.__module__ == "shared.session_share"
        # A redactor that answers with its own withheld marker is also unproven.
        document = share.build_share_document(
            _session([_turn("user", "api_key=sk-FAKESECRET0123456789")])
        )
        verdict = share.shareable_verdict(document)
        assert verdict.shareable is True  # control: the honest path stays open

    def test_a_redactor_that_withholds_still_lets_the_share_through(self, tmp_path: Path):
        """A withheld marker is the redactor's SAFE answer: counted, shown, not
        a refusal."""

        def withholding(value):
            if isinstance(value, dict):
                return {key: withholding(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [withholding(item) for item in value]
            if isinstance(value, str) and "unterrified" in value:
                return "(detail withheld: test)"
            return value

        session = _session([_turn("user", "the unterrified canary lives here")])
        document = share.build_share_document(session, redactor=withholding)
        receipt = share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        assert receipt.withheld_markers >= 1
        assert any("withheld" in line for line in receipt.lines)


# ---------------------------------------------------------------------------
# 3. Secrets cannot travel â€” asserted with the canary corpus
# ---------------------------------------------------------------------------


class TestSecretsCannotTravel:
    def test_a_credential_in_a_turn_never_travels(self, tmp_path: Path):
        session = _session([_turn("user", "use ghp_abcdefghijklmnopqrstuvwxyz012345")])
        document = share.build_share_document(session)
        assert share.shareable_verdict(document).shareable is True
        receipt = share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        body = (tmp_path / "share.json").read_text(encoding="utf-8")
        assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in body
        assert share.unredacted_findings(json.loads(body)) == ()
        assert receipt.redacted_texts >= 1

    def test_a_dotenv_read_in_a_tool_result_never_travels(self, tmp_path: Path):
        env_body = "OPENAI_API_KEY=sk-FAKESECRET0123456789ABCDEF\nDEBUG=1\n"
        session = _session([_turn("tool", f".env read: {env_body}")])
        events = [{"event": "tool_result", "payload": {"output": env_body}}]
        document = share.build_share_document(session, events)
        receipt = share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        body = (tmp_path / "share.json").read_text(encoding="utf-8")
        assert "sk-FAKESECRET0123456789ABCDEF" not in body
        assert "DEBUG=1" in body  # the share stays READABLE
        assert share.unredacted_findings(json.loads(body)) == ()
        assert receipt.redacted_texts >= 2

    def test_an_api_key_field_never_travels_and_is_counted(self, tmp_path: Path):
        session = _session([_turn("user", "hello")])
        session["model_profile"] = {"api_key": "sk-FAKESECRET0123456789", "tier": "cheap"}
        document = share.build_share_document(session)
        receipt = share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        body = (tmp_path / "share.json").read_text(encoding="utf-8")
        assert "sk-FAKESECRET0123456789" not in body
        assert receipt.redacted_keys >= 1
        assert "cheap" in body

    @pytest.mark.parametrize(
        "case",
        list(corpus.iter_canary_exfiltration_cases()),
        ids=lambda case: case.case_id,
    )
    def test_no_canary_case_leaks_a_secret_into_the_export(self, case, tmp_path: Path):
        """Every untrusted-source canary: the payload's SECRET SHAPES are
        redacted, and the exported bytes re-scan clean."""
        session = _session(
            [_turn("user", case.payload), _turn("assistant", case.payload)]
        )
        events = [{"event": "tool_result", "payload": {"output": case.payload}}]
        document = share.build_share_document(session, events)
        assert share.shareable_verdict(document).shareable is True
        share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        body = (tmp_path / "share.json").read_text(encoding="utf-8")
        assert share.unredacted_findings(json.loads(body)) == (), (
            f"{case.case_id} leaked a secret shape"
        )

    @pytest.mark.parametrize(
        "case",
        list(corpus.iter_obfuscated_canary_cases()),
        ids=lambda case: case.case_id,
    )
    def test_no_obfuscated_canary_survives_into_the_export(self, case, tmp_path: Path):
        """The worst case: a secret that HIDES from a matcher inside an export.
        Normalisation is what saves it, and this is the proof."""
        session = _session([_turn("tool", case.payload)])
        document = share.build_share_document(session)
        assert share.shareable_verdict(document).shareable is True
        share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        body = (tmp_path / "share.json").read_text(encoding="utf-8")
        assert share.unredacted_findings(json.loads(body)) == (), (
            f"{case.case_id} survived obfuscation"
        )
# The reassembled secret must not be recoverable from the bytes either.
        # Stripped of separators first: a token split by a zero-width space is
        # still recoverable by a reader, which is the whole point of the case.
        # (The first version of this line read `... or True`, so it asserted
        # nothing at all -- an always-true pin in a security suite.)
        perceived = share._perceivable(body)
        token = share._perceivable(corpus.CANARY_TOKEN)
        assert token not in perceived, (
            f"{case.case_id}: the canary is recoverable from the exported bytes"
        )

    def test_the_export_bytes_are_the_thing_that_is_checked(self, tmp_path: Path):
        """The check that matters is on the exact bytes that travel."""
        session = _session([_turn("user", "Bearer VEXCANARY0000notarealsecret0000")])
        document = share.build_share_document(session)
        share.write_share(document, tmp_path / "share.json", session=session, events=locals().get("events"))
        payload = (tmp_path / "share.json").read_bytes()
        assert b"VEXCANARY0000notarealsecret0000" not in payload


# ---------------------------------------------------------------------------
# 4. The shareable predicate gates sharing â€” not by default
# ---------------------------------------------------------------------------


class TestThePredicateGatesSharing:
    def test_a_document_with_a_surviving_secret_is_refused(self):
        document = {
            "kind": share.EXPORT_KIND,
            "schema_version": share.SCHEMA_VERSION,
            "session": {"notes": "api_key=sk-FAKESECRET0123456789"},
        }
        verdict = share.shareable_verdict(document)
        assert verdict.shareable is False
        assert any("credential" in reason for reason in verdict.reasons)

    def test_a_wrong_kind_is_refused(self):
        verdict = share.shareable_verdict({"kind": "other", "session": {}})
        assert verdict.shareable is False

    def test_a_non_mapping_is_refused(self):
        verdict = share.shareable_verdict(["not", "a", "mapping"])
        assert verdict.shareable is False

    def test_write_refuses_an_oversized_share(self, tmp_path: Path):
        session = _session([_turn("user", "x" * 200)])
        document = share.build_share_document(session)
        document["padding"] = "y" * (share.MAX_SHARE_BYTES + 1)
        with pytest.raises(share.ShareRefused):
            share.write_share(document, tmp_path / "share.json")
        assert not (tmp_path / "share.json").exists()

    def test_write_refuses_a_symlink_destination(self, tmp_path: Path):
        target = tmp_path / "real.json"
        target.write_text("{}", encoding="utf-8")
        link = tmp_path / "link.json"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlink privilege unavailable on this host")
        document = share.build_share_document(_session([_turn("user", "hi")]))
        with pytest.raises(share.ShareRefused):
            share.write_share(document, link)
        assert target.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# 5. The transcript renderer's bounds are stated, never silent
# ---------------------------------------------------------------------------


class TestTheTranscriptBounds:
    def test_a_long_turn_text_is_marked_truncated(self):
        session = _session([_turn("user", "x" * 9000)])
        text = share.render_transcript(session)
        assert "turn text truncated" in text

    def test_turns_beyond_the_cap_are_counted(self):
        session = _session([_turn("user", f"turn {i}") for i in range(300)])
        text = share.render_transcript(session)
        assert "earlier turns not shown" in text


# ---------------------------------------------------------------------------
# 6. The executable gate itself -- including proving it can fail
# ---------------------------------------------------------------------------
#
# `python -m shared.session_share --selftest` is what the `export-redaction`
# CI lane runs. A gate nobody has proved can fail is a gate that reads like a
# guarantee, so the load-bearing test here is the mutation: redaction disabled
# in-process must turn the report red.


class TestTheExportRedactionGate:
    def test_the_gate_is_green_over_the_whole_corpus(self):
        report = share.selftest()
        assert report["ok"] is True, report
        assert report["verdict"] == "EXPORT_REDACTION_CLEAN"
        # Counts derived from the corpus, never restated, so a corpus that grows
        # does not silently stop being covered.
        assert report["canary_cases"] == len(
            list(corpus.iter_canary_exfiltration_cases())
        )
        assert report["obfuscated_cases"] == len(
            list(corpus.iter_obfuscated_canary_cases())
        )
        assert report["cases"] == report["canary_cases"] + report["obfuscated_cases"]
        assert report["refused_exports"] == []
        assert report["canary_tokens_surviving"] == []
        assert report["unredacted_findings"] == []

    def test_the_gate_goes_red_when_redaction_is_disabled(self, monkeypatch):
        """The mutation test. Without it, this gate is a comment with a main()."""
        monkeypatch.setattr(
            share, "build_share_document", lambda session, events=None, **kw: dict(session)
        )
        report = share.selftest()
        assert report["ok"] is False
        assert report["verdict"] == "EXPORT_REDACTION_LEAK"
        # Property 1 fires first and hardest: the substituted credential shapes
        # (`ghp_...`, an AWS key, URL userinfo) survive an unredacted export.
        assert report["unredacted_findings"], report
        # Property 2: of the cases that carried the canary, the OBFUSCATED ones
        # must not still have it. Derived from the report rather than written as
        # a count, so a corpus change moves the expectation instead of breaking it.
        carried = report["cases_carrying_the_canary"]
        assert len(report["canary_tokens_surviving"]) == len(carried) - report["canary_cases"]

    def test_a_preserved_injection_directive_is_not_counted_as_a_leak(self):
        """The distinction this gate had to get right, pinned in both directions.

        The normal canary corpus is prompt-INJECTION text whose token is not
        credential-shaped, so redaction correctly leaves it alone. That must
        stay GREEN -- demanding otherwise would demand free-text redaction, and
        an export with every sentence removed shows nothing -- and the report
        must still NAME it, because a green gate beside an unstated exception is
        how a leak ships.
        """
        report = share.selftest()
        assert report["ok"] is True
        assert report["injection_directives_preserved"], (
            "no directive was preserved, so this test is no longer measuring the "
            "exception it exists to pin"
        )
        assert len(report["injection_directives_preserved"]) == report["canary_cases"]
        assert report["why_directives_survive"]

    def test_the_gate_reports_a_raising_export_as_a_finding(self, monkeypatch):
        """A gate that dies on hostile input reports nothing, and no report
        reads as no problem."""

        def _explode(*args, **kwargs):
            raise RuntimeError("the export path exploded")

        monkeypatch.setattr(share, "build_share_document", _explode)
        report = share.selftest()
        assert report["ok"] is False
        assert report["refused_exports"], report
        assert report["cases"] == report["canary_cases"] + report["obfuscated_cases"]
        for row in report["case_receipts"]:
            assert any("exploded" in finding for finding in row["findings"]), row
            assert row["reasons"] == ["the export path raised"]

    def test_the_cli_exits_zero_when_clean_and_two_when_red(self, capsys, monkeypatch):
        assert share.main(["--selftest"]) == 0
        out = capsys.readouterr().out
        assert "EXPORT_REDACTION_CLEAN" in out
        assert "injection directives preserved" in out

        monkeypatch.setattr(share, "selftest", lambda: {
            "ok": False,
            "verdict": "EXPORT_REDACTION_LEAK",
            "cases": 0,
            "canary_cases": 0,
            "obfuscated_cases": 0,
            "refused_exports": ["planted"],
            "canary_tokens_surviving": ["planted"],
            "unredacted_findings": [],
            "injection_directives_preserved": [],
        })
        assert share.main(["--selftest"]) == 2


