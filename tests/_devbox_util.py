"""Test doubles for the devbox adapter (not a test module itself).

:class:`StubProbe` is a fully in-memory :class:`~cadence.adapters.devbox.DevboxProbe`:
every collector runs against fixture data with no live syscall. Real temp git repos are
built directly in the git-collector tests (they exercise the live ``git`` runner), so
this stub covers processes/tmux/history/load/disk/containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cadence.adapters.devbox import (
    ContainerInfo,
    DiskUsage,
    ProcessInfo,
    RepoDir,
    TmuxSession,
)


@dataclass
class StubProbe:
    """In-memory probe carrying fixture system state."""

    now_value: float
    uid: int = 1000

    # git
    repos: list[RepoDir] = field(default_factory=list)
    cap_hit: bool = False
    git_outputs: dict[Path, str] = field(default_factory=dict)
    mtimes: dict[Path, float] = field(default_factory=dict)
    git_calls: list[tuple[Path, list[str]]] = field(default_factory=list)

    # jobs
    processes: list[ProcessInfo] = field(default_factory=list)

    # session / activity
    tmux: list[TmuxSession] = field(default_factory=list)
    history: list[str] = field(default_factory=list)

    # health
    load: tuple[float, float, float] = (0.5, 0.4, 0.3)
    disks: dict[str, DiskUsage] = field(default_factory=dict)
    containers: list[ContainerInfo] = field(default_factory=list)

    def now(self) -> float:
        return self.now_value

    def current_uid(self) -> int:
        return self.uid

    def find_repos(self, roots, max_depth, max_repos):  # noqa: ANN001, ARG002
        return list(self.repos), self.cap_hit

    def git(self, repo, args):  # noqa: ANN001
        self.git_calls.append((Path(repo), list(args)))
        return self.git_outputs.get(Path(repo))

    def path_mtime(self, path):  # noqa: ANN001
        return self.mtimes.get(Path(path))

    def list_processes(self) -> list[ProcessInfo]:
        return list(self.processes)

    def list_tmux(self) -> list[TmuxSession]:
        return list(self.tmux)

    def read_history_lines(self, path) -> list[str]:  # noqa: ANN001, ARG002
        return list(self.history)

    def load_avg(self) -> tuple[float, float, float]:
        return self.load

    def disk_usage(self, mount: str) -> DiskUsage | None:
        return self.disks.get(mount)

    def list_containers(self) -> list[ContainerInfo]:
        return list(self.containers)
