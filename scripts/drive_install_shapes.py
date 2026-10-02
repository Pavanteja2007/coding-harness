"""Drive the two install shapes and report which directories appear.

Shape A: a brand-new user, no previous state anywhere.
Shape B: a user upgrading an existing `.vex` repository.

The claim under test is the one the product needs to be true: the folder
the product WRITES is the same folder it READS, and an upgrade creates no
second, competing directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def drive(label: str, pre_create_legacy: bool) -> None:
    base = Path(tempfile.mkdtemp(prefix="neo_shape_"))
    home = base / "home"
    repo = base / "repo"
    home.mkdir()
    (repo / ".git").mkdir(parents=True)
    if pre_create_legacy:
        (repo / ".vex" / "commands").mkdir(parents=True)
        (repo / ".vex" / "settings.toml").write_text(
            '# legacy settings the user already had\nmodel = "legacy-model"\n',
            encoding="utf-8",
        )
    os.environ["NEO_HOME"] = str(home / "state")
    os.environ.pop("NEO_PROJECT_DIR", None)
    os.environ.pop("VEX_PROJECT_DIR", None)

    from cli import neoconfig

    info = neoconfig.maybe_scaffold_repo(repo)
    read_dir = neoconfig.project_settings_dir(repo)
    effective = neoconfig.effective_settings(start=repo)

    print(f"--- {label} ---")
    print(
        f"  dirs now in repo      : {sorted(p.name for p in repo.iterdir() if p.is_dir())}"
    )
    print(f"  scaffolded            : {info.get('created') if info else None}")
    print(f"  READS project tier at : {read_dir}")
    print(f"  settings model in use : {effective.get('model')!r}")
    names = sorted(p.name for p in repo.iterdir() if p.is_dir())
    both = ".neo" in names and ".vex" in names
    print(f"  SPLIT BRAIN (both .neo and .vex present): {both}")
    print()
    shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    print("=== the two install shapes ===\n")
    drive("SHAPE A: brand-new user", pre_create_legacy=False)
    drive("SHAPE B: upgrade from .vex", pre_create_legacy=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
