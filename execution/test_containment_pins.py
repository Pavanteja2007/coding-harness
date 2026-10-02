"""T2.W2.1 — every containment claim pinned so a future edit cannot weaken it.

Wave 1 attacked each claim and recorded the outcome in
``execution/containment_audit.py``. This module converts **every row of that
attack table** into an assertion that fails when the claim is weakened, and it
does so **at the argv level**, without a Docker daemon: the argv is what the
daemon is asked to do, and ``assert_sandbox_argv_isolated`` already exists as
the pre-spawn gate over exactly that object. A containment receipt that can be
weakened by an innocuous edit is a receipt that will be weakened.

The seven pinned invariants
---------------------------

===  ==================================  ==========================================
#   claim                                the assertion that breaks if it is weakened
===  ==================================  ==========================================
1    VCS metadata is read-only inside a   every overlay mount spec ends ``:ro``, and
     writable workspace                   a hand-made ``:rw`` overlay is REFUSED
2    a writable root never overlaps a     the resolved set excludes it AND a note
     read-only path                       records the conflict
3    an unusable writable path is         the normalised value is ``""`` (not a clamp)
     DROPPED with a note
4    an absent read-only path is          it is not in the mount list, and the
     reported absent, not mounted         receipt says ``readonly_absent``
5    network is off unless declared       ``--network none`` is in the argv
6    declaring writable roots makes the   both mounts are present: workspace ``:ro``
     workspace mount itself ``:ro``       and each declared root ``:rw``
7    a FRESH container per call          ``--rm`` + a unique ``--name``, and no
                                          production call site reuses a container
===  ==================================  ==========================================

Two properties every pin here depends on
----------------------------------------

**Non-vacuity.** A pre-spawn gate that accepts everything is a gate that has
stopped working, and every "the product's own argv is accepted" assertion would
pass. :func:`_gate_accepts_the_products_own_argv` therefore runs the SAME
fixture argv through a deliberately weakened mutation and requires the gate to
refuse it. If the gate ever stops refusing, the acceptance pins become
meaningless and this fails first.

**The two halves agree.** ``resolve_containment`` decides and
``assert_sandbox_argv_isolated`` re-checks; a policy that resolved correctly
while the gate refused its own argv would mean the two halves disagree. Every
argv pin asserts the gate ACCEPTS the product's own argv for the same policy
it resolved.

Run: ``python -m pytest execution/test_containment_pins.py -q``
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import pytest

from execution import warm_sandbox
from execution.sandbox import (
    CONTAINER_PREFIX,
    DEFAULT_READONLY_SUBPATHS,
    MOUNT_POINT,
    SandboxDependencyError,
    _docker_run_args,
    _normalize_repo_relative,
    _split_volume_spec,
    assert_sandbox_argv_isolated,
    containment_receipt,
    readonly_overlays,
    resolve_containment,
    writable_overlays,
)

#: Every VCS-metadata directory the containment policy declares read-only. The
#: list is read from the policy rather than restated, so a future edit that adds
#: a directory to ``DEFAULT_READONLY_SUBPATHS`` is covered by these pins for
#: free, and one that REMOVES one is caught by
#: :func:`test_the_readonly_vcs_set_is_itself_pinned`.
EXPECTED_READONLY_VCS: Tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".bzr",
    "_darcs",
    ".neo",
)


def _repo(tmp_path: Path, *, subpaths: Sequence[str] = EXPECTED_READONLY_VCS) -> Path:
    """Return a fixture repository carrying every declared read-only directory.

    A fixture without ``.git`` would make the read-only claim untestable in the
    OTHER direction: ``readonly_overlays`` only mounts EXISTING directories, so
    an absent one silently disappears and a weak policy passes.
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "mymod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pin"\nversion = "0"\n', encoding="utf-8"
    )
    for relative in subpaths:
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        (target / "preexisting").write_text("original\n", encoding="utf-8")
    return root


def _mounts(argv: Sequence[str]) -> List[str]:
    """Return every ``--volume``/``-v`` spec in the argv, in order."""
    specs: List[str] = []
    for index, token in enumerate(argv):
        if str(token) in ("--volume", "-v") and index + 1 < len(argv):
            specs.append(str(argv[index + 1]))
    return specs


def _build_argv(root: Path, policy, *, image: str = "pin:image") -> List[str]:
    """Build the argv ``execute_sandboxed`` would spawn for this policy."""
    readonly = readonly_overlays(policy)
    return _docker_run_args(
        image,
        str(root),
        60,
        policy.network_enabled,
        None,
        "1g",
        1.0,
        512,
        workspace_readonly=policy.workspace_readonly,
        readonly_mounts=readonly,
        writable_mounts=writable_overlays(policy),
    )


def _gate_accepts_the_products_own_argv(root: Path, policy) -> None:
    """Require the pre-spawn gate to ACCEPT the argv this policy produces.

    The gate is a SECOND, INDEPENDENT authority over the same object. A policy
    whose argv the gate refuses means the deciding half and the checking half
    disagree, which is a defect in one of them and cannot be told apart from
    here — so it is a failure either way.
    """
    assert_sandbox_argv_isolated(
        _build_argv(root, policy), str(root), writable_paths=policy.writable_roots
    )


def _gate_refuses(argv: Sequence[str], root: Path, **kwargs) -> str:
    """Require the pre-spawn gate to REFUSE this argv; return its reason."""
    try:
        assert_sandbox_argv_isolated(list(argv), str(root), **kwargs)
    except SandboxDependencyError as exc:
        return str(exc)
    raise AssertionError(
        "the pre-spawn isolation gate ACCEPTED an argv it must refuse; every "
        "acceptance assertion in this module is now vacuous"
    )


# ---------------------------------------------------------------------------
# Non-vacuity — run FIRST so a broken gate is reported before anything it hides
# ---------------------------------------------------------------------------


def test_the_gate_is_not_a_rubber_stamp_before_anything_else_is_asserted(
    tmp_path: Path,
) -> None:
    """Prove the pre-spawn gate still refuses, before any pin relies on it.

    Four independent mutations, one per refusal class, so a gate that only
    grew ONE of its rules still fails here:
    a read-write in-workspace mount with no declared writable root, a
    ``--privileged`` flag, a host network namespace, and a secret-directory
    mount.
    """
    root = _repo(tmp_path)
    workspace = f"{root.as_posix()}:{MOUNT_POINT}".replace("\\", "/")

    # 1. a read-write in-workspace mount the policy never declared
    _gate_refuses(
        [
            "run",
            "--volume",
            workspace,
            "--volume",
            f"{(root / '.git').as_posix()}:{MOUNT_POINT}/.git:rw",
        ],
        str(root),
    )
    # 2. privilege widening
    _gate_refuses(["run", "--privileged", "--volume", workspace], str(root))
    # 3. a host namespace
    _gate_refuses(["run", "--network", "host", "--volume", workspace], str(root))
    # 4. a host secret directory
    _gate_refuses(
        ["run", "--volume", workspace, "--volume", "/home/v/.ssh:/keys:ro"],
        str(root),
    )
    # ...and an in-workspace mount with NO explicit mode, which docker defaults
    # to read-write. This is the exact shape a lost ":ro" produces.
    _gate_refuses(
        [
            "run",
            "--volume",
            workspace,
            "--volume",
            f"{(root / '.git').as_posix()}:{MOUNT_POINT}/.git",
        ],
        str(root),
    )


# ---------------------------------------------------------------------------
# Row 1 — VCS metadata is read-only INSIDE a writable workspace
# ---------------------------------------------------------------------------


def test_the_readonly_vcs_set_is_itself_pinned() -> None:
    """Pin the SET, so removing a directory from the policy fails here.

    Without this, every other pin in this row would pass vacuously against a
    shorter list: an empty ``readonly_subpaths`` produces an argv with no
    overlays and therefore no ``:ro`` to assert on.
    """
    assert tuple(DEFAULT_READONLY_SUBPATHS) == EXPECTED_READONLY_VCS


def test_vcs_metadata_is_mounted_read_only_inside_a_writable_workspace(
    tmp_path: Path,
) -> None:
    """Every declared read-only subtree carries ``:ro``; the workspace does not.

    The workspace mount must stay READ-WRITE, because that is what lets an edit
    persist for host-side diffing. The read-only overlays are appended AFTER it
    so Docker applies the deeper target over the shallower one.
    """
    root = _repo(tmp_path)
    policy = resolve_containment(str(root))
    argv = _build_argv(root, policy)
    specs = _mounts(argv)

    workspace = next(
        spec
        for spec in specs
        if spec.endswith(MOUNT_POINT) or f":{MOUNT_POINT}" in spec
    )
    assert not workspace.endswith(":ro"), (
        "the workspace mount is read-only with NO declared writable root; agent "
        "edits could not persist for host-side diffing"
    )

    for relative in EXPECTED_READONLY_VCS:
        expected_target = f"{MOUNT_POINT}/{relative}"
        spec = next(
            (item for item in specs if _split_volume_spec(item)[1] == expected_target),
            None,
        )
        assert spec is not None, (
            f"{relative!r} was declared read-only but carries no mount at all; "
            f"the argv was {specs!r}"
        )
        assert spec.endswith(":ro"), (
            f"{relative!r} is mounted {spec!r} — WITHOUT ':ro'. docker defaults a "
            "-v mount to read-write, so this silently re-opens the one tree "
            "whose forgery changes what a reviewer believes happened"
        )
    _gate_accepts_the_products_own_argv(root, policy)


def _rewrite_mount_modes(
    argv: Sequence[str], suffix: str, replacement: str
) -> Tuple[List[str], List[str]]:
    """Return the argv with every ``:<suffix>`` mount mode replaced.

    This is the literal edit a future weakening makes — ``:ro`` becomes ``:rw``,
    or ``:rw`` loses its mode entirely — applied to the argv the product would
    actually spawn, so the demonstration is against the real object and not
    against a hand-written imitation of it.
    """
    rebuilt: List[str] = []
    modes: List[str] = []
    for index, token in enumerate(argv):
        if str(token) in ("--volume", "-v") and index + 1 < len(argv):
            spec = str(argv[index + 1])
            if spec.endswith(suffix):
                modes.append(spec)
                spec = spec[: -len(suffix)] + replacement
            rebuilt += [token, spec]
        else:
            rebuilt.append(token)
    return rebuilt, modes


def test_a_read_write_vcs_overlay_is_refused_by_the_pre_spawn_gate(
    tmp_path: Path,
) -> None:
    """The one-line weakening of row 1 — ``:ro`` changed to ``:rw`` — fails.

    This is the demonstration shape referenced in the module docstring: the
    weakening is applied to the real argv, and the gate must refuse it.
    """
    root = _repo(tmp_path)
    policy = resolve_containment(str(root))
    argv = _build_argv(root, policy)

    weakened, modes = _rewrite_mount_modes(argv, ":ro", ":rw")
    assert len(modes) >= len(EXPECTED_READONLY_VCS), (
        "the fixture produced too few read-only overlays for the demonstration "
        f"to mean anything: {modes!r}"
    )
    _gate_accepts_the_products_own_argv(root, policy)  # control: the real one passes
    _gate_refuses(weakened, str(root), writable_paths=policy.writable_roots)


# ---------------------------------------------------------------------------
# Row 2 — a declared writable root never overlaps a read-only path
# ---------------------------------------------------------------------------


def test_a_writable_root_overlapping_a_readonly_path_is_recorded_and_loses(
    tmp_path: Path,
) -> None:
    """The read-only side wins, the conflict is RECORDED, nothing is widened."""
    root = _repo(tmp_path)
    hostile: Sequence[str] = (".git", ".git/refs", ".hg", ".hg/stacks/0")
    policy = resolve_containment(str(root), writable_roots=hostile)

    assert ".git" not in policy.writable_roots
    assert ".git/refs" not in policy.writable_roots
    assert ".hg" not in policy.writable_roots
    assert ".hg/stacks/0" not in policy.writable_roots

    overlap_notes = [note for note in policy.notes if "overlaps" in note]
    assert len(overlap_notes) >= 4, (
        "each conflicting declaration must be RECORDED, not silently dropped: "
        f"notes={list(policy.notes)!r}"
    )
    # And the resulting argv still refuses the conflicting paths.
    for spec in _mounts(_build_argv(root, policy)):
        _, target, mode = _split_volume_spec(spec)
        if mode == "rw":
            assert not target.startswith(f"{MOUNT_POINT}/.git")
            assert not target.startswith(f"{MOUNT_POINT}/.hg")
        # Every OVERLAY (a target deeper than /workspace) must carry an
        # explicit mode; the workspace mount itself is the one that may rely on
        # docker's read-write default, and only because nothing was declared.
        if target != MOUNT_POINT:
            assert mode in ("ro", "rw"), (
                f"overlay {spec!r} carries no explicit mode, so docker defaults it "
                "to read-write"
            )
    _gate_accepts_the_products_own_argv(root, policy)


# ---------------------------------------------------------------------------
# Row 3 — an unusable writable path is DROPPED with a note, never clamped
# ---------------------------------------------------------------------------

UNUSABLE_PATHS: Tuple[str, ...] = (
    "/etc",
    "/workspace/src",
    "../outside",
    "src/../../escape",
    "",
    "   ",
    "C:\\Windows",
    "c:/windows",
    ".",
    "./",
)


def test_an_unusable_writable_path_normalises_to_nothing_rather_than_a_clamp() -> None:
    """``_normalize_repo_relative`` refuses; it does not resolve into the repo.

    A clamp is the dangerous shape: it reports a boundary different from the one
    enforced, and a reader of the receipt would believe the clamp.
    """
    for candidate in UNUSABLE_PATHS:
        assert _normalize_repo_relative(candidate) == "", (
            f"{candidate!r} was clamped to {_normalize_repo_relative(candidate)!r} "
            "instead of being refused"
        )
    # The control: an ordinary relative path is NOT refused.
    assert _normalize_repo_relative("src") == "src"
    assert _normalize_repo_relative("src/pkg/") == "src/pkg"


def test_an_unusable_writable_root_is_dropped_with_a_note(tmp_path: Path) -> None:
    """Every hostile declaration leaves a note; none reaches the mount list."""
    root = _repo(tmp_path)
    (root / "src").mkdir(exist_ok=True)
    policy = resolve_containment(
        str(root), writable_roots=("/etc", "../outside", "", "C:\\Windows")
    )
    refusal_notes = [
        note for note in policy.notes if "refused a writable-root declaration" in note
    ]
    assert len(refusal_notes) == 4, (
        f"a dropped declaration must be RECORDED: notes={list(policy.notes)!r}"
    )
    assert policy.writable_roots == ()
    # ...and refusals must not have flipped the workspace read-only, which would
    # turn a typo into a run that cannot edit anything.
    assert policy.workspace_readonly is False
    specs = _mounts(_build_argv(root, policy))
    assert not any(spec.endswith(":rw") for spec in specs), (
        f"a refused declaration still produced a read-write mount: {specs!r}"
    )
    for spec in specs:
        _, target, _mode = _split_volume_spec(spec)
        assert target in (
            MOUNT_POINT,
            *(f"{MOUNT_POINT}/{n}" for n in EXPECTED_READONLY_VCS),
        )


def test_an_unusable_readonly_declaration_is_dropped_with_a_note(
    tmp_path: Path,
) -> None:
    """The read-only side has the same drop-with-a-note rule.

    An explicit ``readonly_paths`` list REPLACES the module default rather than
    adding to it — that distinction is the reason ``None`` and ``[]`` are not
    the same value, so this asserts both halves: a wholly unusable list resolves
    to nothing AND records three refusals, while a partially unusable list keeps
    the entries it could resolve.
    """
    root = _repo(tmp_path)

    wholly_unusable = resolve_containment(
        str(root), readonly_paths=("/etc", "../outside", "")
    )
    refusal_notes = [
        note
        for note in wholly_unusable.notes
        if "refused a read-only path declaration" in note
    ]
    assert len(refusal_notes) == 3, f"notes={list(wholly_unusable.notes)!r}"
    assert wholly_unusable.readonly_subpaths == ()
    # ...and with nothing declared read-only, the ONLY mount is the workspace.
    assert [
        _split_volume_spec(spec)[1]
        for spec in _mounts(_build_argv(root, wholly_unusable))
    ] == [MOUNT_POINT]

    partially_unusable = resolve_containment(
        str(root), readonly_paths=("/etc", ".git", "../outside")
    )
    assert partially_unusable.readonly_subpaths == (".git",)
    assert (
        len(
            [
                note
                for note in partially_unusable.notes
                if "refused a read-only path declaration" in note
            ]
        )
        == 2
    )
    targets = [
        _split_volume_spec(spec)[1]
        for spec in _mounts(_build_argv(root, partially_unusable))
    ]
    assert targets == [MOUNT_POINT, f"{MOUNT_POINT}/.git"]


# ---------------------------------------------------------------------------
# Row 4 — an ABSENT read-only path is reported absent, not mounted
# ---------------------------------------------------------------------------


def test_an_absent_readonly_path_is_not_mounted_because_docker_would_create_it(
    tmp_path: Path,
) -> None:
    """``docker run -v`` CREATES a missing host path, so an absent one is a
    real-repository-corruption risk: it would silently add ``.not-a-real-dir``
    to the user's tree. It must be reported absent instead."""
    root = _repo(tmp_path, subpaths=(".git",))
    absent_name = ".not-a-real-dir"
    assert not (root / absent_name).exists()

    policy = resolve_containment(str(root), readonly_paths=(".git", absent_name))
    assert absent_name in policy.readonly_subpaths, (
        "the ABSENT path must still be DECLARED; dropping it from the receipt "
        "would report 'nothing to protect' instead of 'not present'"
    )

    readonly = readonly_overlays(policy)
    applied_targets = [target for _, target in readonly]
    assert f"{MOUNT_POINT}/.git" in applied_targets
    assert f"{MOUNT_POINT}/{absent_name}" not in applied_targets, (
        "the absent path was MOUNTED; docker run -v would create it inside the "
        "user's repository"
    )
    for source, _ in readonly:
        assert Path(source).is_dir()

    receipt = containment_receipt(policy, applied=readonly)
    assert receipt["readonly_applied"] == [".git"], receipt
    assert receipt["readonly_absent"] == [absent_name], receipt

    argv = _build_argv(root, policy)
    assert not any(absent_name in spec for spec in _mounts(argv)), _mounts(argv)
    assert not (root / absent_name).exists(), (
        "resolving and building the argv CREATED the absent directory"
    )
    _gate_accepts_the_products_own_argv(root, policy)


def test_an_absent_writable_root_is_also_not_mounted(tmp_path: Path) -> None:
    """The same rule on the writable side: mounting a missing root creates it."""
    root = _repo(tmp_path, subpaths=(".git",))
    (root / "src").mkdir(exist_ok=True)
    policy = resolve_containment(
        str(root), readonly_paths=[".git"], writable_roots=("src", "does-not-exist")
    )
    assert set(policy.writable_roots) == {"src", "does-not-exist"}
    writable = writable_overlays(policy)
    assert [target for _, target in writable] == [f"{MOUNT_POINT}/src"]
    assert not any(
        "does-not-exist" in spec for spec in _mounts(_build_argv(root, policy))
    )
    assert not (root / "does-not-exist").exists()


# ---------------------------------------------------------------------------
# Row 5 — network is off unless a declaration turns it on
# ---------------------------------------------------------------------------


def test_the_network_is_off_unless_a_declaration_turns_it_on(tmp_path: Path) -> None:
    """The default argv carries ``--network none``; a declaration removes it."""
    root = _repo(tmp_path)

    default_policy = resolve_containment(str(root))
    assert default_policy.network_enabled is False
    default_argv = _build_argv(root, default_policy)
    assert "--network" in default_argv
    assert default_argv[default_argv.index("--network") + 1] == "none"
    _gate_accepts_the_products_own_argv(root, default_policy)

    declared_policy = resolve_containment(
        str(root), allow_network=True, network_declaration="caller"
    )
    assert declared_policy.network_enabled is True
    assert declared_policy.network_declaration == "caller"
    declared_argv = _build_argv(root, declared_policy)
    assert "--network" not in declared_argv, (
        "a declared network must OMIT '--network none', not pass it and then "
        "hope the later flag wins"
    )
    _gate_accepts_the_products_own_argv(root, declared_policy)


def test_the_network_absence_is_also_recorded_on_the_receipt(tmp_path: Path) -> None:
    """The receipt NAMES the declaration, so 'no network' is not an assumption."""
    root = _repo(tmp_path)
    off = resolve_containment(str(root)).to_dict()
    assert off["network_enabled"] is False
    assert off["network_declaration"] == "none"
    assert off["axis"] == "containment"

    on = resolve_containment(str(root), allow_network=True).to_dict()
    assert on["network_enabled"] is True
    assert on["network_declaration"] == "caller"
    assert any("not an enforced allowlist" in note for note in on["notes"]) or any(
        "egress allowlist" in note for note in on["notes"]
    )


# ---------------------------------------------------------------------------
# Row 6 — declaring writable roots makes the workspace mount :ro itself
# ---------------------------------------------------------------------------


def test_declaring_writable_roots_makes_the_workspace_mount_read_only(
    tmp_path: Path,
) -> None:
    """Both mounts must be present: ``/workspace:ro`` AND ``/workspace/src:rw``."""
    root = _repo(tmp_path, subpaths=(".git",))
    (root / "src").mkdir(exist_ok=True)
    policy = resolve_containment(
        str(root), readonly_paths=[".git"], writable_roots=("src",)
    )
    assert policy.workspace_readonly is True

    argv = _build_argv(root, policy)
    specs = _mounts(argv)
    by_target: Dict[str, str] = {}
    for spec in specs:
        _, target, mode = _split_volume_spec(spec)
        by_target[target] = mode

    assert by_target.get(MOUNT_POINT) == "ro", (
        f"declaring writable roots did not make the workspace mount :ro: {specs!r}"
    )
    assert by_target.get(f"{MOUNT_POINT}/src") == "rw", (
        f"the declared writable root is not :rw: {specs!r}"
    )
    assert by_target.get(f"{MOUNT_POINT}/.git") == "ro", (
        f"the read-only overlay lost its :ro once writable roots were declared: "
        f"{specs!r}"
    )
    # ORDER: overlays come after the workspace mount so Docker's deeper-target
    # rule narrows rather than being shadowed.
    targets = [_split_volume_spec(spec)[1] for spec in specs]
    assert targets.index(MOUNT_POINT) < targets.index(f"{MOUNT_POINT}/src")
    _gate_accepts_the_products_own_argv(root, policy)


def test_the_readonly_policy_workspace_and_overlays_stay_compatible(
    tmp_path: Path,
) -> None:
    """The declared-writable arm does not weaken the default arm.

    Two different policies, both resolved from the same repository: the default
    must leave the workspace writable AND ``.git`` read-only, while the
    declared-writable arm must flip exactly one thing and no other.
    """
    root = _repo(tmp_path)
    (root / "src").mkdir(exist_ok=True)

    default_specs = _mounts(_build_argv(root, resolve_containment(str(root))))
    default_by_target = {
        _split_volume_spec(s)[1]: _split_volume_spec(s)[2] for s in default_specs
    }

    declared = resolve_containment(str(root), writable_roots=("src",))
    declared_by_target = {
        _split_volume_spec(s)[1]: _split_volume_spec(s)[2]
        for s in _mounts(_build_argv(root, declared))
    }

    assert default_by_target[MOUNT_POINT] == ""  # no mode == docker's read-write
    assert declared_by_target[MOUNT_POINT] == "ro"
    for relative in EXPECTED_READONLY_VCS:
        assert default_by_target[f"{MOUNT_POINT}/{relative}"] == "ro"
        assert declared_by_target[f"{MOUNT_POINT}/{relative}"] == "ro"


# ---------------------------------------------------------------------------
# Row 7 — a FRESH container per call, and no call site reuses one
# ---------------------------------------------------------------------------

#: ``docker`` verbs that would make a call REUSE a container instead of
#: creating one. ``run --rm`` is the only shape this module permits.
REUSING_DOCKER_VERBS: Tuple[str, ...] = (
    "exec",
    "start",
    "attach",
    "restart",
    "commit",
    "cp",
)


def test_every_call_builds_a_fresh_rem_container_with_a_unique_name(
    tmp_path: Path,
) -> None:
    """``--rm`` plus a fresh ``--name`` per call: no container is reused."""
    root = _repo(tmp_path)
    policy = resolve_containment(str(root))

    first = _build_argv(root, policy)
    second = _build_argv(root, policy)
    for argv in (first, second):
        assert argv[0] == "run"
        assert "--rm" in argv
        assert "--name" in argv
        name = argv[argv.index("--name") + 1]
        assert name.startswith(CONTAINER_PREFIX), name

    def _name(argv: Sequence[str]) -> str:
        return str(argv[argv.index("--name") + 1])

    assert _name(first) != _name(second), (
        "two calls built the SAME container name; a name is how a reused "
        "container would be found instead of created"
    )
    # ...and the detached flag must never be used on this path.
    for argv in (first, second):
        assert "-d" not in argv
        assert "--detach" not in argv


def _production_modules() -> List[Path]:
    """Return every non-test module of ``execution/``."""
    here = Path(__file__).resolve().parent
    return sorted(p for p in here.glob("*.py") if not p.name.startswith("test_"))


def test_no_production_call_site_reuses_a_container() -> None:
    """AST-scan ``execution/`` for a reuse verb outside the declared owner.

    ``execution/warm_sandbox.py`` is the ONE documented reuse surface and it is
    declared by name below; every other production module must not be able to
    reach ``docker exec``/``start``/``attach``/``commit`` at all. A grep would
    match the drivers' own prose, so this is an AST pass over executable string
    literals only.
    """
    declared_reuse_owner = "warm_sandbox.py"
    offences: List[str] = []
    for path in _production_modules():
        if path.name == declared_reuse_owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if not text.startswith("docker "):
                    continue
                parts = text.split()
                if len(parts) > 1 and parts[1] in REUSING_DOCKER_VERBS:
                    offences.append(f"{path.name}:{node.lineno}: {text!r}")
    assert not offences, (
        "a production module can reach a container-REUSING docker verb: "
        f"{offences!r}. A fresh container per call is the containment claim; "
        f"only {declared_reuse_owner} may reuse, and only for the agent step loop"
    )


def test_the_verification_path_does_not_go_through_the_warm_container(
    tmp_path: Path,
) -> None:
    """``warm_sandbox`` refuses the verification purpose at CONSTRUCTION.

    The verifier is the gate that mints a verified completion, so it must run a
    fresh container per command. Pinning the refusal is what makes "the warm
    container never touches the gate" structural rather than conventional, and
    the refusal must arrive at construction — a per-call check would still let
    a container be started first.
    """
    root = _repo(tmp_path)
    with pytest.raises(warm_sandbox.WarmSandboxError) as caught:
        warm_sandbox.WarmTaskSandbox(
            repo_path=str(root),
            task_id="t-pin",
            image="pin:image",
            purpose="verification",
        )
    refusal = str(caught.value)
    assert "fresh container per command" in refusal, refusal
    assert "verification" not in warm_sandbox.ALLOWED_PURPOSES
    assert frozenset({warm_sandbox.AGENT_STEP_PURPOSE}) == warm_sandbox.ALLOWED_PURPOSES


def test_a_hostile_warm_task_keeps_per_command_isolation(tmp_path: Path) -> None:
    """``hostile=True`` / ``reuse=False`` must never START a container.

    The control for the row above: a warm sandbox that refused nothing would
    also pass a "it refuses verification" test, so the reuse surface is proven
    to be OPT-OUT rather than opt-in.
    """
    root = _repo(tmp_path)
    for kwargs in ({"hostile": True}, {"reuse": False}):
        warm = warm_sandbox.WarmTaskSandbox(
            repo_path=str(root),
            task_id=f"t-hostile-{sorted(kwargs)[0]}",
            image="pin:image",
            **kwargs,
        )
        stats = warm.stats()
        assert stats["container"] in ("", None), stats
        assert stats["warm_exec_count"] == 0
        assert stats["per_command_count"] == 0
        assert stats["released"] is False, "nothing has been released yet"
        assert warm_sandbox.live_warm_identity() is None
        assert warm.release() is False, (
            "release() reports whether a container was REMOVED; nothing was ever "
            "started, so False is the honest answer and not a failure"
        )
        assert warm.stats()["released"] is True
        with pytest.raises(warm_sandbox.WarmSandboxError):
            warm.run("echo never")


# ---------------------------------------------------------------------------
# The receipt is axis (a) alone — no prompting field can appear on it
# ---------------------------------------------------------------------------

#: Fields a CONTAINMENT receipt must never carry. A receipt that also said
#: "no prompt was needed" would be the exact conflation the axis split removes.
PROMPTING_FIELDS: Tuple[str, ...] = (
    "requires_human",
    "approval",
    "approved",
    "prompted",
    "user_confirmed",
    "ask",
)


def test_the_containment_receipt_carries_no_prompting_field(tmp_path: Path) -> None:
    """``ContainmentPolicy.to_dict()`` must stay axis (a) only."""
    root = _repo(tmp_path)
    policy = resolve_containment(str(root))
    for receipt in (policy.to_dict(), containment_receipt(policy)):
        for field in PROMPTING_FIELDS:
            assert field not in receipt, (
                f"the containment receipt gained a prompting field {field!r}: "
                f"keys={sorted(receipt)!r}"
            )
        assert receipt["axis"] == "containment"
        assert receipt["rootfs_readonly"] is True


def test_the_argv_still_carries_the_baseline_isolation_flags(tmp_path: Path) -> None:
    """Containment is more than the mounts; the fixed flag set is pinned too.

    A future edit that drops ``--read-only``, ``--cap-drop ALL`` or
    ``--security-opt no-new-privileges`` weakens the boundary without touching
    a single mount, so the mount pins above would not notice.
    """
    root = _repo(tmp_path)
    argv = _build_argv(root, resolve_containment(str(root)))
    joined = " ".join(argv)
    for required in (
        "--rm",
        "--pull=never",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--tmpfs /tmp:rw,exec,size=256m",
        "--memory-swap",
        "--pids-limit",
        "--cpus",
    ):
        assert required in joined, f"the argv lost {required!r}: {joined!r}"
    assert re.search(r"--user\s+\S+", joined), joined
    assert "--workdir /workspace" in joined
