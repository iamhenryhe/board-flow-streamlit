from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


APP_NAME = "RealtimeBoardViewer"
ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def run(args: list[str]) -> None:
    print(" ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run([sys.executable, "-m", "pip", "install", "-r", "requirements_viewer.txt"])
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            APP_NAME,
            "viewer_app.py",
        ]
    )

    config_dst = DIST / "viewer_config.json"
    shutil.copy2(ROOT / "viewer_config_windows.json", config_dst)

    exe_path = DIST / f"{APP_NAME}.exe"
    zip_path = DIST / "RealtimeBoardViewer_windows.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(exe_path, exe_path.name)
        archive.write(config_dst, config_dst.name)
    print(f"DONE: {zip_path}", flush=True)


if __name__ == "__main__":
    main()
