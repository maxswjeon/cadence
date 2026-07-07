"""Tests for the interactive onboarding CLI (``python -m cadence.onboarding``).

Exercises the gray-zone warning + confirmation gate directly (no real network, no real
browser, no vault I/O): declining must never construct/invoke
``ChatGPTOAuthOnboarding.login``, and ``--yes``/``assume_yes=True`` must skip the
prompt but still print the warning and proceed.
"""

from __future__ import annotations

import pytest

from cadence.onboarding import __main__ as cli


class _ExplodingOnboarding:
    """Stands in for ChatGPTOAuthOnboarding — merely constructing it means the CLI tried
    to actually run the OAuth flow, which the declined-confirmation tests must never do.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("ChatGPTOAuthOnboarding should not be constructed")


class _FakeTokenSet:
    account_id = "acct-fake"


class _FakeOnboarding:
    """Records that login() was actually invoked, without touching the network."""

    instances: list[_FakeOnboarding] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.login_called = False
        _FakeOnboarding.instances.append(self)

    def login(self, **kwargs: object) -> _FakeTokenSet:
        self.login_called = True
        return _FakeTokenSet()


@pytest.fixture(autouse=True)
def _reset_fake_onboarding():
    _FakeOnboarding.instances.clear()
    yield
    _FakeOnboarding.instances.clear()


def test_declined_confirmation_never_calls_login(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ChatGPTOAuthOnboarding", _ExplodingOnboarding)
    monkeypatch.setattr("builtins.input", lambda *_a: "n")

    cli._run_chatgpt_oauth(device=False, assume_yes=False)  # must not raise

    out = capsys.readouterr().out
    assert "GRAY ZONE" in out
    assert "Aborted." in out


@pytest.mark.parametrize("answer", ["", "no", "nope", "maybe"])
def test_declined_confirmation_rejects_anything_but_yes(monkeypatch, answer: str) -> None:
    monkeypatch.setattr(cli, "ChatGPTOAuthOnboarding", _ExplodingOnboarding)
    monkeypatch.setattr("builtins.input", lambda *_a: answer)

    cli._run_chatgpt_oauth(device=False, assume_yes=False)  # must not construct/raise


def test_assume_yes_skips_prompt_and_proceeds(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ChatGPTOAuthOnboarding", _FakeOnboarding)

    def _no_input(*_a: object, **_k: object) -> str:
        raise AssertionError("input() should not be called when assume_yes=True")

    monkeypatch.setattr("builtins.input", _no_input)

    cli._run_chatgpt_oauth(device=False, assume_yes=True)

    assert len(_FakeOnboarding.instances) == 1
    assert _FakeOnboarding.instances[0].login_called is True
    out = capsys.readouterr().out
    assert "GRAY ZONE" in out
    assert "Signed in (account acct-fake)" in out


def test_explicit_yes_confirmation_also_proceeds(monkeypatch) -> None:
    monkeypatch.setattr(cli, "ChatGPTOAuthOnboarding", _FakeOnboarding)
    monkeypatch.setattr("builtins.input", lambda *_a: "y")

    cli._run_chatgpt_oauth(device=False, assume_yes=False)

    assert len(_FakeOnboarding.instances) == 1
    assert _FakeOnboarding.instances[0].login_called is True
