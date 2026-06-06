"""Tests for the shell-command spinner: label rendering and gating."""

import contextlib
import json
import types
from io import StringIO

import pytest

from swival import agent, fmt
from swival.agent import _command_label, handle_tool_call
from swival.thinking import ThinkingState

from tests.conftest import styled_console


def _make_tool_call(name, arguments, call_id="call_1"):
    tc = types.SimpleNamespace()
    tc.id = call_id
    tc.function = types.SimpleNamespace()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


class TestCommandLabel:
    def test_argv_list_is_shlex_quoted(self):
        assert _command_label(["echo", "a b"]) == "echo 'a b'"

    def test_shell_string_collapses_whitespace(self):
        assert _command_label("ls   -l\n  /tmp") == "ls -l /tmp"

    def test_long_command_truncated_with_ellipsis(self):
        label = _command_label("x" * 500)
        assert len(label) == 140
        assert label.endswith("…")

    def test_none_falls_back(self):
        assert _command_label(None) == "command"

    def test_empty_falls_back(self):
        assert _command_label("") == "command"
        assert _command_label([]) == "command"

    def test_non_str_non_list_falls_back(self):
        assert _command_label(42) == "command"


class _Spy:
    """Records whether the spinner context manager was entered."""

    def __init__(self):
        self.calls = []
        self.timeouts = []

    def __call__(self, label, timeout=None):
        self.calls.append(label)
        self.timeouts.append(timeout)
        return contextlib.nullcontext()


def _run(monkeypatch, tmp_path, name, args, **kwargs):
    spy = _Spy()
    monkeypatch.setattr(fmt, "command_spinner", spy)
    monkeypatch.setattr(agent, "dispatch", lambda *a, **k: "ok")
    handle_tool_call(
        _make_tool_call(name, json.dumps(args)),
        str(tmp_path),
        ThinkingState(),
        kwargs.pop("verbose", True),
        shell_allowed=True,
        commands_unrestricted=True,
        **kwargs,
    )
    return spy


class TestSpinnerGating:
    def test_called_for_run_command(self, monkeypatch, tmp_path):
        spy = _run(monkeypatch, tmp_path, "run_command", {"command": ["ls"]})
        assert spy.calls == ["ls"]

    def test_timeout_passed_through(self, monkeypatch, tmp_path):
        spy = _run(
            monkeypatch,
            tmp_path,
            "run_command",
            {"command": ["ls"], "timeout": 90},
        )
        assert spy.timeouts == [90]

    def test_timeout_defaults_to_30(self, monkeypatch, tmp_path):
        spy = _run(monkeypatch, tmp_path, "run_command", {"command": ["ls"]})
        assert spy.timeouts == [30]

    def test_timeout_clamped_to_max(self, monkeypatch, tmp_path):
        from swival.tools import MAX_TIMEOUT

        spy = _run(
            monkeypatch,
            tmp_path,
            "run_command",
            {"command": ["ls"], "timeout": 99999},
        )
        assert spy.timeouts == [MAX_TIMEOUT]

    def test_called_for_run_shell_command(self, monkeypatch, tmp_path):
        spy = _run(monkeypatch, tmp_path, "run_shell_command", {"command": "ls -l"})
        assert spy.calls == ["ls -l"]

    def test_not_called_when_not_verbose(self, monkeypatch, tmp_path):
        spy = _run(
            monkeypatch, tmp_path, "run_command", {"command": ["ls"]}, verbose=False
        )
        assert spy.calls == []

    def test_not_called_for_subagent(self, monkeypatch, tmp_path):
        spy = _run(
            monkeypatch,
            tmp_path,
            "run_command",
            {"command": ["ls"]},
            is_subagent=True,
        )
        assert spy.calls == []

    def test_not_called_for_background(self, monkeypatch, tmp_path):
        spy = _run(
            monkeypatch,
            tmp_path,
            "run_command",
            {"command": ["ls"], "background": True},
        )
        assert spy.calls == []

    def test_not_called_for_non_command_tool(self, monkeypatch, tmp_path):
        spy = _run(monkeypatch, tmp_path, "read_file", {"file_path": "x.txt"})
        assert spy.calls == []


class TestSuspendLive:
    @pytest.fixture
    def tty_console(self, monkeypatch):
        console = styled_console(StringIO())
        monkeypatch.setattr(fmt, "_console", console)
        return console

    def test_noop_without_active_display(self):
        with fmt.suspend_live():
            pass

    def test_suspend_stops_and_resumes_display(self, tty_console):
        with fmt.command_spinner("sleep 1", timeout=30):
            assert fmt._active_live_suspend is not None
            with fmt.suspend_live():
                assert tty_console._live_stack == []
            assert len(tty_console._live_stack) == 1
        assert fmt._active_live_suspend is None
        assert tty_console._live_stack == []

    def test_resume_after_dismiss_is_noop(self, tty_console):
        with fmt.command_spinner("sleep 1", timeout=30) as dismiss:
            suspend = fmt._active_live_suspend
            resume = suspend()
            dismiss()
            resume()
            assert tty_console._live_stack == []

    def test_prompt_approval_suspends_display(self, tty_console, monkeypatch):
        from swival import command_policy

        seen = {}

        def fake_input():
            seen["live_stack"] = list(tty_console._live_stack)
            return "y"

        monkeypatch.setattr("builtins.input", fake_input)
        with fmt.command_spinner("sleep 1", timeout=30):
            answer = command_policy.prompt_approval("sleep")
            assert answer == "allow"
            assert seen["live_stack"] == []
            assert len(tty_console._live_stack) == 1


class TestResultTransparency:
    def test_result_unchanged_with_spinner(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            fmt,
            "command_spinner",
            lambda label, timeout=None: contextlib.nullcontext(),
        )
        monkeypatch.setattr(agent, "dispatch", lambda *a, **k: "the output")
        tool_msg, meta = handle_tool_call(
            _make_tool_call("run_command", json.dumps({"command": ["ls"]})),
            str(tmp_path),
            ThinkingState(),
            True,
            shell_allowed=True,
            commands_unrestricted=True,
        )
        assert tool_msg["content"] == "the output"
        assert meta["succeeded"] is True
        assert meta["name"] == "run_command"
