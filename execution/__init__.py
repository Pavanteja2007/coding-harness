"""Terminal 2 — execution module: sandboxed runs, verification, git output,
and rationale logging. Public API is the INTERFACES.md Boundary 1 contract
(plus this module's own git/rationale helpers); see execution/AGENTS.md.

Import the functions from their submodules directly:
    from execution.sandbox import execute_sandboxed
    from execution.verify import verify

NOTE: `verify` is deliberately NOT re-exported here (no
`from execution.verify import verify`): that would bind the package
attribute `execution.verify` to the FUNCTION, shadowing the submodule of
the same name and breaking every `import execution.verify` /
`from execution.verify import X` statement executed afterwards.
"""
from execution.sandbox import ensure_image, execute_sandboxed

__all__ = ["execute_sandboxed", "ensure_image"]
