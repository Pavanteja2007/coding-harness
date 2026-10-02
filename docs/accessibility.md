# Terminal accessibility

The measured state of Neo's terminal accessibility, verified against a **real
attached pseudo-terminal** rather than by reading the source.

**Summary:** the visual/motion accessibility contract holds on two of three
rendering profiles. One profile fails a latency gate by 4.763 ms, and one
promised feature — screen-reader announcements — does not exist. Both are
recorded below with reproducers. The verification harness that produced these
numbers is itself untracked, which is its own problem.

---

## 1. What is implemented and verified

| capability | status | evidence |
|---|---|---|
| `NO_COLOR` / `NEO_NO_COLOR=1` → plain output | implemented | `cli/ui.py`, `cli/theme.py`; `tests/test_cli_release.py::TestColorControl` |
| `TERM=dumb` → plain output, TUI entry disabled | implemented | `cli/theme.py`; `tests/test_cli_theme.py` |
| High-contrast profile | implemented | `cli/theme.py`; `tests/test_cli_theme.py` |
| Reduced-motion profile | implemented | `cli/ui.py::motion_enabled`; `cli/tui.py::_spinner_frames` |
| Reduced motion removes decorative animation but keeps state text | implemented | `tests/test_cli_terminal_ux.py` |
| No colour-only state (status carries text) | implemented | `tests/test_cli_terminal_ux.py` |
| Glyph text alternatives (ASCII fallback on legacy encodings) | implemented | `cli/ui.py::_glyph`; `tests/test_cli_theme.py` |
| Focus-visible states; keyboard reachability | implemented | `cli/tui.py` `NeoApp.CSS`; `tests/test_cli_terminal_ux.py` |
| Bounded, selectable transcript (1200 lines, `ALLOW_SELECT`) | implemented | `cli/tui_components.py::EventFeed` |
| Long diffs summarised before detail, hard 242-line cap | implemented | `tests/test_cli_terminal_ux.py` |
| Cancellable pending state with text hint | implemented | `tests/test_cli_terminal_ux.py` |
| Secrets redacted before render | implemented | `tests/test_cli_terminal_ux.py` |
| Resize preserves cursor, input, and active task | implemented | verified on 5 viewports below |
| Unverified completion never reads as success | implemented | verified on a real PTY below |

## 2. What is NOT implemented

| gap | detail |
|---|---|
| **Screen-reader announcements** | No screen-reader detection, no live region, no announcement channel, no config key, no test. See §5.1. |
| **Native Windows real-terminal evidence** | The ConPTY driver exists but cannot run in a session without an interactive window station. See §4.2. |
| **A tracked, reproducible harness** | Every receipt below was produced by scripts under `logs/`, which is gitignored. See §5.2. |
| **Colour-vision-deficiency simulation** | No deuteranopia/protanopia simulation or palette validation exists. High contrast is a different axis. |

---

## 3. Performance and layout gates, measured on a real PTY

The accessibility contract in `NEO_TERMINAL_UX_MASTER_PROMPTS.md` §7 sets these
gates. Values are p95 unless noted.

| gate | limit | `xterm-256color` | `NO_COLOR=1` | `TERM=dumb` |
|---|---|---|---|---|
| Input acknowledgement | < 100 ms | 45.586 ms | 49.954 ms | 0.555 ms |
| Event-to-UI | < 250 ms | 44.965 ms | 46.792 ms | 44.767 ms |
| UI-thread stall | none > 500 ms | **0** | **0** | **0** |
| Modal open | < 250 ms | 182.089 ms | 228.038 ms | **254.763 ms — FAIL** |
| Transcript bounded | yes | 90 lines | — | — |

Layout preservation, all three profiles: `80x24`, `60x24`, `120x36`, `200x50`,
`50x160` — cursor preserved, composer input preserved, active task visible, in
every case.

---

## 4. How this was verified (and why "by inspection" is not enough)

### 4.1 The real-PTY campaign

Run inside WSL under `script -qec`, which **allocates a genuine
pseudo-terminal** and makes the child's stdout and stderr real TTYs. The probe
runs the actual `NeoApp` — no Pilot, no simulated driver.

This matters concretely. On the first attempt the probe was run through a plain
pipe and reported:

```json
{"passed": false, "real_terminal": false, "stdout_isatty": false, ...,
 "checks": { ... 21 of 21 true ... }}
```

21 of 21 *functional* checks passed and the overall verdict was still `false`,
because the probe refuses to report a pass when it is not attached to a
terminal. That is the harness doing the right thing, and it is the reason the
PTY is allocated explicitly rather than assumed.

Reproduce (Linux/macOS/WSL, from the repository root):

```bash
# dependencies resolve from the host's site-packages under WSL
export PYTHONPATH="/mnt/c/Users/pavan/AppData/Local/Programs/Python/Python310/lib/site-packages:$PWD"

for profile in "xterm-256color 0 0" "dumb 1 0" "xterm-256color 0 1"; do
  set -- $profile
  NEO_TERMINAL_06_REPORT="/tmp/t06-$1-$3.json" TERM="$1" \
  NEO_TERMINAL_06_REDUCED_MOTION="$2" NEO_NO_COLOR="$3" \
    script -qec "python3 logs/terminal-ux/terminal06_real_pty_check.py" /dev/null
done
```

(The probe scripts live under `logs/`, which is gitignored. See §5.2 — this is
the defect, and it is why the path above will not work on a clean clone.)

### 4.2 What could not run here: native Windows ConPTY

```bash
python logs/terminal-ux/terminal09_conpty_check.py
```

```json
{"available": false, "passed": false,
 "reason": "CreatePseudoConsole returned FALSE with last error 0; this session
            has no interactive window station, so a native Windows
            pseudoconsole cannot be created here"}
```

Exit 1. The driver distinguishes "API missing" from "API present but unusable
in this session" and reports the latter as **blocked**, not as a pass — which
is why the WSL POSIX PTY is the real-terminal evidence of record and no
Windows-native claim is made here.

---

## 5. The two open gaps

### 5.1 Screen-reader announcements: promised, not built

The terminal-UX accessibility prompt listed thirteen items. One read:

> status announcements for screen readers and dumb terminals

What shipped satisfies the **dumb-terminal** half: a plain-text status tooltip
(`"Status: idle"` in `status.tooltip`, asserted in
`tests/test_cli_terminal_ux.py::test_status_has_plain_text_assistance_and_focus_css`).

The **screen-reader** half has no implementation. Searches across `cli/` and
`tests/` for `screen_reader`, `aria`, `aria_live`, `live region`, `sr_`,
`announce` (as a mechanism rather than a docstring) return nothing that
implements an announcement path. There is no screen-reader mode, no
announcement sink, and no test for one.

`logs/terminal-ux/terminal-06.json` nonetheless records that item as
`"status": "implemented_and_verified"`. The claim was **true of the dumb
terminal half and untrue of the screen-reader half**, and the handoff did not
distinguish them. That is the defect R2-18 was opened for, and it is a defect
of *reporting* as much as of code.

`cli/tui.py` is owned by another round. The implementation request, with the
acceptance criteria, is in `docs/AGENTS.md` § "Cross-terminal requests".

### 5.2 The verification harness is not in version control

```bash
git ls-files | grep -cE 'pty|conpty|terminal-ux'
# 0
```

Every real-PTY driver — `terminal06_real_pty_check.py`, `terminal08_real_pty.py`,
`terminal09_conpty_check.py`, the WSL launcher, and the summary scripts — lives
under `logs/terminal-ux/`, and `.gitignore:3` ignores `logs/`.

Consequences, all real:

- A clean clone contains **no way to reproduce any receipt in this document.**
- CI cannot run the accessibility gate, because the gate is not in the repo.
- The WSL drivers hardcode `/mnt/c/Users/pavan/Desktop/projects/coding-harness`,
  so they would not work on another machine even if they were tracked.
- The ConPTY driver has consequently **never successfully executed**: its only
  recorded run is the same `blocked_by_session` result reproduced above.

The fix is a relocation, not a rewrite: move the drivers to a tracked location
(`scripts/terminal_probe/`), parameterise the repository root, and add a
`pytest -m slow` marker so the suite can select them. Requested in
`docs/AGENTS.md`.

---

## 6. Honest reading of the `TERM=dumb` failure

`TERM=dumb` misses the 250 ms modal gate by 4.763 ms — 1.9%. The paint phase
is 236.238 ms of the 254.763 ms total; the push is 18.525 ms. So the cost is in
rendering the modal, and a `dumb` terminal takes the slow path through it.

Three things should be said rather than one:

1. **It is a real failure of a stated gate**, and it is reported as one.
2. **It contradicts a prior green receipt** in `cli/AGENTS.md`. The likeliest
   explanation is that `dumb` was always marginal and drifted; the alternative —
   that the earlier number was wrong — cannot be excluded from the record alone.
3. **A pass here is fragile.** `NO_COLOR` measures 228.038 ms, leaving 22 ms of
   headroom. On a slower machine both would fail. The 250 ms gate is close to
   the noise floor of this measurement on a loaded host, and any claim that
   this margin is comfortable would be false.
