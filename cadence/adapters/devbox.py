"""Devbox (dev-server) source adapter — signals from the always-on box.

Cadence's other adapters (GitHub, Google Calendar, Gmail) capture work the user *files*.
This one captures work the user's **always-on dev server** is *doing* while the phone
is elsewhere: a build finished or crashed, a branch left dirty for hours, a disk filling
up, an unhealthy container. That is uniquely valuable precisely because it is invisible
to the phone — the box knows things no other source can report.

Signals (four independent, injectable collectors → structured :class:`Event`s)
------------------------------------------------------------------------------
1. :class:`GitWipCollector` → ``devbox.git_status`` — per own-user repo: name/alias,
   branch, ahead/behind, dirty flag, untracked count, and the *age* of the newest
   uncommitted change. Feeds "stale WIP" nudges. Read via ``git`` **porcelain only**
   (no diffs, no file names ever emitted).
2. :class:`JobLifecycleCollector` → ``devbox.job_finished`` — a tracked long-running
   job (own uid, matching a config pattern) that **disappears** between polls becomes a
   "your run finished/crashed while you were away" signal, carrying the command **verb**
   (first token, args stripped), a duration, and success/failure when determinable.
   State is persisted on disk so the transition survives across poll processes.
3. :class:`SessionActivityCollector` → ``devbox.activity`` — tmux session counts
   (attached/detached, last-activity) plus the timestamped zsh history reduced to an
   activity *cadence*: command **count + timestamps only**, never the command text.
   Bash history without timestamps is skipped (nothing to reduce).
4. :class:`HealthCollector` → ``devbox.health`` — load average, disk pressure on key
   mounts, and own Docker containers' name+status → device-care nudges.

Privacy stance (non-negotiable — this is a SHARED box with secrets/other users)
-------------------------------------------------------------------------------
* **Own-user only.** Every collector filters by uid / ``$HOME`` (see
  :func:`owned_by_current_user`). Another user's process, repo, tmux session or
  container is *never* captured. This is the shared-box analog of the office-mic
  participant gate.
* **Structured signals only — never content.** No source code, diffs, file contents,
  DB contents, env vars, secrets, or **full command lines/arguments** (they carry paths
  and tokens). Only the command *verb* (first token), a repo *name* (optionally aliased
  or hashed for sensitive repos), counts, metadata and timestamps leave a collector.
* **Read-only.** No ``git`` writes, no killing/mutating processes, no touching files.
  The live ``git`` runner (:meth:`LiveProbe.git`) refuses any non-read-only subcommand.

Design & testability
---------------------
Collectors are **pure with respect to an injected** :class:`DevboxProbe` — the probe is
the *only* seam to the live OS. Tests drive collectors with a stub probe carrying
fixture process lists, tmux listings, history lines, load/disk/container data (and real
temp git repos for the git collector), so the whole adapter runs with **no live system
dependency**. :class:`DevboxAdapter` wires the enabled collectors into the normal
adapter contract (``fetch`` → ``normalize`` → provenance-tagged Events), and a thin
``python -m cadence.adapters.devbox`` poller (see :func:`main`) is the **runtime** step
that feeds the ingest endpoint — it is never exercised by tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cadence.adapters.base import (
    AcquisitionTier,
    Adapter,
    CredentialVault,
    Event,
    registry,
)
from cadence.obs.logging import get_logger, log_event
from cadence.stores.nas import NASStore

_log = get_logger("adapters.devbox")


# --------------------------------------------------------------------------- #
# Own-user scope guard (the shared-box participant gate)
# --------------------------------------------------------------------------- #


def owned_by_current_user(owner_uid: int | None, current_uid: int) -> bool:
    """True only if ``owner_uid`` is the current user's uid.

    The single guard every collector routes ownership decisions through. A ``None``
    owner (ownership genuinely undeterminable for the resource, e.g. a Docker container
    with no owner label) is treated as **not** the current user's — fail closed, never
    capture something we cannot attribute to ourselves on a shared box.
    """
    return owner_uid is not None and owner_uid == current_uid


# --------------------------------------------------------------------------- #
# Structured, content-free system records the probe returns
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProcessInfo:
    """A process snapshot. ``argv`` is provided so the collector can take the *verb*
    (``argv[0]`` basename) — the rest is **never** stored or emitted."""

    pid: int
    uid: int
    argv: Sequence[str]
    start_time: float  # epoch seconds
    returncode: int | None = None  # set only when a snapshot already reports an exit


@dataclass(frozen=True)
class TmuxSession:
    """A tmux session owned by some uid (count/metadata only — never pane contents)."""

    name: str
    attached: bool
    last_activity: float  # epoch seconds
    uid: int


@dataclass(frozen=True)
class RepoDir:
    """A discovered git repo directory + its owning uid."""

    path: Path
    uid: int


@dataclass(frozen=True)
class DiskUsage:
    """Bytes for one mount point."""

    total: int
    used: int
    free: int


@dataclass(frozen=True)
class ContainerInfo:
    """A Docker container's name + status (never logs, env, or contents)."""

    name: str
    status: str
    uid: int | None = None


# --------------------------------------------------------------------------- #
# The probe: the ONLY seam to the live OS (injected; stubbed in tests)
# --------------------------------------------------------------------------- #


class DevboxProbe(Protocol):
    """The live-system boundary. Collectors call only these methods, so tests swap in a
    stub with fixture data and the collectors run with no real syscalls."""

    def now(self) -> float:
        """Current wall-clock epoch seconds."""

    def current_uid(self) -> int:
        """The uid the adapter runs as (own-user scope reference)."""

    # -- git ---------------------------------------------------------------- #
    def find_repos(
        self, roots: Sequence[Path], max_depth: int, max_repos: int
    ) -> tuple[list[RepoDir], bool]:
        """Discover git repos under ``roots``; return ``(repos, cap_hit)``."""

    def git(self, repo: Path, args: Sequence[str]) -> str | None:
        """Run a **read-only** git porcelain command in ``repo``; stdout or ``None``."""

    def path_mtime(self, path: Path) -> float | None:
        """Modification time (epoch) of ``path``, or ``None`` if it cannot be stat'd."""

    # -- jobs --------------------------------------------------------------- #
    def list_processes(self) -> list[ProcessInfo]:
        """All visible processes (collector filters to own uid + job pattern)."""

    # -- session/activity --------------------------------------------------- #
    def list_tmux(self) -> list[TmuxSession]:
        """tmux sessions (collector filters to own uid)."""

    def read_history_lines(self, path: Path) -> list[str]:
        """Raw history lines. The collector parses **timestamps only** and discards the
        command text — returning raw lines here lets tests prove that discard."""

    # -- health ------------------------------------------------------------- #
    def load_avg(self) -> tuple[float, float, float]:
        """1/5/15-minute load averages."""

    def disk_usage(self, mount: str) -> DiskUsage | None:
        """Disk usage for ``mount``, or ``None`` if it does not exist."""

    def list_containers(self) -> list[ContainerInfo]:
        """Docker containers (collector filters to own uid)."""


# Read-only git subcommands the live runner will execute — anything else is refused so
# the adapter can never mutate a repo.
_READONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "rev-parse", "rev-list", "log", "for-each-ref", "branch", "symbolic-ref"}
)


class LiveProbe:
    """Default :class:`DevboxProbe` backed by real syscalls. Used by the runtime poller;
    tests never use it except the git collector against real *temp* repos."""

    def __init__(self) -> None:
        #: Cache of the daemon's rootless status (see :meth:`_docker_rootless`); the
        #: daemon does not change mode under a running poller, so probe once.
        self._rootless_cache: bool | None = None

    def now(self) -> float:
        return time.time()

    def current_uid(self) -> int:
        return os.getuid()

    def find_repos(
        self, roots: Sequence[Path], max_depth: int, max_repos: int
    ) -> tuple[list[RepoDir], bool]:
        repos: list[RepoDir] = []
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            root_depth = len(root.parts)
            for dirpath, dirnames, _ in os.walk(root):
                current = Path(dirpath)
                depth = len(current.parts) - root_depth
                if (current / ".git").exists():
                    try:
                        uid = current.stat().st_uid
                    except OSError:
                        continue
                    repos.append(RepoDir(path=current, uid=uid))
                    dirnames[:] = []  # do not descend into a repo
                    if len(repos) >= max_repos:
                        return repos, True
                    continue
                if depth >= max_depth:
                    dirnames[:] = []
        return repos, False

    def git(self, repo: Path, args: Sequence[str]) -> str | None:
        args = list(args)
        if not args or args[0] not in _READONLY_GIT_SUBCOMMANDS:
            raise ValueError(f"refusing non-read-only git subcommand: {args[:1]}")
        try:
            proc = subprocess.run(  # noqa: S603 — fixed 'git' binary, read-only subcmds
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def path_mtime(self, path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def list_processes(self) -> list[ProcessInfo]:
        out: list[ProcessInfo] = []
        proc_root = Path("/proc")
        boot = _boot_time()
        clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        for entry in proc_root.iterdir() if proc_root.is_dir() else []:
            if not entry.name.isdigit():
                continue
            try:
                uid = entry.stat().st_uid
                cmdline = (entry / "cmdline").read_bytes()
                stat_txt = (entry / "stat").read_text()
            except OSError:
                continue
            argv = [a for a in cmdline.split(b"\x00") if a]
            if not argv:
                continue
            start_time = _proc_start_epoch(stat_txt, boot, clk_tck)
            out.append(
                ProcessInfo(
                    pid=int(entry.name),
                    uid=uid,
                    argv=[a.decode("utf-8", "replace") for a in argv],
                    start_time=start_time,
                )
            )
        return out

    def list_tmux(self) -> list[TmuxSession]:
        fmt = "#{session_name}\x1f#{session_attached}\x1f#{session_activity}"
        try:
            proc = subprocess.run(  # noqa: S603, S607 — fixed read-only tmux query
                ["tmux", "list-sessions", "-F", fmt],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        uid = self.current_uid()
        sessions: list[TmuxSession] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 3:
                continue
            name, attached, activity = parts
            sessions.append(
                TmuxSession(
                    name=name,
                    attached=attached.strip() not in ("", "0"),
                    last_activity=_safe_float(activity),
                    uid=uid,  # tmux only lists the caller's own server
                )
            )
        return sessions

    def read_history_lines(self, path: Path) -> list[str]:
        try:
            return Path(path).read_text(errors="replace").splitlines()
        except OSError:
            return []

    def load_avg(self) -> tuple[float, float, float]:
        try:
            return os.getloadavg()
        except (OSError, AttributeError):
            return (0.0, 0.0, 0.0)

    def disk_usage(self, mount: str) -> DiskUsage | None:
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            return None
        return DiskUsage(total=usage.total, used=usage.used, free=usage.free)

    def _docker_rootless(self) -> bool:
        """True only when the Docker daemon is **rootless** (per-user).

        Own-user scope is enforceable for containers ONLY under rootless Docker, where
        each user runs their own daemon and ``docker ps`` lists solely that user's
        containers. A shared *rootful* daemon lists EVERY user's containers via
        ``docker ps -a`` with no per-user owner field, so ownership is genuinely
        undeterminable and we must not capture them (fail closed — see
        :meth:`list_containers`). Result is cached for the probe's lifetime.
        """
        if self._rootless_cache is not None:
            return self._rootless_cache
        rootless = False
        try:
            proc = subprocess.run(  # noqa: S603, S607 — fixed read-only docker query
                ["docker", "info", "--format", "{{json .SecurityOptions}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                rootless = "rootless" in proc.stdout.lower()
        except (OSError, subprocess.SubprocessError):
            rootless = False
        self._rootless_cache = rootless
        return rootless

    def list_containers(self) -> list[ContainerInfo]:
        # On a shared rootful daemon, `docker ps` returns other users' containers with no
        # owner field, so ownership cannot be attributed — do not even fetch the listing
        # (their container names must not enter our process), fail closed to no signal.
        if not self._docker_rootless():
            log_event(
                _log, 20, "devbox.container_health_disabled",
                reason="docker daemon is not rootless; per-user ownership cannot be "
                "attributed on a shared box",
            )
            return []
        try:
            proc = subprocess.run(  # noqa: S603, S607 — fixed read-only docker query
                ["docker", "ps", "-a", "--format", "{{.Names}}\x1f{{.Status}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        # Safe: a rootless daemon lists ONLY this user's containers.
        uid = self.current_uid()
        containers: list[ContainerInfo] = []
        for line in proc.stdout.splitlines():
            name, _, status = line.partition("\x1f")
            if name:
                containers.append(ContainerInfo(name=name, status=status, uid=uid))
        return containers


def _boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def _proc_start_epoch(stat_txt: str, boot: float, clk_tck: int) -> float:
    """Field 22 of /proc/<pid>/stat is starttime in clock ticks since boot."""
    close = stat_txt.rfind(")")
    fields = stat_txt[close + 1 :].split() if close != -1 else stat_txt.split()
    try:
        starttime_ticks = float(fields[19])  # field 22 counting the two before comm
    except (IndexError, ValueError):
        return 0.0
    return boot + starttime_ticks / (clk_tck or 100)


def _safe_float(text: str) -> float:
    try:
        return float(text.strip())
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def _default_repo_roots() -> list[Path]:
    return [Path.home() / "projects"]


def _default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "cadence"


@dataclass
class DevboxCollectorConfig:
    """Everything the four collectors need — repo roots/bounds, the job verb pattern,
    sensitive-repo aliasing, history/tmux/mount targets, poll cadence, per-collector
    enable, and the ingest endpoint the runtime poller feeds."""

    account_ref: str = "devbox"

    # Git WIP
    repo_roots: list[Path] = field(default_factory=_default_repo_roots)
    repo_max_depth: int = 3
    repo_max_count: int = 200
    stale_wip_seconds: int = 8 * 3600
    #: Repo names whose *structured name* must not reach the LLM egress. Aliased to
    #: ``sensitive_repo_aliases[name]`` if given, else a stable non-reversible hash.
    sensitive_repos: frozenset[str] = frozenset()
    sensitive_repo_aliases: dict[str, str] = field(default_factory=dict)

    # Job lifecycle
    #: Matched (fullmatch, case-insensitive) against the command **verb** only.
    job_pattern: str = (
        r"(python[0-9.]*|pytest|node|npm|yarn|pnpm|cargo|go|make|gradle|mvn|"
        r"docker|ruff|tsc|jest|vite|webpack|rustc|gcc|clang|ninja|bazel|train|build)"
    )

    # Session / activity
    history_path: Path = field(default_factory=lambda: Path.home() / ".zsh_history")
    activity_window_seconds: int = 3600

    # Health
    mounts: list[str] = field(default_factory=lambda: ["/"])
    disk_pressure_percent: float = 90.0
    high_load_threshold: float = 8.0

    # Runtime
    state_dir: Path = field(default_factory=_default_state_dir)
    poll_interval_seconds: float = 300.0
    ingest_url: str | None = None
    client_cert_header: str | None = None

    # Per-collector enable
    enable_git: bool = True
    enable_jobs: bool = True
    enable_activity: bool = True
    enable_health: bool = True

    def repo_key(self, name: str) -> str:
        """The name (or sensitive alias/hash) used in the structured signal."""
        if name not in self.sensitive_repos:
            return name
        alias = self.sensitive_repo_aliases.get(name)
        if alias:
            return alias
        import hashlib

        return "repo-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]

    @property
    def job_verb_re(self) -> re.Pattern[str]:
        return re.compile(self.job_pattern, re.IGNORECASE)

    @property
    def job_state_path(self) -> Path:
        return Path(self.state_dir) / "devbox_jobs.json"


# --------------------------------------------------------------------------- #
# Observation → Event
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DevboxObservation:
    """A single content-free structured observation from a collector, one-to-one with
    an emitted :class:`Event`."""

    kind: str
    event_id: str
    summary: str
    structured: dict[str, Any]
    occurred_at: datetime | None = None


def observation_to_event(
    obs: DevboxObservation,
    *,
    source: str,
    account_ref: str,
    nas: NASStore,
    confidence: float = 1.0,
) -> Event:
    """Wrap an observation as a provenance-tagged Event.

    The observation is content-free by construction, so the "raw evidence" we persist to
    NAS *is* the structured observation itself — it gives every devbox fact a retrievable
    provenance blob + payload hash, exactly like the reference adapters, without ever
    storing verbatim content (there is none)."""
    payload = json.dumps(
        {"kind": obs.kind, "structured": obs.structured}, sort_keys=True, default=str
    ).encode("utf-8")
    ref = nas.put(payload)
    return Event(
        event_id=obs.event_id,
        source=source,
        account_ref=account_ref,
        kind=obs.kind,
        occurred_at=obs.occurred_at,
        payload_hash=ref.hash,
        raw_evidence_ref=ref.id,
        summary=obs.summary,
        confidence=confidence,
        structured=dict(obs.structured),
    )


def _epoch_to_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def command_verb(argv: Sequence[str]) -> str:
    """The command *verb*: basename of ``argv[0]`` with any path/args stripped.

    This is the only piece of a command line we ever keep — arguments carry paths and
    tokens, so ``argv[1:]`` is discarded and never emitted."""
    if not argv:
        return ""
    first = str(argv[0])
    # An interpreter invoked as an absolute path → keep just the program name.
    return os.path.basename(first) or first


# --------------------------------------------------------------------------- #
# Collector 1 — Git WIP
# --------------------------------------------------------------------------- #


@dataclass
class _GitStatus:
    branch: str
    ahead: int
    behind: int
    dirty: bool
    untracked_count: int
    changed_paths: list[str]


def parse_git_porcelain_v2(text: str) -> _GitStatus:
    """Parse ``git status --porcelain=v2 --branch`` into a structured status.

    Only branch/ahead/behind/dirty/untracked and the *paths of changed files* are read;
    paths are used solely to stat mtimes for the stale-WIP age and are **never emitted**.
    """
    branch = "(detached)"
    ahead = behind = 0
    untracked = 0
    changed: list[str] = []
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head ") :].strip()
        elif line.startswith("# branch.ab "):
            for tok in line.split()[2:]:
                if tok.startswith("+"):
                    ahead = int(tok[1:] or 0)
                elif tok.startswith("-"):
                    behind = int(tok[1:] or 0)
        elif line.startswith(("1 ", "2 ")):
            # ordinary/renamed change; path is the tail (rename has a tab-separated orig)
            path = line.split(" ", 8)[-1].split("\t", 1)[0]
            changed.append(path)
        elif line.startswith("u "):
            changed.append(line.split(" ", 10)[-1])
        elif line.startswith("? "):
            untracked += 1
            changed.append(line[2:])
    return _GitStatus(
        branch=branch,
        ahead=ahead,
        behind=behind,
        dirty=_has_tracked(text),
        untracked_count=untracked,
        changed_paths=[c for c in changed if c],
    )


def _has_tracked(text: str) -> bool:
    """True if the working tree has any *tracked* change (staged/unstaged/unmerged)."""
    return any(line.startswith(("1 ", "2 ", "u ")) for line in text.splitlines())


class GitWipCollector:
    """Emits ``devbox.git_status`` per own-user repo (stale-WIP signal)."""

    def __init__(self, config: DevboxCollectorConfig, probe: DevboxProbe) -> None:
        self.config = config
        self.probe = probe

    def observe(self) -> list[DevboxObservation]:
        now = self.probe.now()
        current_uid = self.probe.current_uid()
        roots = [Path(r) for r in self.config.repo_roots]
        repos, cap_hit = self.probe.find_repos(
            roots, self.config.repo_max_depth, self.config.repo_max_count
        )
        if cap_hit:
            log_event(
                _log, 30, "devbox.git.cap_hit",
                cap=self.config.repo_max_count, roots=[str(r) for r in roots],
            )
        out: list[DevboxObservation] = []
        for repo in repos:
            if not owned_by_current_user(repo.uid, current_uid):
                continue
            porcelain = self.probe.git(
                repo.path,
                ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            )
            if porcelain is None:
                continue
            status = parse_git_porcelain_v2(porcelain)
            age = self._newest_change_age(repo.path, status.changed_paths, now)
            has_uncommitted = status.dirty or status.untracked_count > 0
            is_stale = (
                has_uncommitted and age is not None and age >= self.config.stale_wip_seconds
            )
            key = self.config.repo_key(repo.path.name)
            structured = {
                "repo": key,
                "branch": status.branch,
                "ahead": status.ahead,
                "behind": status.behind,
                "dirty": status.dirty,
                "untracked_count": status.untracked_count,
                "newest_change_age_seconds": None if age is None else round(age, 1),
                "is_stale": is_stale,
            }
            state = "dirty" if status.dirty else "clean"
            summary = (
                f"git {key}@{status.branch}: {state}, {status.untracked_count} untracked, "
                f"ahead {status.ahead}/behind {status.behind}"
                + (" (stale WIP)" if is_stale else "")
            )
            out.append(
                DevboxObservation(
                    kind="devbox.git_status",
                    event_id=f"devbox:git:{key}",
                    summary=summary,
                    structured=structured,
                    occurred_at=_epoch_to_dt(now),
                )
            )
        return out

    def _newest_change_age(
        self, repo: Path, changed_paths: Iterable[str], now: float
    ) -> float | None:
        newest: float | None = None
        for rel in changed_paths:
            mtime = self.probe.path_mtime(repo / rel)
            if mtime is not None and (newest is None or mtime > newest):
                newest = mtime
        return None if newest is None else max(0.0, now - newest)


# --------------------------------------------------------------------------- #
# Collector 2 — Job lifecycle (the headline "your run finished while away")
# --------------------------------------------------------------------------- #


class JobStateStore:
    """Tiny on-disk tracker of running jobs so a finish is detected across poll runs.

    Persists ONLY ``{job_key: {verb, start_time, first_seen}}`` — no argv, no paths."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, state: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, sort_keys=True))


class JobLifecycleCollector:
    """Emits ``devbox.job_finished`` when a tracked own-user job disappears (or a
    snapshot reports its exit)."""

    def __init__(
        self,
        config: DevboxCollectorConfig,
        probe: DevboxProbe,
        *,
        state: JobStateStore | None = None,
    ) -> None:
        self.config = config
        self.probe = probe
        self.state = state or JobStateStore(config.job_state_path)

    def observe(self) -> list[DevboxObservation]:
        now = self.probe.now()
        current_uid = self.probe.current_uid()
        verb_re = self.config.job_verb_re

        running: dict[str, dict[str, Any]] = {}
        finished_now: list[tuple[str, str, float, int]] = []
        for p in self.probe.list_processes():
            if not owned_by_current_user(p.uid, current_uid):
                continue
            verb = command_verb(p.argv)
            if not verb or not verb_re.fullmatch(verb):
                continue
            key = f"{p.pid}:{int(p.start_time)}"
            if p.returncode is None:
                running[key] = {"verb": verb, "start_time": p.start_time}
            else:
                finished_now.append((key, verb, p.start_time, p.returncode))

        prev = self.state.load()
        finished_keys = {k for k, *_ in finished_now}
        out: list[DevboxObservation] = []

        # (a) tracked jobs that vanished between polls → finished, outcome unknown.
        for key, info in prev.items():
            if key in running or key in finished_keys:
                continue
            out.append(
                self._finished_event(
                    key, info["verb"], now - float(info["start_time"]), None, now
                )
            )

        # (b) jobs a snapshot reports as exited → determinable success/failure.
        for key, verb, start_time, rc in finished_now:
            out.append(self._finished_event(key, verb, now - start_time, rc, now))

        # Persist the still-running set (carry first_seen forward).
        new_state = {
            key: {
                "verb": info["verb"],
                "start_time": info["start_time"],
                "first_seen": prev.get(key, {}).get("first_seen", now),
            }
            for key, info in running.items()
        }
        self.state.save(new_state)
        return out

    def _finished_event(
        self, key: str, verb: str, duration: float, rc: int | None, now: float
    ) -> DevboxObservation:
        if rc is None:
            outcome = "unknown"
        elif rc == 0:
            outcome = "success"
        else:
            outcome = "failure"
        structured = {
            "verb": verb,
            "duration_seconds": round(max(0.0, duration), 1),
            "outcome": outcome,
            "returncode": rc,
        }
        summary = (
            f"job '{verb}' finished ({outcome}) after {_human_duration(max(0.0, duration))}"
        )
        return DevboxObservation(
            kind="devbox.job_finished",
            event_id=f"devbox:job:{key}",
            summary=summary,
            structured=structured,
            occurred_at=_epoch_to_dt(now),
        )


def _human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


# --------------------------------------------------------------------------- #
# Collector 3 — Session / activity
# --------------------------------------------------------------------------- #

#: zsh EXTENDED_HISTORY line: ``: <epoch>:<elapsed>;<command>``. We capture ONLY the
#: epoch group; the command after ``;`` is never read into any field.
_ZSH_HISTORY_TS_RE = re.compile(r"^:\s*(\d{6,}):\-?\d+;")


def parse_history_timestamps(lines: Iterable[str]) -> tuple[bool, list[int]]:
    """Reduce raw history lines to (has_timestamps, [epoch, ...]) — timestamps only.

    Returns ``has_timestamps=False`` for a non-empty history with no timestamped lines
    (bash without ``HISTTIMEFORMAT``): there is no cadence to extract, so it is skipped.
    """
    any_lines = False
    timestamps: list[int] = []
    for line in lines:
        any_lines = any_lines or bool(line.strip())
        m = _ZSH_HISTORY_TS_RE.match(line)
        if m:
            timestamps.append(int(m.group(1)))
    has_timestamps = bool(timestamps) or not any_lines
    return has_timestamps, timestamps


class SessionActivityCollector:
    """Emits ``devbox.activity``: tmux session counts + history activity cadence."""

    def __init__(self, config: DevboxCollectorConfig, probe: DevboxProbe) -> None:
        self.config = config
        self.probe = probe

    def observe(self) -> list[DevboxObservation]:
        now = self.probe.now()
        current_uid = self.probe.current_uid()

        sessions = [
            s for s in self.probe.list_tmux() if owned_by_current_user(s.uid, current_uid)
        ]
        attached = sum(1 for s in sessions if s.attached)
        newest_activity = max((s.last_activity for s in sessions), default=None)
        newest_age = None if newest_activity is None else round(max(0.0, now - newest_activity), 1)

        lines = self.probe.read_history_lines(Path(self.config.history_path))
        has_ts, timestamps = parse_history_timestamps(lines)
        window_start = now - self.config.activity_window_seconds
        recent = sorted(t for t in timestamps if t >= window_start)

        structured = {
            "tmux_session_count": len(sessions),
            "tmux_attached": attached,
            "tmux_detached": len(sessions) - attached,
            "tmux_newest_activity_age_seconds": newest_age,
            "history_available": has_ts and bool(timestamps),
            "command_count": len(recent),
            "command_timestamps": recent,
        }
        summary = (
            f"activity: {len(recent)} commands/"
            f"{self.config.activity_window_seconds // 60}m, "
            f"{len(sessions)} tmux sessions ({attached} attached)"
        )
        return [
            DevboxObservation(
                kind="devbox.activity",
                event_id=f"devbox:activity:{int(now)}",
                summary=summary,
                structured=structured,
                occurred_at=_epoch_to_dt(now),
            )
        ]


# --------------------------------------------------------------------------- #
# Collector 4 — Health
# --------------------------------------------------------------------------- #

_UNHEALTHY_MARKERS = ("unhealthy", "exited", "dead", "restarting")


class HealthCollector:
    """Emits ``devbox.health``: load, disk pressure, own-container health."""

    def __init__(self, config: DevboxCollectorConfig, probe: DevboxProbe) -> None:
        self.config = config
        self.probe = probe

    def observe(self) -> list[DevboxObservation]:
        now = self.probe.now()
        current_uid = self.probe.current_uid()

        load1, load5, load15 = self.probe.load_avg()

        mounts: list[dict[str, Any]] = []
        disk_pressure = False
        for mount in self.config.mounts:
            usage = self.probe.disk_usage(mount)
            if usage is None or usage.total <= 0:
                continue
            percent = round(usage.used / usage.total * 100, 1)
            disk_pressure = disk_pressure or percent >= self.config.disk_pressure_percent
            mounts.append(
                {"mount": mount, "percent_used": percent, "free_gb": round(usage.free / 1e9, 2)}
            )

        containers: list[dict[str, str]] = []
        unhealthy = 0
        for c in self.probe.list_containers():
            if not owned_by_current_user(c.uid, current_uid):
                continue
            # Alias a container whose name matches a sensitive repo, same as git repos, so
            # a client/project identifier in a container name never reaches the egress.
            containers.append({"name": self.config.repo_key(c.name), "status": c.status})
            if any(m in c.status.lower() for m in _UNHEALTHY_MARKERS):
                unhealthy += 1

        high_load = load1 >= self.config.high_load_threshold
        structured = {
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "mounts": mounts,
            "containers": containers,
            "disk_pressure": disk_pressure,
            "high_load": high_load,
            "unhealthy_container_count": unhealthy,
        }
        worst_disk = max((m["percent_used"] for m in mounts), default=0.0)
        summary = (
            f"health: load {round(load1, 2)}, disk {worst_disk}%, "
            f"{len(containers)} containers, {unhealthy} unhealthy"
        )
        return [
            DevboxObservation(
                kind="devbox.health",
                event_id=f"devbox:health:{int(now)}",
                summary=summary,
                structured=structured,
                occurred_at=_epoch_to_dt(now),
            )
        ]


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


@registry.register
class DevboxAdapter(Adapter):
    """The devbox source adapter — composes the four collectors into the adapter
    contract. ``fetch`` gathers each enabled collector's content-free observations;
    ``normalize`` wraps one as a provenance-tagged :class:`Event`."""

    provider = "devbox"
    acquisition_tier = AcquisitionTier.DEVICE_OS_API

    def __init__(
        self,
        account_ref: str = "devbox",
        *,
        vault: CredentialVault | None = None,
        nas: NASStore | None = None,
        config: DevboxCollectorConfig | None = None,
        probe: DevboxProbe | None = None,
        collectors: Sequence[Any] | None = None,
    ) -> None:
        super().__init__(account_ref, vault=vault)
        self.config = config or DevboxCollectorConfig()
        self.probe = probe or LiveProbe()
        self._nas = nas or NASStore()
        self.collectors = list(collectors) if collectors is not None else self._build_collectors()

    def _build_collectors(self) -> list[Any]:
        collectors: list[Any] = []
        if self.config.enable_git:
            collectors.append(GitWipCollector(self.config, self.probe))
        if self.config.enable_jobs:
            collectors.append(JobLifecycleCollector(self.config, self.probe))
        if self.config.enable_activity:
            collectors.append(SessionActivityCollector(self.config, self.probe))
        if self.config.enable_health:
            collectors.append(HealthCollector(self.config, self.probe))
        return collectors

    def fetch(self) -> list[DevboxObservation]:
        observations: list[DevboxObservation] = []
        for collector in self.collectors:
            try:
                observations.extend(collector.observe())
            except Exception as exc:  # one collector failing must not sink the others
                log_event(
                    _log, 40, "devbox.collector_failed",
                    collector=type(collector).__name__, error=str(exc),
                )
        return observations

    def normalize(self, raw: DevboxObservation) -> Event:
        return observation_to_event(
            raw, source=self.provider, account_ref=self.account_ref, nas=self._nas
        )


# --------------------------------------------------------------------------- #
# Runtime poller (NOT exercised by tests — live syscalls + network)
# --------------------------------------------------------------------------- #


def _post_event(url: str, event: Event, *, cert_header: str | None) -> None:
    import urllib.request

    body = event.model_dump_json().encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cert_header:
        headers["X-Client-Cert"] = cert_header
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — configured URL
        resp.read()


def run_once(config: DevboxCollectorConfig, *, probe: DevboxProbe | None = None) -> list[Event]:
    """Run every enabled collector once and (if configured) POST each Event to the
    ingest endpoint. Returns the emitted Events (for logging/inspection)."""
    adapter = DevboxAdapter(config.account_ref, config=config, probe=probe)
    events = list(adapter.emit())
    for event in events:
        if config.ingest_url:
            _post_event(config.ingest_url, event, cert_header=config.client_cert_header)
        log_event(_log, 20, "devbox.emitted", kind=event.kind, event_id=event.event_id)
    return events


def _config_from_env(env: dict[str, str] | None = None) -> DevboxCollectorConfig:
    env = dict(os.environ if env is None else env)
    cfg = DevboxCollectorConfig()
    if roots := env.get("CADENCE_DEVBOX_REPO_ROOTS"):
        cfg.repo_roots = [Path(p) for p in roots.split(os.pathsep) if p]
    if url := env.get("CADENCE_INGEST_URL"):
        cfg.ingest_url = url
    if hist := env.get("CADENCE_DEVBOX_HISTORY"):
        cfg.history_path = Path(hist)
    if interval := env.get("CADENCE_DEVBOX_POLL_SECONDS"):
        cfg.poll_interval_seconds = float(interval)
    if cert := env.get("CADENCE_CLIENT_CERT"):
        cfg.client_cert_header = cert
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    """Thin poll loop: ``python -m cadence.adapters.devbox`` — the runtime step.

    Reads config from the environment, then runs the collectors every
    ``poll_interval_seconds`` and feeds each Event to ``CADENCE_INGEST_URL``. Pass
    ``--once`` to run a single pass. Never imported by tests."""
    argv = list(argv if argv is not None else [])
    once = "--once" in argv
    config = _config_from_env()
    while True:
        try:
            run_once(config)
        except Exception as exc:  # a poll must never crash the loop
            log_event(_log, 40, "devbox.poll_failed", error=str(exc))
        if once:
            return 0
        time.sleep(config.poll_interval_seconds)


__all__ = [
    "DevboxAdapter",
    "DevboxCollectorConfig",
    "DevboxObservation",
    "DevboxProbe",
    "LiveProbe",
    "GitWipCollector",
    "JobLifecycleCollector",
    "SessionActivityCollector",
    "HealthCollector",
    "JobStateStore",
    "ProcessInfo",
    "TmuxSession",
    "RepoDir",
    "DiskUsage",
    "ContainerInfo",
    "owned_by_current_user",
    "command_verb",
    "parse_git_porcelain_v2",
    "parse_history_timestamps",
    "observation_to_event",
    "run_once",
    "main",
]


if __name__ == "__main__":  # pragma: no cover — runtime entrypoint
    raise SystemExit(main())
