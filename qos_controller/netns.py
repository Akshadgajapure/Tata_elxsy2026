"""The ONLY place the backend runs shell commands.

Rules: no shell=True, argv lists only, namespace and binary allowlists,
always a timeout for one-shot commands.  The web layer never passes raw
command text here; it passes structured, validated profile names.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from .config import ALLOWED_NAMESPACES

log = logging.getLogger(__name__)

ALLOWED_BINARIES = frozenset({"cat", "tc", "ping", "iperf3"})


class CommandError(RuntimeError):
    """A bounded command failed, timed out, or was refused."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def build_argv(namespace: str, argv: list[str]) -> list[str]:
    if namespace not in ALLOWED_NAMESPACES:
        raise CommandError(f"namespace not allowed: {namespace!r}")
    if not argv or argv[0] not in ALLOWED_BINARIES:
        raise CommandError(f"binary not allowed: {argv[:1]!r}")
    if any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise CommandError("invalid argument")
    return ["ip", "netns", "exec", namespace, *argv]


def run_in_ns(namespace: str, argv: list[str], timeout: float = 2.0,
              input: str | None = None) -> CommandResult:
    full = build_argv(namespace, argv)
    try:
        proc = subprocess.run(
            full, capture_output=True, text=True, timeout=timeout, check=False, input=input
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"timeout after {timeout}s: {' '.join(full)}") from exc
    except FileNotFoundError as exc:
        raise CommandError("'ip' not found (Linux + iproute2 required)") from exc
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def popen_in_ns(namespace: str, argv: list[str], **kwargs) -> subprocess.Popen:
    """Long-running process in its own session so the whole group can be killed."""
    full = build_argv(namespace, argv)
    try:
        if os.name == "nt":
            return subprocess.Popen(full, text=True, **kwargs)
        return subprocess.Popen(full, start_new_session=True, text=True, **kwargs)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise CommandError(f"'ip' or binary not found / unsupported on platform: {exc}") from exc
