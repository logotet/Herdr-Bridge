"""Git diff for a pane's working directory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .herdr_client import no_window_flags

MAX_DIFF_BYTES = 2 * 1024 * 1024


class GitError(Exception):
    pass


async def _git(cwd: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", cwd, "-c", "core.quotepath=off", "--no-pager", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=no_window_flags(),
    )
    out, err = await asyncio.wait_for(proc.communicate(), 30)
    if proc.returncode != 0:
        raise GitError(err.decode(errors="replace").strip() or f"git {args[0]} failed")
    return out.decode("utf-8", errors="replace")


async def diff(cwd: str, staged: bool = False, path: str | None = None) -> dict[str, Any]:
    if not cwd or not Path(cwd).is_dir():
        raise GitError(f"not a directory: {cwd!r}")
    root = (await _git(cwd, "rev-parse", "--show-toplevel")).strip()
    branch = (await _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    extra = ["--staged"] if staged else []
    paths = ["--", path] if path else []
    stat = await _git(cwd, "diff", *extra, "--stat", *paths)
    text = await _git(cwd, "diff", *extra, "--no-color", *paths)
    untracked = [] if staged else [
        line for line in (await _git(cwd, "ls-files", "--others", "--exclude-standard")).splitlines()
        if line
    ]
    raw = text.encode("utf-8")
    truncated = len(raw) > MAX_DIFF_BYTES
    if truncated:
        text = raw[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    return {"cwd": cwd, "root": root, "branch": branch, "staged": staged, "stat": stat,
            "diff": text, "truncated": truncated, "untracked": untracked[:500]}
