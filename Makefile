# Vex — root Makefile (one-command dev workflows; see CONTRIBUTING.md)
#
# Windows note: use Git Bash (`bash`), or run the underlying commands
# directly from PowerShell:
#   dev:    bash scripts/dev-setup.sh
#   fmt:    ruff format .
#   lint:   python scripts/lint_ratchet.py
#   test:   python -m pytest

.PHONY: dev fmt fmt-check lint lint-baseline test smoke clean

# One-command dev environment: deps + pre-commit hook + Docker check.
dev:
	bash scripts/dev-setup.sh

# Auto-format the whole repo (same config the pre-commit hook uses).
fmt:
	ruff format .

# CI-style check: nothing left unformatted.
fmt-check:
	ruff format --check .

# Lint with the ratchet: full rule set on new/clean files, no growth
# on baselined files (details: scripts/lint-baseline.txt).
lint:
	python scripts/lint_ratchet.py

# Re-count existing lint debt after cleaning files (deliberate action,
# not part of lint).
lint-baseline:
	python scripts/lint_ratchet.py --update-baseline

# Full test suite (Docker-gated e2e tests self-skip without a daemon).
test:
	python -m pytest

# Quick CLI plumbing check — offline, no API key, no Docker needed.
smoke:
	python -m cli status --task-id smoke

clean:
	find . -type d -name __pycache__ -not -path "./.git/*" -exec rm -rf {} + 2>/dev/null; true
