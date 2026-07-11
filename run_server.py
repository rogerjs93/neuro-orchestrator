"""Self-contained local launcher for the neuro-orchestrator web app.

Used by the preview `launch.json`. Deliberately shell-agnostic: it sets sane LOCAL defaults
(the app otherwise defaults to the Docker paths /data and /outputs) and runs uvicorn in-process,
so it works the same whether invoked from Git Bash, cmd, PowerShell, or a preview harness.

Run directly:  python run_server.py            (defaults to 127.0.0.1:8080, MOCK_MODE=1)
Env overrides:  PORT, HOST, DATA_DIR, OUTPUT_DIR, MOCK_MODE
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Local defaults (only applied if not already set in the environment).
os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("OUTPUT_DIR", str(ROOT / "outputs"))

# Make the `web_server` / `pipeline` packages importable without PYTHONPATH.
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("web_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
