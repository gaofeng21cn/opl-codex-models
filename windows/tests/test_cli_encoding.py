"""CLI diagnostics must survive Windows ANSI redirected streams."""
import os
from pathlib import Path
import subprocess
import sys


def test_cli_outputs_utf8_when_parent_requests_ansi(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "codex_model_manager", "--config", str(tmp_path / "不存在.json"), "list"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict"}, capture_output=True,
    )
    assert result.returncode == 1
    message = result.stderr.decode("utf-8")
    assert "尚未找到配置" in message
    assert "不存在.json" in message
    assert "charmap" not in message


def test_mock_catalog_is_utf8_with_ansi_parent(tmp_path):
    catalog = tmp_path / "目录.json"
    catalog.write_text('{"models": [{"slug": "example", "display_name": "中文模型"}]}', encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "demo/mock/codex.py"
    result = subprocess.run([sys.executable, str(script), "debug", "models", "--bundled"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict", "MOCK_BUNDLED_JSON": str(catalog)}, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "中文模型" in result.stdout.decode("utf-8")
