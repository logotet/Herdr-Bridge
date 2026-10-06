"""Start the bridge hidden at logon (Windows Task Scheduler)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .herdr_client import no_window_flags

TASK_NAME = "herdr-bridge"


def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _user() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def task_xml(command: str, arguments: str, workdir: str) -> str:
    user = escape(_user())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>herdr-bridge: WebSocket bridge between herdr and the Herdr App</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT15S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(workdir)}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          creationflags=no_window_flags())


def install(start_now: bool = True) -> str:
    if sys.platform != "win32":
        raise RuntimeError("install-task is Windows-only; use a systemd user service or launchd instead")
    xml = task_xml(_pythonw(), "-m herdr_bridge serve", str(Path.home()))
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(xml)
        xml_path = f.name
    try:
        res = _schtasks("/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F")
    finally:
        os.unlink(xml_path)
    if res.returncode != 0:
        raise RuntimeError(f"schtasks failed: {(res.stderr or res.stdout).strip()}")
    if start_now:
        _schtasks("/Run", "/TN", TASK_NAME)
    return f"Scheduled task '{TASK_NAME}' installed (runs hidden at logon: {_pythonw()} -m herdr_bridge serve)"


def uninstall() -> str:
    _schtasks("/End", "/TN", TASK_NAME)
    res = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if res.returncode != 0:
        raise RuntimeError(f"schtasks failed: {(res.stderr or res.stdout).strip()}")
    return f"Scheduled task '{TASK_NAME}' removed"


def status() -> str:
    if sys.platform != "win32":
        return "not supported on this platform"
    res = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    return res.stdout.strip() if res.returncode == 0 else "not installed"
