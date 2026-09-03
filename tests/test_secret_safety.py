from __future__ import annotations

from pathlib import Path
import subprocess
import sys


CHECKER = Path(__file__).resolve().parents[1] / "tools" / "check_secret_safety.py"


def _run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_secret_safety_allows_short_fixture_placeholders(tmp_path):
    (tmp_path / "module.py").write_text('api_key = "fixture-key"\n', encoding="utf-8")

    result = _run_checker(tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_secret_safety_rejects_credential_shaped_literals_without_echoing_them(tmp_path):
    literal = "sk" + "-" + ("A" * 32)
    (tmp_path / "module.py").write_text(f'API_KEY = "{literal}"\n', encoding="utf-8")

    result = _run_checker(tmp_path)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "credential-shaped literal" in combined
    assert literal not in combined


def test_secret_safety_rejects_forbidden_auth_files_without_echoing_contents(tmp_path):
    literal = "sk" + "-" + ("B" * 32)
    (tmp_path / "auth.json").write_text(f'{{"api_key": "{literal}"}}\n', encoding="utf-8")

    result = _run_checker(tmp_path)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "forbidden file" in combined
    assert literal not in combined
