"""Subprocess wrapper with progress tracking and structured output.

Provides run_command() as a unified interface for calling external tools
like qemu-img, virt-customize, guestfish, etc.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def check_tool_available(tool: str) -> bool:
    """Check if an external tool is available in PATH."""
    return shutil.which(tool) is not None


def run_command(
    cmd: list[str],
    env: dict[str, str] | None = None,
    capture_output: bool = True,
    check: bool = True,
    timeout: int | None = None,
    progress_pattern: str | None = None,
    progress_callback: Callable[[float], None] | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run an external command with optional progress tracking.

    Args:
        cmd: Command and arguments
        env: Environment variables (merged with os.environ)
        capture_output: Capture stdout/stderr
        check: Raise on non-zero exit
        timeout: Timeout in seconds
        progress_pattern: Regex to extract progress percentage from stderr
        progress_callback: Called with progress percentage (0-100)

    Returns:
        CompletedProcess result

    Raises:
        RuntimeError: If command fails and check=True
    """
    # Merge environment
    run_env = dict(os.environ)
    if env:
        run_env.update(env)

    cmd_str = " ".join(str(c) for c in cmd[:6])
    logger.debug(f"Running: {cmd_str}{'...' if len(cmd) > 6 else ''}")

    try:
        if progress_pattern and progress_callback:
            # Stream stderr for progress updates
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=run_env,
                **kwargs,
            )
            stderr_lines = []
            pattern = re.compile(progress_pattern)

            for line in iter(proc.stderr.readline, b""):
                line_str = line.decode("utf-8", errors="replace").strip()
                stderr_lines.append(line_str)
                match = pattern.search(line_str)
                if match:
                    try:
                        pct = float(match.group(1))
                        progress_callback(pct)
                    except (ValueError, IndexError):
                        pass

            proc.wait(timeout=timeout)
            stdout = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            stderr = "\n".join(stderr_lines)

            result = subprocess.CompletedProcess(
                args=cmd,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        else:
            result = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                env=run_env,
                timeout=timeout,
                **kwargs,
            )

        if check and result.returncode != 0:
            err_msg = ""
            if result.stderr:
                err_msg = result.stderr.strip()[-500:]
            raise RuntimeError(
                f"Command failed (exit {result.returncode}): {cmd_str}\n{err_msg}"
            )

        return result

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {cmd_str}")
    except FileNotFoundError:
        raise RuntimeError(
            f"Command not found: {cmd[0]}. "
            f"Install the required package."
        )
