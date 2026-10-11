"""The launcher scripts: install.sh, start.sh, dev.sh and the Dock app's build script."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.mark.parametrize(
    "script", ["install.sh", "start.sh", "dev.sh", "lib/conda.sh", "macos/make_app.sh"]
)
def test_shell_scripts_parse(script: str) -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPTS / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
