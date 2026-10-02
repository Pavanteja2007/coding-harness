"""Render the captured `neo fix` transcript into demo/neo-demo.gif —
the README hero demo. Terminal-styled frames drawn with Pillow: the
run's real phases appear progressively (spinner braille animating), so
viewers watch the loop live, then see the verified result + diff.

Frames are composed from the transcript's semantically distinct lines
(plan/phase transitions, result block, diff, rationale), not its raw
spinner spam (dozens of identical braille refresh lines exist because
the captured stdout was a pipe, not a TTY).

Usage:  python scripts/render_demo_gif.py   (after make_demo_gif.py)
Output: demo/neo-demo.gif
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent.parent
TRANSCRIPT = HERE / "demo" / "neo-demo-transcript.txt"
OUT = HERE / "demo" / "neo-demo.gif"

# ---- terminal look --------------------------------------------------------
COLS, ROWS = 72, 22
CH = 14  # char cell height
CW = 9  # char cell width (monospace-ish)
PAD = 18
W, H = COLS * CW + PAD * 2, ROWS * CH + PAD * 2 + 34  # +34: title bar

BG = (16, 16, 20)
FG = (208, 208, 208)
TITLE_FG = (222, 154, 120)
GREEN = (120, 200, 130)
RED = (220, 110, 110)
GOLD = (217, 142, 95)
MUTED = (130, 130, 140)
CYAN = (120, 180, 210)
BAR = (52, 52, 60)
ACCENT = (201, 80, 76)  # the oxblood accent

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\lucon.ttf",
        r"C:\Windows\Fonts\cour.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


FONT = _font(15)
SMALL = _font(13)


def _fit(text: str) -> str:
    return text if len(text) <= COLS else text[: COLS - 1] + "…"


class Screen:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []  # (text, color)

    def add(self, text: str, color: str = "fg") -> None:
        self.lines.append((text, color))

    def visible(self) -> list[tuple[str, str]]:
        return self.lines[-ROWS:]

    def draw(self, spinner: str | None = None, spin_label: str = "") -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        # title bar
        d.rectangle([0, 0, W, 30], fill=BAR)
        d.ellipse([14, 11, 24, 21], fill=(220, 100, 90))
        d.ellipse([32, 11, 42, 21], fill=(215, 175, 90))
        d.ellipse([50, 11, 60, 21], fill=(110, 190, 120))
        d.text((W - 190, 8), "neo — fix (Docker sandbox)", font=SMALL, fill=TITLE_FG)
        y = 30 + PAD
        for text, color in self.visible():
            col = globals().get(color.upper() if color.isalpha() else "", FG)
            d.text((PAD, y), _fit(text), font=FONT, fill=col)
            y += CH
        if spinner is not None:
            d.text((PAD, y), f"{spinner} {spin_label}", font=FONT, fill=GOLD)
        return img


def _color(line: str) -> str:
    if line.startswith("neo fix"):
        return "accent"
    if "success" in line and "task" in line:
        return "green"
    if line.startswith("+"):
        return "green"
    if line.startswith("-") and not line.startswith("---"):
        return "red"
    if line.startswith("@@"):
        return "cyan"
    if line.startswith(
        (
            "attempts:",
            "cost:",
            "model calls:",
            "elapsed:",
            "target test:",
            "regression:",
            "flaky:",
        )
    ):
        return "muted"
    if "PASS" in line:
        return "green"
    if line.startswith(("run ", "issue:", "task:")):
        return "muted"
    if "Rationale" in line:
        return "gold"
    return "fg"


TMP_ROOT = re.compile(
    r"[A-Z]:\\Users\\[^\s\\]+\\AppData\\Local\\Temp\\opencode\\neo-demo-gif"
)


def _script_rows(transcript: str) -> list[str]:
    """The semantic rows worth showing, in order."""
    rows: list[str] = []
    for raw in transcript.splitlines():
        line = raw.rstrip()
        # display-only path cleanup: the recording scratch dir reads as a
        # normal relative path in the screencast
        line = TMP_ROOT.sub(".", line)
        line = line.replace(".\\repo", "./smoke_repo")
        line = line.replace(".\\logs\\", "./logs/")
        line = line.replace("\\", "/")
        line = line.replace(". /logs", "./logs")
        # the wrap artifact: the captured console wrapped the trace path
        line = re.sub(r"^onl$", "", line)
        line = re.sub(r"trace\.js$", "trace.jsonl", line)
        # spinner refresh lines -> collapse to one phase row
        m = re.match(r"^(?:\s*)[\u2800-\u28ff] (.+)$", line)
        if m:
            label = re.sub(r"\s+·\s+\d+ events.*$", "", m.group(1))
            label = re.sub(r"\$0\.\d+$", "", label).strip()
            if not rows or rows[-1] != "PHASE:" + label:
                rows.append("PHASE:" + label)
            continue
        if line.startswith(("┌", "└", "· ")):
            continue
        rows.append(line)
    return rows


def main() -> int:
    transcript = TRANSCRIPT.read_text(encoding="utf-8")
    rows = _script_rows(transcript)

    # Fix glyph artifacts from the cp1252 capture: -> and the trace-path
    # line get display-normalized (the GIF shows a repo-relative layout)
    rows = [r.replace("\u2192", "->").replace("\ufffd", "-") for r in rows]
    rows = [
        re.sub(r"^neo fix -> \.\\\\repo$", "neo fix -> ./smoke_repo", r) for r in rows
    ]
    rows = [re.sub(r"^\.\]repo$", "./smoke_repo", r) for r in rows]
    rows = [r.replace(".\\logs\\demo-hero", "./logs/demo-hero") for r in rows]
    rows = [re.sub(r"\.\\repo", "./smoke_repo", r) for r in rows]

    sc = Screen()
    sc.add(
        '$ neo fix --repo ./smoke_repo --issue "mean() returns the sum, not the mean"',
        "gold",
    )

    frames: list[Image.Image] = []
    phases = [r for r in rows if r.startswith("PHASE:")]
    body = [r for r in rows if not r.startswith("PHASE:")]

    # 1) opening lines held briefly
    frames += [sc.draw()] * 18

    # 2) run header lines
    for line in body[:3]:
        sc.add(line, _color(line))
        frames += [sc.draw()] * 6

    # 3) the loop: each phase animates the spinner over accumulated lines
    sc.add("─" * 66, "muted")
    for i, ph in enumerate(phases):
        label = ph[len("PHASE:") :]
        for s in range(10):
            frames.append(sc.draw(SPIN[(i * 3 + s) % len(SPIN)], label))
        sc.add("  " + label, "muted")

    sc.add("─" * 66, "muted")
    frames += [sc.draw()] * 10

    # 4) result block + diff + rationale, revealed line by line
    for line in body[3:]:
        sc.add(line, _color(line))
        frames += [sc.draw()] * 5
    frames += [sc.draw()] * 40  # hold the final frame

    frames[0].save(
        OUT,
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        optimize=True,
    )
    print(f"wrote {OUT} ({len(frames)} frames, {OUT.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
