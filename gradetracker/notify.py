"""Desktop notifications (M8), with no new dependency.

On Windows this shells out to PowerShell and the Windows.UI.Notifications API.
Title and body travel in environment variables rather than being interpolated
into the script, so an assignment called "Quiz 1'; rm -rf" cannot become code.

Every failure is swallowed: a missing toast is a nuisance, but a sync that dies
because a notification failed would lose grades.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable

from gradetracker import config

IS_WINDOWS = sys.platform == "win32"

# A registered AUMID is required to raise a toast; PowerShell's own will do.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# Reads the text from the environment, so nothing is interpolated into the script.
_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName("text")
$texts.Item(0).AppendChild($template.CreateTextNode($env:GT_TOAST_TITLE)) | Out-Null
$texts.Item(1).AppendChild($template.CreateTextNode($env:GT_TOAST_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:GT_TOAST_APPID).Show($toast)
"""

Backend = Callable[[str, str], None]


def windows_backend(title: str, body: str) -> None:
    """Raise a Windows toast. Never raises on failure."""
    environment = dict(os.environ)
    environment["GT_TOAST_TITLE"] = title
    environment["GT_TOAST_BODY"] = body
    environment["GT_TOAST_APPID"] = _APP_ID

    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _TOAST_SCRIPT,
            ],
            env=environment,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        # A toast is never worth failing a sync over.
        pass


def stderr_backend(title: str, body: str) -> None:
    """Fallback for anything that is not Windows."""
    print(f"[notify] {title}: {body}", file=sys.stderr)


def default_backend() -> Backend:
    return windows_backend if IS_WINDOWS else stderr_backend


_backend: Backend | None = None


def set_backend(backend: Backend | None) -> None:
    """Override the backend, for tests or for a different desktop."""
    global _backend
    _backend = backend


def enabled() -> bool:
    """GT_NOTIFY=0 turns every notification off."""
    return str(config.GT_NOTIFY).strip().lower() not in ("0", "false", "no", "off")


def notify(title: str, body: str, backend: Backend | None = None) -> bool:
    """Show one notification. Returns whether anything was sent."""
    if not enabled():
        return False

    chosen = backend or _backend or default_backend()
    try:
        chosen(title, body)
    except Exception:
        return False
    return True
