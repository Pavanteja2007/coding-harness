"""Real-crash driver for the R2-06 atomic-write proof.

Run as: ``python tests/editor_crash_driver.py <repo_root> <relative_path>
<old_string> <new_string>``.

The edit is attempted through ``harness.editor.apply_text_edit`` and the
repository's real atomic-write primitive, with ``os.replace`` replaced by
``os._exit(70)`` so the process dies at the exact instant the rename would have
made the new content visible. That is the only moment a caller can lose a file,
and faking the write instead would only prove the fake.

Exit codes: 70 = the simulated crash fired (the expected outcome);
0 = the edit returned without reaching the rename (the primitive is NOT atomic
and the test fails); anything else = a harness error printed to stderr.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import editor


def main() -> int:
    if len(sys.argv) < 5:
        sys.stderr.write(
            "usage: editor_crash_driver.py <repo_root> <relative> <old> <new>\n"
        )
        return 2
    root, relative, old, new = sys.argv[1:5]

    import execution.workspace as workspace_module

    def _die(*_args: object, **_kwargs: object) -> None:
        os._exit(70)

    workspace_module.os.replace = _die  # type: ignore[attr-defined]

    session = editor.EditSession()
    session.note_read(relative, root=root)
    outcome = editor.apply_text_edit(root, relative, old, new, session=session)
    sys.stdout.write(f"returned ok={outcome.ok} kind={outcome.error_kind}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
