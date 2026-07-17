"""
CLI entry point for `s1grits-gui`.

Registered in pyproject.toml as:
    [project.scripts]
    s1grits-gui = "s1grits.gui_entry:main"

Usage:
    s1grits-gui                        # default: 127.0.0.1:8501
    s1grits-gui --port 8502
    s1grits-gui --host 0.0.0.0 --port 8080
    s1grits-gui --no-browser
"""

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    """Launch the S1-GRiTS Streamlit GUI."""
    parser = argparse.ArgumentParser(
        prog="s1grits-gui",
        description="Launch the S1-GRiTS graphical user interface.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        metavar="HOST",
        help="Server address to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        default="8501",
        metavar="PORT",
        help="Server port to listen on (default: 8501)",
    )
    parser.add_argument(
        "--no-browser",
        dest="headless",
        action="store_true",
        default=False,
        help="Do not open a browser tab automatically",
    )
    args = parser.parse_args()

    # Resolve gui/app.py via the installed gui package location.
    # gui/ is bundled as a top-level package alongside s1grits in site-packages.
    try:
        import gui as _gui_pkg
        gui_app = Path(_gui_pkg.__file__).resolve().parent / "app.py"
    except ImportError:
        gui_app = None

    if gui_app is None or not gui_app.exists():
        print("ERROR: GUI application not found. "
              "Install with GUI extras: pip install s1grits[gui]", file=sys.stderr)
        sys.exit(1)

    cmd = [
        sys.executable, "-m", "streamlit", "run", str(gui_app),
        f"--server.address={args.host}",
        f"--server.port={args.port}",
        f"--server.headless={'true' if args.headless else 'false'}",
        "--browser.gatherUsageStats=false",
        "--theme.base=light",
    ]

    print(
        "WARNING: the Streamlit GUI is DEPRECATED and will be removed in v3.0.0. "
        " Use the web interface instead: "
        "`s1grits serve --root <workspace>` (see docs/webapp.md).",
        file=sys.stderr,
    )
    print(f"INFO: Starting S1-GRiTS GUI  →  http://{args.host}:{args.port}")
    print("INFO: Press Ctrl+C to stop the server.")

    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\nINFO: S1-GRiTS GUI server stopped.")


if __name__ == "__main__":
    main()
