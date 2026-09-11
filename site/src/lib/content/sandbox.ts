/** Docker sandbox properties. INTERFACES.md:81-88; CHANGELOG.md:24-28. */
export type Flag = { label: string; detail: string };

export const SANDBOX_FLAGS: Flag[] = [
  { label: "read-only rootfs",   detail: "the image layer is immutable" },
  { label: "--network none",     detail: "no egress unless explicitly allowed" },
  { label: "--cap-drop ALL",     detail: "every Linux capability dropped" },
  { label: "mem-limit",          detail: "OOM-killed rather than starving the host" },
  { label: "pids-limit",         detail: "fork bombs collapse at the cap" },
  { label: "fresh container",    detail: "one --rm container per command" },
];

export const SANDBOX_SOURCE = "INTERFACES.md:81-88; CHANGELOG.md:24-28";

/** Adversarial result. CHANGELOG.md:69-71; INTERFACES.md:508-527. */
export const SANDBOX_ADVERSARIAL = {
  sequential: "24/24 sequential attacks held",
  concurrent: "78 concurrent hostile runs at widths 8/10/16 — 0 findings",
  detail:
    "Escape attempts against host mounts, the PID namespace, the Docker socket and cross-container networking all failed. A fork bomb collapsed at the pids limit, a 2 GB memory bomb was OOM-killed, and tmpfs hit ENOSPC at exactly the 256 MB cap.",
  source: "INTERFACES.md:508-527; CHANGELOG.md:69-71",
} as const;
