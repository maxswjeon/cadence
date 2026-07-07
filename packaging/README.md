# Packaging — run Cadence as systemd services (with `uv`)

Cadence installs a single `cadence` console command (from `pyproject.toml`'s
`[project.scripts]`) with subcommands:

| Command | What it runs | Unit |
|---------|--------------|------|
| `cadence runtime` | the brain — ingest API + attention engine + nudge delivery | `cadence-runtime.service` |
| `cadence devbox`  | the content-free, own-user dev-server signal poller (`--once` for one pass) | `cadence-devbox.service` |
| `cadence onboarding` | store an LLM provider credential in the vault | — (run manually) |

## Quick install (user services)

```bash
# one-time: get uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# from the repo root
packaging/install.sh
```

That uses **uv** to build the virtualenv and install Cadence (editable), drops your
config at `~/.config/cadence/cadence.env`, and installs the two **user** units. Then:

```bash
$EDITOR ~/.config/cadence/cadence.env          # set port, mode, ingest URL, ...
systemctl --user enable --now cadence-runtime cadence-devbox
journalctl --user -u cadence-runtime -f
curl http://127.0.0.1:3245/healthz             # default port is 3245 (0x0CAD)
loginctl enable-linger "$USER"                 # keep running while logged out
```

### Why **user** services (not system/root)

The devbox source is **own-user-only by construction** — it captures only the current
user's repos, processes, tmux, and containers. Running it as your user (not root) is the
correct, safe match for that design; a system/root service would read across users and
defeat the privacy model. The runtime is single-user too, so both belong in
`systemctl --user`.

## Manual setup (without the script)

```bash
uv venv ~/.local/share/cadence/venv
uv pip install --python ~/.local/share/cadence/venv/bin/python -e .
mkdir -p ~/.config/cadence ~/.local/state/cadence ~/.config/systemd/user
cp packaging/systemd/cadence.env.example ~/.config/cadence/cadence.env
cp packaging/systemd/cadence-*.service    ~/.config/systemd/user/
systemctl --user daemon-reload
```

## Layout the units assume

| Path | Purpose |
|------|---------|
| `~/.local/share/cadence/venv` | the uv-managed virtualenv (`ExecStart` points into its `bin/`) |
| `~/.config/cadence/cadence.env` | config, loaded via `EnvironmentFile=` |
| `~/.local/state/cadence` | `WorkingDirectory`; runtime data lands in `var/` here (D1, NAS, R2, vault) |

The runtime **self-initializes its SQLite schema** on start — no `alembic upgrade` step is
needed for the service.

## System-wide install (optional)

If you must run it as a system service instead, copy the units to `/etc/systemd/system/`,
replace `%h` with an absolute home, add `User=<you>`/`Group=<you>` under `[Service]`, point
`WorkingDirectory`/`EnvironmentFile`/`ExecStart` at absolute paths, and use `WantedBy=
multi-user.target`. Keep `User=` set to the account whose signals you want captured — never
root.

## Configuration

Every knob is a `CADENCE_*` environment variable — see `cadence.env.example` (annotated),
`cadence/config.py` (storage/vault/LLM/mTLS), and `RuntimeConfig` in
`cadence/runtime/service.py` (port, governor mode, delivery). The default HTTP port is
**3245** (`0x0CAD`), overridable with `CADENCE_HTTP_PORT`.
