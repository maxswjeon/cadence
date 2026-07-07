"""Unit + privacy tests for the four devbox collectors.

Every collector is driven purely by an injected probe (fixture data, or real *temp* git
repos for the git collector) — no live system dependency. The privacy assertions are the
point: no command args, no file/source content, no other-user data, and own-user scope
enforced, all proven against the *emitted Event*, not just the raw observation.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from _devbox_util import StubProbe

from cadence.adapters.base import AcquisitionTier
from cadence.adapters.devbox import (
    ContainerInfo,
    DevboxAdapter,
    DevboxCollectorConfig,
    DevboxObservation,
    DiskUsage,
    GitWipCollector,
    HealthCollector,
    JobLifecycleCollector,
    JobStateStore,
    LiveProbe,
    ProcessInfo,
    SessionActivityCollector,
    TmuxSession,
    command_verb,
    observation_to_event,
    owned_by_current_user,
    parse_history_timestamps,
)
from cadence.stores.nas import NASStore

NOW = 1_720_000_000.0


def _event(obs: DevboxObservation, nas: NASStore):
    return observation_to_event(obs, source="devbox", account_ref="devbox", nas=nas)


def _event_text(obs: DevboxObservation, nas: NASStore) -> str:
    ev = _event(obs, nas)
    return json.dumps(ev.structured) + (ev.summary or "") + ev.model_dump_json()


# --------------------------------------------------------------------------- #
# Own-user scope guard
# --------------------------------------------------------------------------- #


def test_user_scope_guard() -> None:
    assert owned_by_current_user(1000, 1000) is True
    assert owned_by_current_user(0, 1000) is False
    assert owned_by_current_user(1001, 1000) is False
    assert owned_by_current_user(None, 1000) is False  # undeterminable → fail closed


def test_command_verb_strips_path_and_args() -> None:
    assert command_verb(["/usr/bin/python3", "train.py", "--token=SECRET"]) == "python3"
    assert command_verb(["node", "server.js"]) == "node"
    assert command_verb([]) == ""


# --------------------------------------------------------------------------- #
# Collector 1 — Git WIP, on REAL temp git repos (live git runner)
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _git_events(projects: Path, settings, config: DevboxCollectorConfig | None = None):
    cfg = config or DevboxCollectorConfig(repo_roots=[projects])
    cfg.repo_roots = [projects]
    cfg.enable_jobs = cfg.enable_activity = cfg.enable_health = False
    adapter = DevboxAdapter(
        config=cfg, probe=LiveProbe(), nas=NASStore(settings)
    )
    return {e.structured["repo"]: e for e in adapter.emit()}


def test_git_clean_repo(tmp_path, settings) -> None:
    projects = tmp_path / "projects"
    _init_repo(projects / "clean")
    events = _git_events(projects, settings)
    ev = events["clean"]
    assert ev.kind == "devbox.git_status"
    assert ev.structured["branch"] == "main"
    assert ev.structured["dirty"] is False
    assert ev.structured["untracked_count"] == 0
    assert ev.structured["newest_change_age_seconds"] is None
    assert ev.acquisition_tier is AcquisitionTier.DEVICE_OS_API


def test_git_dirty_and_untracked_repo_hides_filenames(tmp_path, settings) -> None:
    projects = tmp_path / "projects"
    repo = _init_repo(projects / "svc")
    # A tracked modification that carries a secret, plus an untracked secret file.
    (repo / "README.md").write_text("API_KEY=SUPERSECRETVALUE\n")
    (repo / "secret_credentials.py").write_text("password = 'HUNTER2'\n")

    events = _git_events(projects, settings)
    ev = events["svc"]
    assert ev.structured["dirty"] is True
    assert ev.structured["untracked_count"] == 1

    # No file contents and no filenames leak into the Event.
    blob = json.dumps(ev.structured) + (ev.summary or "") + ev.model_dump_json()
    for forbidden in ("SUPERSECRETVALUE", "HUNTER2", "secret_credentials", "README.md"):
        assert forbidden not in blob


def test_git_ahead_behind(tmp_path, settings) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    projects = tmp_path / "projects"
    projects.mkdir()
    work = projects / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "a.txt").write_text("1\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "c1")
    _git(work, "push", "-q", "origin", "main")
    # One local commit not pushed → ahead 1, behind 0.
    (work / "b.txt").write_text("2\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-q", "-m", "c2")

    events = _git_events(projects, settings)
    ev = events["work"]
    assert ev.structured["ahead"] == 1
    assert ev.structured["behind"] == 0


def test_git_stale_wip_flagged(tmp_path, settings) -> None:
    projects = tmp_path / "projects"
    repo = _init_repo(projects / "stale")
    (repo / "README.md").write_text("work in progress\n")
    # Backdate the uncommitted change so it reads as old.
    old = NOW - 10 * 3600
    os.utime(repo / "README.md", (old, old))

    cfg = DevboxCollectorConfig(repo_roots=[projects], stale_wip_seconds=8 * 3600)
    # Use the live probe for git/discovery but pin "now" far in the future of the mtime.
    probe = LiveProbe()
    probe.now = lambda: NOW  # type: ignore[method-assign]
    cfg.enable_jobs = cfg.enable_activity = cfg.enable_health = False
    adapter = DevboxAdapter(config=cfg, probe=probe, nas=NASStore(settings))
    ev = {e.structured["repo"]: e for e in adapter.emit()}["stale"]
    assert ev.structured["dirty"] is True
    assert ev.structured["is_stale"] is True
    assert ev.structured["newest_change_age_seconds"] >= 8 * 3600


def test_git_sensitive_repo_is_aliased(tmp_path, settings) -> None:
    projects = tmp_path / "projects"
    _init_repo(projects / "client-acme")
    cfg = DevboxCollectorConfig(
        repo_roots=[projects],
        sensitive_repos=frozenset({"client-acme"}),
        sensitive_repo_aliases={"client-acme": "project-x"},
    )
    events = _git_events(projects, settings, cfg)
    assert "client-acme" not in events
    ev = events["project-x"]
    assert "client-acme" not in ev.model_dump_json()


def test_live_git_runner_refuses_write_subcommands(tmp_path) -> None:
    import pytest

    repo = _init_repo(tmp_path / "projects" / "ro")
    probe = LiveProbe()
    # Read-only porcelain is allowed...
    assert probe.git(repo, ["status", "--porcelain=v2"]) is not None
    # ...but any mutating subcommand is refused before it can touch the repo.
    for write in (["commit", "-m", "x"], ["push"], ["checkout", "-b", "y"], ["reset", "--hard"]):
        with pytest.raises(ValueError, match="read-only"):
            probe.git(repo, write)


def test_git_other_uid_repo_is_not_captured(settings) -> None:
    from cadence.adapters.devbox import RepoDir

    mine = Path("/repos/mine")
    theirs = Path("/repos/theirs")
    porcelain = "# branch.head main\n# branch.ab +0 -0\n"
    probe = StubProbe(
        now_value=NOW,
        uid=1000,
        repos=[RepoDir(mine, uid=1000), RepoDir(theirs, uid=1001)],
        git_outputs={mine: porcelain, theirs: porcelain},
    )
    obs = GitWipCollector(DevboxCollectorConfig(), probe).observe()
    assert [o.structured["repo"] for o in obs] == ["mine"]
    # The other user's repo was never even shelled out to.
    assert all(repo != theirs for repo, _ in probe.git_calls)


# --------------------------------------------------------------------------- #
# Collector 2 — Job lifecycle
# --------------------------------------------------------------------------- #


def _job_config(tmp_path) -> DevboxCollectorConfig:
    return DevboxCollectorConfig(state_dir=tmp_path / "state")


def test_job_disappearance_emits_finished_with_verb_and_duration(tmp_path, settings) -> None:
    cfg = _job_config(tmp_path)
    nas = NASStore(settings)
    # Poll 1: a tracked training job with a SECRET token in its argv is running.
    probe = StubProbe(
        now_value=NOW,
        processes=[
            ProcessInfo(
                pid=4242,
                uid=1000,
                argv=["/usr/bin/python3", "train.py", "--token=SUPERSECRETTOKEN"],
                start_time=NOW - 100,
            )
        ],
    )
    first = JobLifecycleCollector(cfg, probe).observe()
    assert first == []  # running, not finished yet

    # Poll 2: the job has disappeared.
    probe.processes = []
    probe.now_value = NOW + 50
    finished = JobLifecycleCollector(cfg, probe).observe()
    assert len(finished) == 1
    obs = finished[0]
    assert obs.kind == "devbox.job_finished"
    assert obs.structured["verb"] == "python3"
    assert obs.structured["duration_seconds"] == 150.0
    assert obs.structured["outcome"] == "unknown"

    # The secret token and script argument NEVER appear in the emitted Event.
    text = _event_text(obs, nas)
    assert "SUPERSECRETTOKEN" not in text
    assert "train.py" not in text
    assert "--token" not in text


def test_job_determinable_success_and_failure(tmp_path) -> None:
    cfg = _job_config(tmp_path)
    probe = StubProbe(
        now_value=NOW,
        processes=[
            ProcessInfo(pid=1, uid=1000, argv=["pytest"], start_time=NOW - 30, returncode=0),
            ProcessInfo(pid=2, uid=1000, argv=["make"], start_time=NOW - 60, returncode=2),
        ],
    )
    obs = {o.structured["verb"]: o for o in JobLifecycleCollector(cfg, probe).observe()}
    assert obs["pytest"].structured["outcome"] == "success"
    assert obs["make"].structured["outcome"] == "failure"
    assert obs["make"].structured["duration_seconds"] == 60.0


def test_job_other_uid_process_is_never_tracked(tmp_path) -> None:
    cfg = _job_config(tmp_path)
    probe = StubProbe(
        now_value=NOW,
        uid=1000,
        processes=[ProcessInfo(pid=9, uid=0, argv=["python", "evil.py"], start_time=NOW - 10)],
    )
    assert JobLifecycleCollector(cfg, probe).observe() == []
    # And nothing about the foreign job was persisted to the tracker.
    assert JobStateStore(cfg.job_state_path).load() == {}


def test_job_non_matching_command_is_ignored(tmp_path) -> None:
    cfg = _job_config(tmp_path)
    probe = StubProbe(
        now_value=NOW,
        processes=[ProcessInfo(pid=5, uid=1000, argv=["vim", "notes.md"], start_time=NOW - 10)],
    )
    JobLifecycleCollector(cfg, probe).observe()
    assert JobStateStore(cfg.job_state_path).load() == {}


# --------------------------------------------------------------------------- #
# Collector 3 — Session / activity
# --------------------------------------------------------------------------- #


def test_activity_counts_and_timestamps_only_no_command_text(settings) -> None:
    nas = NASStore(settings)
    probe = StubProbe(
        now_value=NOW,
        uid=1000,
        tmux=[
            TmuxSession("main", attached=True, last_activity=NOW - 30, uid=1000),
            TmuxSession("bg", attached=False, last_activity=NOW - 600, uid=1000),
            TmuxSession("intruder", attached=True, last_activity=NOW - 5, uid=1001),
        ],
        history=[
            f": {int(NOW) - 100}:0;git push --token=SUPERSECRET",
            f": {int(NOW) - 50}:0;ls -la /etc/shadow",
            "a-plain-bash-line-without-timestamp",
        ],
    )
    obs = SessionActivityCollector(DevboxCollectorConfig(), probe).observe()
    assert len(obs) == 1
    o = obs[0]
    # tmux: the foreign-uid session is excluded from the counts.
    assert o.structured["tmux_session_count"] == 2
    assert o.structured["tmux_attached"] == 1
    assert o.structured["tmux_detached"] == 1
    # history cadence: count + timestamps only.
    assert o.structured["command_count"] == 2
    assert o.structured["command_timestamps"] == [int(NOW) - 100, int(NOW) - 50]
    assert o.structured["history_available"] is True

    text = _event_text(o, nas)
    for forbidden in ("SUPERSECRET", "git push", "--token", "shadow", "ls -la", "intruder"):
        assert forbidden not in text


def test_activity_bash_without_timestamps_is_skipped() -> None:
    probe = StubProbe(now_value=NOW, history=["ls -la", "cd /home", "make build"])
    o = SessionActivityCollector(DevboxCollectorConfig(), probe).observe()[0]
    assert o.structured["history_available"] is False
    assert o.structured["command_count"] == 0
    assert o.structured["command_timestamps"] == []


def test_parse_history_timestamps_helper() -> None:
    has_ts, ts = parse_history_timestamps([": 1720000000:0;echo hi", ": 1720000005:12;git status"])
    assert has_ts is True
    assert ts == [1720000000, 1720000005]
    # Empty history is "available" (nothing to skip), just zero commands.
    assert parse_history_timestamps([]) == (True, [])
    # Non-timestamped, non-empty → not available.
    assert parse_history_timestamps(["ls"]) == (False, [])


# --------------------------------------------------------------------------- #
# Collector 4 — Health
# --------------------------------------------------------------------------- #


def test_health_load_disk_and_own_container_status(settings) -> None:
    nas = NASStore(settings)
    probe = StubProbe(
        now_value=NOW,
        uid=1000,
        load=(9.5, 6.0, 4.0),
        disks={"/": DiskUsage(total=100, used=95, free=5)},
        containers=[
            ContainerInfo("db", "unhealthy", uid=1000),
            ContainerInfo("web", "Up 3 hours", uid=1000),
            ContainerInfo("other-user-box", "running", uid=0),
        ],
    )
    cfg = DevboxCollectorConfig(mounts=["/"], disk_pressure_percent=90.0, high_load_threshold=8.0)
    o = HealthCollector(cfg, probe).observe()[0]
    assert o.structured["load1"] == 9.5
    assert o.structured["high_load"] is True
    assert o.structured["disk_pressure"] is True
    assert o.structured["mounts"] == [{"mount": "/", "percent_used": 95.0, "free_gb": 0.0}]
    names = {c["name"] for c in o.structured["containers"]}
    assert names == {"db", "web"}  # the other user's container is excluded
    assert o.structured["unhealthy_container_count"] == 1
    assert "other-user-box" not in _event_text(o, nas)


class _FakeProc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _fake_docker(*, info_stdout: str, info_rc: int, ps_stdout: str, ps_rc: int):
    """Dispatch a stubbed ``subprocess.run`` on whether it's ``docker info`` or ``ps``."""

    def run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if "info" in cmd:
            return _FakeProc(info_rc, info_stdout)
        return _FakeProc(ps_rc, ps_stdout)

    return run


def test_live_containers_fail_closed_on_rootful_docker(monkeypatch) -> None:
    # A shared *rootful* daemon lists every user's containers; ownership is undeterminable
    # so LiveProbe must NOT trust `docker ps` — it captures nothing (fail closed) and does
    # not let a foreign container name reach an Event. (StubProbe-based tests can't catch
    # this — only the live probe fabricated the owner.)
    probe = LiveProbe()
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_docker(
            info_stdout='["seccomp","apparmor"]',
            info_rc=0,
            ps_stdout="acme-prod-db\x1fUp 2 hours\n",
            ps_rc=0,
        ),
    )
    assert probe.list_containers() == []


def test_live_containers_captured_under_rootless_docker(monkeypatch) -> None:
    # A rootless (per-user) daemon lists ONLY this user's containers, so they are genuinely
    # own-user and captured with the current uid.
    probe = LiveProbe()
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_docker(
            info_stdout='["name=rootless","seccomp"]',
            info_rc=0,
            ps_stdout="mine\x1fUp 1 hour\n",
            ps_rc=0,
        ),
    )
    got = probe.list_containers()
    assert [c.name for c in got] == ["mine"]
    assert all(c.uid == os.getuid() for c in got)


def test_live_containers_fail_closed_when_docker_unavailable(monkeypatch) -> None:
    # No docker (or `docker info` errors) -> rootless is unproven -> fail closed.
    probe = LiveProbe()

    def boom(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise OSError("docker not installed")

    monkeypatch.setattr(subprocess, "run", boom)
    assert probe.list_containers() == []
