"""Runtime-local pins for the unshipped structural difficulty predictor (W2 T3).

``runtime/difficulty.py``'s structural predictor WON its held-out split
(0.9412 against 0.8235) and was deliberately NOT shipped: that split carried
**one** hard-labelled observation against a declared floor of three, so the win
was one lucky prediction. Shipping it anyway would be a SILENT behaviour change
to every routing decision in the project.

What is pinned here, and what is not:

* ``predict_structural`` is reachable only by an explicit
  ``Task.config["difficulty_features"] = "structural"``. There is no config
  default, no environment variable and no CLI switch that selects it.
* ``"auto"`` resolves to the INCUMBENT heuristic, because
  ``runtime/difficulty_structural_calibration.json`` — the only thing that would
  switch ``auto`` to the challenger — does not exist.
* The two arms must be DISTINGUISHABLE, or "unshipped" and "shipped" would be
  the same observable behaviour and this guard could not tell them apart.
* ``compare_predictors`` refuses to ship on a below-floor sample and DOES ship
  on an adequate one, with a live control for each.

``predict_structural`` is callable directly with no arguments, because
``context`` defaults to ``None``. That is a fact about the function, not a
selector: the requirement pinned here is that no CONFIGURATION path reaches it.

Requires no Docker, no provider, no network.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from runtime import difficulty as df
from runtime import model_router as mr

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CALIBRATION_FILE = _REPO_ROOT / "runtime" / "difficulty_structural_calibration.json"

_AUTO_CTX: Dict[str, Any] = {
    "adaptive_routing": True,
    "difficulty_estimator": "heuristic",
}
_SCARY = [{"role": "user", "content": "## Issue\ndeadlock race in the retry loop"}]


def _estimator(result: Mapping[str, Any]) -> str:
    return str((result.get("info") or {}).get("estimator") or "")


# -- the guard itself ------------------------------------------------------


def test_the_calibration_file_that_would_switch_auto_does_not_exist() -> None:
    """The file is the ONLY thing that makes `auto` resolve to the challenger."""
    assert not _CALIBRATION_FILE.exists(), (
        f"{_CALIBRATION_FILE.name} exists; that file is the only thing that makes "
        "'auto' resolve to the structural predictor, and on this repository the "
        "holdout comparison reports ship=False, so it must not be written"
    )


def test_no_calibration_is_loaded_from_any_rung() -> None:
    """A loader that could find a calibration somewhere else would be a second door."""
    calibrated = mr._structural_calibrated()
    assert calibrated in (None, False, "", {}, [])


def test_auto_resolves_to_the_incumbent_heuristic() -> None:
    """The DEFAULT path is what every unconfigured run in the project takes."""
    auto = mr._maybe_predict_difficulty(
        [{"role": "user", "content": "## Issue\nThe function returns the sum."}],
        dict(_AUTO_CTX),
    )
    assert _estimator(auto), "the default path produced no estimator at all"
    assert _estimator(auto) != "structural"


def test_explicit_structural_is_still_reachable_so_the_measurement_is_protected() -> (
    None
):
    """A guard that made the challenger unreachable would protect nothing."""
    explicit = mr._maybe_predict_difficulty(
        _SCARY, dict(_AUTO_CTX, difficulty_features="structural")
    )
    assert _estimator(explicit) == "structural"


def test_the_two_arms_are_distinguishable_or_the_switch_is_inert() -> None:
    """Without this the guard cannot tell unshipped from shipped."""
    auto = mr._maybe_predict_difficulty(_SCARY, dict(_AUTO_CTX))
    explicit = mr._maybe_predict_difficulty(
        _SCARY, dict(_AUTO_CTX, difficulty_features="structural")
    )
    assert _estimator(auto) != _estimator(explicit)


def test_no_difficulty_features_value_in_harness_defaults_can_switch_it_on() -> None:
    """A DEFAULTS entry is merged into EVERY task config and every eval arm."""
    from harness.config import DEFAULTS

    if "difficulty_features" in DEFAULTS:
        assert DEFAULTS["difficulty_features"] != "structural"


def test_no_environment_variable_selects_the_structural_predictor() -> None:
    """Env is a configuration path too, and it is the easiest one to forget."""
    for name in (
        "NEO_DIFFICULTY_FEATURES",
        "NEO_PREDICTOR",
        "NEO_DIFFICULTY_PREDICTOR",
    ):
        assert name not in os.environ, (
            f"{name} is set in this environment; if any module reads it, the "
            "structural predictor has a selector the guard cannot see"
        )
    import runtime.difficulty as difficulty_module
    import runtime.model_router as router_module

    for module in (difficulty_module, router_module):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "getenv" not in source.replace("os.getenv", "GETENV"), (
            f"{module.__name__} reads the environment; a difficulty-feature "
            "environment switch would be a selector this guard cannot see"
        )


def test_the_only_structural_selector_in_the_router_is_an_explicit_context_key() -> (
    None
):
    """AST, not a string count: `structural` lives in ONE function and nowhere else."""
    import ast

    path = Path(mr.__file__ or "")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents: Dict[Any, Any] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node: ast.AST) -> str:
        current: Any = node
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current.name
            current = parents.get(current)
        return "<module>"

    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and node.value == "structural"
        and not isinstance(parents.get(node), ast.Expr)
    ]
    assert literals, "the router no longer contains a 'structural' selector at all"
    assert {enclosing(node) for node in literals} <= {"_maybe_predict_difficulty"}, (
        "a 'structural' literal appeared outside _maybe_predict_difficulty: "
        f"{sorted({enclosing(n) for n in literals})}"
    )
    assert "difficulty_features" in source


# -- the shipping gate itself ---------------------------------------------


def _rows(labels: Sequence[str]) -> List[Dict[str, Any]]:
    """Build rows where the challenger is right and the incumbent is not."""
    rows: List[Dict[str, Any]] = []
    for index, label in enumerate(labels):
        group = f"bug-{index:02d}"
        rows.append(
            {
                "group_id": group,
                "task_id": group,
                "label": label,
                "legacy_hint": "easy",
                "structural_hint": "hard" if label == "hard" else "easy",
                "features_resolved": True,
            }
        )
    return rows


def test_a_win_below_the_declared_hard_label_floor_is_refused_as_a_ship_decision() -> (
    None
):
    """The live reproduction: two hard labels is not evidence enough to ship."""
    labels = ["hard"] * 4 + ["easy"] * 8
    report = df.compare_predictors(_rows(labels))
    assert report["winner"] == "structural", (
        "fixture no longer produces a challenger win"
    )
    assert report["decidable"] is True
    assert 0 < report["holdout_hard_labels"] < df.MIN_HOLDOUT_HARD_LABELS
    assert report["sample_adequate"] is False
    assert report["ship"] is False
    assert "promising-but-unproven" in report["honesty"]


def test_the_same_win_on_an_adequate_sample_does_ship() -> None:
    """The control: the gate is the SAMPLE, not a permanent refusal."""
    labels = ["hard"] * 12 + ["easy"] * 4
    report = df.compare_predictors(_rows(labels))
    assert report["winner"] == "structural"
    assert report["holdout_hard_labels"] >= df.MIN_HOLDOUT_HARD_LABELS
    assert report["sample_adequate"] is True
    assert report["ship"] is True


def test_no_rows_is_not_a_win_and_no_hard_label_is_not_a_win() -> None:
    """The degenerate arms must refuse rather than report a vacuous tie."""
    empty = df.compare_predictors([])
    assert empty["ship"] is False
    assert empty["decidable"] is False
    assert empty["reason"] == "no_observations"
    all_easy = df.compare_predictors(_rows(["easy"] * 12))
    assert all_easy["decidable"] is False
    assert all_easy["ship"] is False


def test_a_holdout_with_no_hard_label_cannot_validate_a_difficulty_predictor() -> None:
    """Not merely "unlucky in this sample" — unvalidatable in principle."""
    report = df.compare_predictors(
        _rows(["easy", "easy", "easy", "easy", "easy", "easy"])
    )
    assert report["decidable"] is False
    assert report["sample_adequate"] is False
    assert report["ship"] is False
    assert "no hard-labelled observation" in report["honesty"]


def test_the_floor_is_a_declared_constant_nobody_can_tune() -> None:
    """No measurement can move the floor; only more data can clear it."""
    assert df.MIN_HOLDOUT_HARD_LABELS == 3
    source = Path(df.__file__ or "").read_text(encoding="utf-8")
    assignments = [
        line for line in source.splitlines() if "MIN_HOLDOUT_HARD_LABELS =" in line
    ]
    assert assignments == ["MIN_HOLDOUT_HARD_LABELS = 3"]


def test_predict_structural_is_usable_directly_which_is_why_no_selector_may_exist() -> (
    None
):
    """The function is honest on its own; the CONFIGURATION is what must be closed."""
    hint, info = df.predict_structural()
    assert hint in ("easy", "medium", "hard")
    assert info["estimator"] == "structural"
    assert set(info["features"]) >= {
        "bug_class",
        "bug_class_weight",
        "fan_in",
        "changed_files",
        "distinct_touched_modules",
    }, sorted(info["features"])
    assert set(info["features"]["contributions"]) >= {
        "bug_class",
        "fan_in",
        "multi_module",
        "repo_size",
        "test_density",
    }, sorted(info["features"]["contributions"])


# -- the Phase 6 pin ------------------------------------------------------

#: T5 owns the shared suite. This is the exact test name that must be added
#: there, and the thing it must assert. Kept as data so a future Phase 6 cannot
#: discover the requirement by accident.
PHASE6_PIN_NAME = "test_phase6_revive_the_structural_predictor_is_a_deliberate_decision"

PHASE6_PIN_INTENT = """
When the structural difficulty predictor is revived, the following become true
simultaneously and each one alone is evidence of nothing:

* `runtime/difficulty_structural_calibration.json` exists (written by
  `evals.difficulty_holdout --apply` only when `ship: true`);
* `runtime.model_router._structural_calibrated()` returns a calibration;
* an UNCONFIGURED run resolves to `estimator == "structural"` instead of the
  incumbent heuristic.

That last one is the silent behaviour change: a routing decision made by
harness/config.py DEFAULT - the one every task and every eval arm inherits -
would change for the whole project. T5 must therefore add the test named
`{name}` to the shared suite, INVERTED so that it FAILS the moment the
calibration file appears, and flip it in the same change that ships the
predictor, with `python -m evals.difficulty_holdout` re-run first so the
shipped decision rests on an ADEQUATE hard-label count rather than one lucky
prediction. Until then the guards in this module hold the line.
""".strip()


def test_the_phase6_pin_is_named_and_its_intent_is_recorded() -> None:
    """A recorded debt somebody can act on; an unrecorded one gets rediscovered."""
    assert PHASE6_PIN_NAME == (
        "test_phase6_revive_the_structural_predictor_is_a_deliberate_decision"
    )
    assert PHASE6_PIN_NAME.islower()
    assert "deliberate_decision" in PHASE6_PIN_NAME
    assert PHASE6_PIN_NAME not in globals(), (
        "the Phase 6 pin is a NAME to add to tests/, not a test to run here; "
        "defining it here would let it silently pass forever"
    )
    for phrase in ("difficulty_structural_calibration.json", "ship: true", "INVERTED"):
        assert phrase in PHASE6_PIN_INTENT


def test_the_phase6_marker_is_not_accidentally_satisfied_today() -> None:
    """Today the file is absent; the inverted pin must therefore be RED."""
    assert not _CALIBRATION_FILE.exists()
    with pytest.raises(AssertionError):
        assert _CALIBRATION_FILE.exists()
