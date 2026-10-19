#!/usr/bin/env python3
"""Cross-platform launcher for Moomoo OpenD.

On macOS/Linux this delegates to start_opend.sh.
On Windows this delegates to start_opend.ps1 through PowerShell.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    system = platform.system()

    if system == "Windows":
        script = ROOT / "start_opend.ps1"
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            print("ERROR: PowerShell is not installed or not on PATH.", file=sys.stderr)
            return 1
        if not script.exists():
            print(f"ERROR: Missing launcher script: {script}", file=sys.stderr)
            return 1
        command = [shell, "-ExecutionPolicy", "Bypass", "-File", str(script), *sys.argv[1:]]
        completed = subprocess.run(command, cwd=str(ROOT), check=False)
        return completed.returncode

    script = ROOT / "start_opend.sh"
    if not script.exists():
        print(f"ERROR: Missing launcher script: {script}", file=sys.stderr)
        return 1

    shell = shutil.which("bash")
    if shell is None:
        print("ERROR: bash is not available on PATH.", file=sys.stderr)
        return 1

    command = [shell, str(script), *sys.argv[1:]]
    completed = subprocess.run(command, cwd=str(ROOT), check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
