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

from execution.sandbox import (
    SandboxDependencyError,
    SandboxUnavailableError,
    ensure_image,
    execute_sandboxed,
)
from execution.warm_sandbox import (
    IdentityMismatch,
    SandboxIdentity,
    WarmSandboxError,
    WarmTaskSandbox,
    warm_sandbox_available,
)
from execution.workspace import (
    ApprovalGrant,
    ApprovalResponse,
    ApprovalStore,
    ExecutionProfile,
    NativeSandboxUnavailableError,
    PermissionDecision,
    PermissionRule,
    PolicyContext,
    ProcessManager,
    SafeToolBackend,
    ToolPolicyEngine,
    ToolResult,
    Workspace,
    WorkspaceConflictError,
    WorkspaceEditError,
    WorkspaceLeaseError,
    WorkspaceSecurityError,
    WorkspaceUndoConflictError,
    execute_local,
    execute_typed_tool,
    scrub_env,
)

__all__ = [
    "ApprovalGrant",
    "ApprovalResponse",
    "ApprovalStore",
    "ExecutionProfile",
    "IdentityMismatch",
    "NativeSandboxUnavailableError",
    "PermissionDecision",
    "PermissionRule",
    "PolicyContext",
    "ProcessManager",
    "SafeToolBackend",
    "SandboxDependencyError",
    "SandboxIdentity",
    "SandboxUnavailableError",
    "ToolPolicyEngine",
    "ToolResult",
    "WarmSandboxError",
    "WarmTaskSandbox",
    "Workspace",
    "WorkspaceConflictError",
    "WorkspaceEditError",
    "WorkspaceLeaseError",
    "WorkspaceSecurityError",
    "WorkspaceUndoConflictError",
    "ensure_image",
    "execute_local",
    "execute_sandboxed",
    "execute_typed_tool",
    "scrub_env",
    "warm_sandbox_available",
]
