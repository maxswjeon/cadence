"""Unified ``cadence`` command-line entry point.

One executable, subcommands — ``cadence runtime`` / ``cadence devbox`` / ``cadence
onboarding`` — installed via ``[project.scripts]`` in ``pyproject.toml``. Each subcommand
simply dispatches to the module that already owns that behaviour, so the CLI is a thin
router with no logic of its own.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_USAGE = """\
cadence <command> [args]

Commands:
  runtime      Run the brain: ingest API + attention engine + nudge delivery
  devbox       Poll this dev server for content-free, own-user signals (--once for one pass)
  onboarding   Store an LLM provider credential (--provider openai_api|chatgpt_oauth)

Configuration is environment-based (CADENCE_* — see cadence/config.py and RuntimeConfig).
Run `cadence <command> --help` where supported."""


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(_USAGE)
        return 0

    if cmd == "runtime":
        if rest:
            print("cadence runtime takes no arguments (configure via CADENCE_* env vars)",
                  file=sys.stderr)
            return 2
        from cadence.runtime.__main__ import main as _run
        _run()  # blocks serving until stopped
        return 0
    if cmd == "devbox":
        from cadence.adapters.devbox import main as _devbox
        return _devbox(rest)
    if cmd == "onboarding":
        from cadence.onboarding.__main__ import main as _onboarding
        return _onboarding(rest)

    print(f"cadence: unknown command {cmd!r}\n\n{_USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
