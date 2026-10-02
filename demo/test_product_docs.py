from pathlib import Path

from cli.commands import command_specs

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def test_product_guide_has_required_pages():
    """Keep the documented daily-use surface discoverable."""
    required = {
        "README.md",
        "quickstart.md",
        "providers.md",
        "commands.md",
        "workflows.md",
        "permissions-and-sandbox.md",
        "extensions.md",
        "sessions-and-recovery.md",
        "headless-and-sdk.md",
        "troubleshooting.md",
        "architecture-and-events.md",
        "feature-matrix.md",
        "dogfood-report.md",
    }
    assert required <= {path.name for path in DOCS.glob("*.md")}


def test_command_reference_covers_current_registry():
    """Require every built-in slash command in the current reference."""
    text = (DOCS / "commands.md").read_text(encoding="utf-8")
    missing = [spec.name for spec in command_specs() if spec.name not in text]
    assert not missing


def test_demo_graph_query_is_bounded_to_fixture():
    """Prevent the demo from indexing the entire checkout again."""
    source = (ROOT / "demo" / "run_demo.py").read_text(encoding="utf-8")
    assert "CodeGraph(str(REPO))" in source
    assert "CodeGraph(str(ROOT))" not in source


def test_demo_entrypoints_are_reproducible():
    """Keep both documented demo scripts directly executable."""
    for name in ("run_demo.py", "agent_demo.py"):
        source = (ROOT / "demo" / name).read_text(encoding="utf-8")
        assert "def main()" in source
        assert "if __name__ ==" in source
