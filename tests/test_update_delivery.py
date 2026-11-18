from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("script", ["update_delivery.sh", "update_pdf_input.sh"])
def test_legacy_updater_is_disabled_and_cannot_replace_installed_code(script):
    source = (ROOT / script).read_text(encoding="utf-8")
    assert "已停用" in source
    assert "sudo bash setup.sh" in source
    assert "exit 1" in source
    assert "UPDATE_PY" not in source
    assert "base64.b64decode" not in source
    assert "systemctl stop" not in source
