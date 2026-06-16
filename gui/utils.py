"""
Shared utility functions for the S1-GRiTS GUI.
"""
import os
import sys
from pathlib import Path


def open_in_explorer(path: str) -> None:
    """
    Open a directory in the OS file explorer.
    Only opens directories — never opens files directly via os.startfile,
    which could execute .bat/.exe files on Windows.
    """
    import subprocess
    target = Path(path).resolve()
    # Only open directories; refuse to open files to prevent execution
    if not target.is_dir():
        target = target.parent
    if not target.is_dir():
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(target))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception:
        pass
