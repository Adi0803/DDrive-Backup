"""Creates/removes the Windows Task Scheduler entry that runs the hidden check."""

from __future__ import annotations

import getpass
import os
import subprocess
import tempfile
from datetime import datetime
from xml.sax.saxutils import escape

from .winutils import CREATE_NO_WINDOW, IS_WINDOWS, windowless_python

TASK_NAME = "D-Drive OneDrive Backup"

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Right after you sign in to Windows, whenever the PC connects to a network, and every {interval} minutes: if connected to the office Wi-Fi and something changed, back up {source} to OneDrive.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
      <Delay>PT1M</Delay>
    </LogonTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>{network_query}</Subscription>
      <Delay>PT30S</Delay>
    </EventTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{interval}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# "Network connected" (Wi-Fi or cable) is event 10000 in the NetworkProfile log.
NETWORK_CONNECTED_QUERY = (
    '<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational">'
    '<Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[(EventID=10000)]]</Select>'
    '</Query></QueryList>')


def task_xml(script: str, workdir: str, interval: int, source: str) -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()
    return _TASK_XML.format(
        interval=int(interval), source=escape(source), user=escape(user),
        network_query=escape(NETWORK_CONNECTED_QUERY),
        start=datetime.now().replace(microsecond=0).isoformat(),
        command=escape(windowless_python()), arguments=escape(f'"{script}" --scheduled'),
        workdir=escape(workdir))


def install(script: str, workdir: str, interval: int, source: str) -> str:
    if not IS_WINDOWS:
        raise RuntimeError("The schedule can only be installed on Windows.")
    xml = task_xml(script, workdir, interval, source)
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        result = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"],
                                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    finally:
        os.remove(path)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if "denied" in message.lower():
            message += (" - Windows refused to create the task. Double-click \"Install autostart.bat\" normally, "
                        "while signed in as yourself. Don't use \"Run as administrator\" with another account: "
                        "the task would then belong to that account and never run for you. If it still fails, "
                        "your company may block scheduled tasks, or an old task with this name belongs to "
                        f"another account - ask IT to delete the task \"{TASK_NAME}\".")
        raise RuntimeError(message)
    return (result.stdout or "").strip()


def run_now() -> None:
    """Start the background check once right away (best effort)."""
    if IS_WINDOWS:
        subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)


def remove() -> str:
    if not IS_WINDOWS:
        raise RuntimeError("The schedule only exists on Windows.")
    result = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return (result.stdout or "").strip()
