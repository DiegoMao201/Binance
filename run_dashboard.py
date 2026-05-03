from __future__ import annotations

import subprocess
import sys

from src.utils.config import load_settings


def main() -> None:
    settings = load_settings()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "src/ui/dashboard.py",
            "--server.port",
            str(settings.streamlit_port),
            "--server.headless",
            "true",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()