"""Child driver: hold a repository guard so a test can collide with it.

Run as ``python tests/platform_lock_driver.py <repo> <home> <ready> <hold_s>``.

It takes the single-writer guard for ``<repo>`` in ITS OWN process, writes
``<ready>`` once the record is on disk, and then holds for ``<hold_s``
seconds. Tests use this instead of taking the lease in-process, because
``acquire_repository_lock`` is deliberately RE-ENTRANT per thread and process:
a same-process "peer" is not a peer, and a test that collided with one would
prove only that the guard does not fire on its own author.

Nothing here is a pytest module; it is never collected.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import instance_guard


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        sys.stderr.write(
            "usage: platform_lock_driver.py <repo> <home> <ready> [hold_s]\n"
        )
        return 2
    repo, home, ready = argv[0], argv[1], argv[2]
    hold_s = float(argv[3]) if len(argv) > 3 else 60.0
    lease = instance_guard.acquire_repository_lock(
        repo,
        owner="tests.platform_lock_driver",
        command="neo (a second session)",
        session_id="sess-peer001",
        home=home,
    )
    Path(ready).write_text(
        json.dumps(
            {
                "lock_path": str(lease.path),
                "pid": os.getpid(),
                "token_is_mine": True,
            }
        ),
        encoding="utf-8",
    )
    deadline = time.monotonic() + max(0.5, hold_s)
    while time.monotonic() < deadline:
        time.sleep(0.05)
    lease.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
