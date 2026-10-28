"""Run the deployment script against isolated command shims, without root access."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def installation(tmp_path, *, active=False, old_active=False, fail_on=""):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash is needed to exercise setup.sh")
    release, target = tmp_path / "release", tmp_path / "installed"
    release.mkdir()
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / "data").mkdir()
    (release / ".env").write_text("synthetic release config\n", encoding="utf-8")
    (target / ".env").write_text("synthetic existing config\n", encoding="utf-8")
    (target / "data" / "tasks.sqlite3").write_bytes(b"synthetic task data")
    marker, trace = tmp_path / "active", tmp_path / "trace"
    if active:
        marker.touch()
    shims = tmp_path / "shims.sh"
    shims.write_text(r'''
record() { printf '%s\n' "$*" >> "$TRACE_FILE"; }
systemctl() {
  record "systemctl $*"
  case "$*" in
    'is-active --quiet qqbot') [[ "$OLD_ACTIVE" == 1 ]];;
    'is-active --quiet qq-math-bot') [[ -f "$ACTIVE_FILE" ]];;
    'stop qq-math-bot') rm -f -- "$ACTIVE_FILE";;
    'start qq-math-bot'|'restart qq-math-bot')
      [[ "$FAIL_ON" != "${1}" ]] || return 1
      : > "$ACTIVE_FILE";;
    *) return 0;;
  esac
}
apt-get() { record "apt-get $*"; }
python3() { record "python3 $*"; }
id() { return 0; }
useradd() { record "useradd $*"; }
install() {
  record "install $*"
  if [[ "$1" != -d ]]; then
    [[ ! -f "$ACTIVE_FILE" ]] || return 93
  fi
}
cp() { record "cp $*"; [[ ! -f "$ACTIVE_FILE" ]]; }
chown() { record "chown $*"; }
chmod() { record "chmod $*"; }
''', encoding="utf-8", newline="\n")
    for name in ("python", "pip"):
        executable = target / ".venv" / "bin" / name
        executable.write_text(
            "#!/usr/bin/env bash\n"
            f'record "venv-{name} $*"\n'
            '[[ ! -f "$ACTIVE_FILE" ]] || exit 93\n'
            f'[[ "$FAIL_ON" != {name} ]] || exit 17\n',
            encoding="utf-8", newline="\n")
        executable.chmod(0o755)
    unit = tmp_path / "qq-math-bot.service"
    source = (Path(__file__).resolve().parents[1] / "setup.sh").read_text(encoding="utf-8")
    source = source.replace("if [[ ${EUID} -ne 0 ]]; then", "if false; then", 1)
    source = source.replace("APP_DIR=/opt/qq-math-bot", f'APP_DIR="{target.as_posix()}"', 1)
    source = source.replace("/etc/systemd/system/qq-math-bot.service", unit.as_posix(), 1)
    script = release / "setup.sh"
    script.write_text(source, encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update(BASH_ENV=shims.as_posix(), TRACE_FILE=trace.as_posix(),
                       ACTIVE_FILE=marker.as_posix(), OLD_ACTIVE=str(int(old_active)),
                       FAIL_ON=fail_on)
    result = subprocess.run([bash, script.as_posix()], cwd=release, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    commands = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
    assert (target / ".env").read_text(encoding="utf-8") == "synthetic existing config\n"
    assert (target / "data" / "tasks.sqlite3").read_bytes() == b"synthetic task data"
    return result, commands, marker, unit


def position(commands, prefix):
    return next(i for i, command in enumerate(commands) if command.startswith(prefix))


@pytest.mark.parametrize("active", [False, True])
def test_setup_stops_before_application_writes_and_starts_updated_service(tmp_path, active):
    result, commands, marker, unit = installation(tmp_path, active=active)
    assert result.returncode == 0, result.stdout + result.stderr
    install = position(commands, "install -m 644")
    pip = position(commands, "venv-pip ")
    restart = position(commands, "systemctl restart qq-math-bot")
    assert install < pip < restart
    if active:
        assert position(commands, "systemctl stop qq-math-bot") < install
    else:
        assert "systemctl stop qq-math-bot" not in commands
    assert marker.exists()
    assert "ExecStart=/usr/bin/env DATA_DIR=/opt/qq-math-bot/data " in unit.read_text(encoding="utf-8")


def test_setup_rejects_old_service_before_installing_anything(tmp_path):
    result, commands, marker, unit = installation(tmp_path, old_active=True)
    assert result.returncode != 0
    assert commands == ["systemctl is-active --quiet qqbot"]
    assert not marker.exists() and not unit.exists()


@pytest.mark.parametrize("failure", ["pip", "python", "restart"])
def test_setup_failure_requests_recovery_of_previously_running_service(tmp_path, failure):
    result, commands, marker, _ = installation(tmp_path, active=True, fail_on=failure)
    assert result.returncode == (17 if failure != "restart" else 1)
    assert position(commands, "systemctl stop qq-math-bot") < position(commands, "install -m 644")
    assert commands[-1] == "systemctl start qq-math-bot"
    assert marker.exists()


def test_failed_first_install_leaves_service_stopped(tmp_path):
    result, commands, marker, _ = installation(tmp_path, fail_on="pip")
    assert result.returncode == 17
    assert "systemctl start qq-math-bot" not in commands
    assert "systemctl restart qq-math-bot" not in commands
    assert not marker.exists()
