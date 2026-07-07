"""Interactive onboarding CLI — ``python -m cadence.onboarding``.

Lets the user pick an LLM auth path and completes it:

* ``openai_api``     — paste an API key (the fully-supported default).
* ``chatgpt_oauth``  — browser-based ChatGPT-OAuth login (opt-in, **GRAY ZONE** — see
  :data:`cadence.onboarding.chatgpt_oauth.GRAY_ZONE_WARNING`, printed below with a
  required confirmation before anything runs), or ``--device`` for the headless
  device-code fallback.

Never prints a token, refresh token, id_token, or API key.
"""

from __future__ import annotations

import argparse
import sys

from cadence.onboarding.api_key import store_api_key
from cadence.onboarding.chatgpt_oauth import (
    GRAY_ZONE_WARNING,
    ChatGPTOAuthOnboarding,
    poll_device_login,
    start_device_login,
)


def _prompt_provider() -> str:
    print("Choose an LLM auth provider:")
    print("  1) openai_api    — API key (recommended, fully supported)")
    print("  2) chatgpt_oauth — ChatGPT-account OAuth login (opt-in, gray-zone)")
    choice = input("> ").strip()
    return "chatgpt_oauth" if choice in ("2", "chatgpt_oauth") else "openai_api"


def _run_api_key() -> None:
    api_key = input("Paste your OpenAI API key (sk-...): ").strip()
    store_api_key(api_key)
    print("Stored. The 'openai_api' provider is ready.")


def _run_chatgpt_oauth(*, device: bool, assume_yes: bool) -> None:
    print(GRAY_ZONE_WARNING)
    if not assume_yes:
        confirm = input("Continue with ChatGPT-OAuth? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            return
    if device:
        session = start_device_login()
        print(f"Go to {session.verification_uri} and enter code: {session.user_code}")
        token_set = poll_device_login(session)
    else:
        token_set = ChatGPTOAuthOnboarding().login()
    print(f"Signed in (account {token_set.account_id}). The 'chatgpt_oauth' provider is ready.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cadence.onboarding", description="Cadence LLM onboarding login flow."
    )
    parser.add_argument(
        "--provider",
        choices=["openai_api", "chatgpt_oauth"],
        default=None,
        help="Skip the interactive prompt and use this provider.",
    )
    parser.add_argument(
        "--device",
        action="store_true",
        help="Use the device-code flow for chatgpt_oauth (headless; no local browser/loopback).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive gray-zone confirmation (the warning is still printed).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provider = args.provider or _prompt_provider()
    if provider == "openai_api":
        _run_api_key()
    else:
        _run_chatgpt_oauth(device=args.device, assume_yes=args.yes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
