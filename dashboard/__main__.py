"""`python -m dashboard` entry — delegates to dashboard.server.main."""
from dashboard.server import main

if __name__ == "__main__":
    raise SystemExit(main())
