"""Tests for the /loop command."""

from __future__ import annotations

import os
import signal
import threading
import types

import pytest

from swival import agent
from swival.input_commands import INPUT_COMMANDS
from swival.input_dispatch import InputContext, StepResult, parse_input_line


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_loop_in_input_commands(self):
        assert "/loop" in INPUT_COMMANDS

    def test_loop_kind(self):
        assert INPUT_COMMANDS["/loop"].kind == "agent_turn"

    def test_loop_modes(self):
        assert INPUT_COMMANDS["/loop"].modes == ("repl", "oneshot")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


class TestParseLoopArgs:
    def test_interval_then_prompt(self):
        assert agent._parse_loop_args("5m foo") == (300, "foo")

    def test_seconds(self):
        assert agent._parse_loop_args("30s /bar") == (30, "/bar")

    def test_compound(self):
        assert agent._parse_loop_args("1h30m foo") == (3600 + 1800, "foo")

    def test_compound_with_seconds(self):
        assert agent._parse_loop_args("1h2m3s foo") == (3723, "foo")

    def test_default_when_no_interval(self):
        seconds, prompt = agent._parse_loop_args("foo bar")
        assert seconds == 600
        assert prompt == "foo bar"

    def test_bare_integer_is_prompt_not_interval(self):
        seconds, prompt = agent._parse_loop_args("10 foo")
        assert seconds == 600
        assert prompt == "10 foo"

    def test_slash_prompt_with_default(self):
        seconds, prompt = agent._parse_loop_args("/audit src")
        assert seconds == 600
        assert prompt == "/audit src"

    def test_empty(self):
        with pytest.raises(ValueError, match="requires a prompt"):
            agent._parse_loop_args("")

    def test_whitespace_only(self):
        with pytest.raises(ValueError, match="requires a prompt"):
            agent._parse_loop_args("   ")

    def test_interval_only_no_prompt(self):
        with pytest.raises(ValueError, match="no prompt"):
            agent._parse_loop_args("5m")

    def test_zero_below_floor(self):
        with pytest.raises(ValueError, match="floor"):
            agent._parse_loop_args("0s foo")

    def test_three_seconds_below_floor(self):
        with pytest.raises(ValueError, match="floor"):
            agent._parse_loop_args("3s foo")

    def test_above_ceiling(self):
        with pytest.raises(ValueError, match="ceiling"):
            agent._parse_loop_args("25h foo")

    def test_repeated_component_falls_through_to_prompt(self):
        seconds, prompt = agent._parse_loop_args("1h2h foo")
        assert seconds == 600
        assert prompt == "1h2h foo"

    def test_out_of_order_components_falls_through(self):
        seconds, prompt = agent._parse_loop_args("30m1h foo")
        assert seconds == 600
        assert prompt == "30m1h foo"


class TestFormatDuration:
    def test_seconds_only(self):
        assert agent._format_loop_duration(30) == "30s"

    def test_minutes(self):
        assert agent._format_loop_duration(300) == "5m"

    def test_compound(self):
        assert agent._format_loop_duration(3723) == "1h2m3s"

    def test_zero(self):
        assert agent._format_loop_duration(0) == "0s"


# ---------------------------------------------------------------------------
# Loop body
# ---------------------------------------------------------------------------


def _make_ctx() -> InputContext:
    """Minimal InputContext for loop dispatch."""
    from swival.thinking import ThinkingState
    from swival.todo import TodoState

    return InputContext(
        messages=[],
        tools=[],
        base_dir="/tmp",
        turn_state={"max_turns": 10, "turns_used": 0},
        thinking_state=ThinkingState(),
        todo_state=TodoState(),
        snapshot_state=None,
        file_tracker=None,
        no_history=True,
        continue_here=False,
        verbose=False,
        loop_kwargs={
            "model_id": "test",
            "api_base": "http://test",
            "context_length": 128000,
            "files_mode": "some",
            "compaction_state": None,
            "command_policy": types.SimpleNamespace(mode="allowlist"),
            "top_p": None,
            "seed": None,
            "llm_kwargs": {},
        },
    )


class _Recorder:
    """Patch execute_input + interruptible sleep so loop tests run quickly.

    The loop exits when ``len(self.calls) >= target_iterations`` via the
    fake sleep returning False — no reliance on KeyboardInterrupt to
    terminate the test.
    """

    def __init__(self, monkeypatch, results, *, target_iterations=None):
        self.calls: list[str] = []
        self.results = list(results)
        self.sleep_count = 0
        self._results_iter = iter(self.results)
        self.target = (
            target_iterations if target_iterations is not None else len(self.results)
        )

        def fake_execute_input(parsed, ctx, *, mode="repl"):
            self.calls.append(parsed.raw if parsed.raw else "")
            try:
                return next(self._results_iter)
            except StopIteration as e:
                raise RuntimeError(
                    f"recorder exhausted after {len(self.calls)} calls"
                ) from e

        def fake_interruptible_sleep(seconds, stop_event):
            self.sleep_count += 1
            if stop_event.is_set():
                return False
            if len(self.calls) >= self.target:
                return False
            return True

        monkeypatch.setattr(agent, "execute_input", fake_execute_input)
        monkeypatch.setattr(
            agent, "_loop_interruptible_sleep", fake_interruptible_sleep
        )


class TestLoopDispatch:
    def test_bad_args_returns_error(self):
        result = agent._execute_loop("", _make_ctx(), mode="repl")
        assert result.is_error
        assert result.kind == "info"
        assert "requires a prompt" in (result.text or "")

    def test_interval_only_returns_error(self):
        result = agent._execute_loop("5m", _make_ctx(), mode="repl")
        assert result.is_error
        assert "no prompt" in (result.text or "")

    def test_floor_violation_returns_error(self):
        result = agent._execute_loop("1s foo", _make_ctx(), mode="repl")
        assert result.is_error
        assert "floor" in (result.text or "")

    def test_repl_renders_iteration_answers(self, monkeypatch):
        rec = _Recorder(
            monkeypatch,
            [
                StepResult(kind="agent_turn", text="answer 1"),
                StepResult(kind="agent_turn", text="answer 2"),
                StepResult(kind="agent_turn", text="answer 3"),
            ],
        )

        rendered: list[str] = []
        monkeypatch.setattr(agent.fmt, "repl_answer", lambda t: rendered.append(t))

        ctx = _make_ctx()
        result = agent._execute_loop("5s foo", ctx, mode="repl")

        assert result.kind == "state_change"
        assert "loop stopped" in (result.text or "")
        assert rec.calls == ["foo", "foo", "foo"]
        assert rendered == ["answer 1", "answer 2", "answer 3"]

    def test_double_tap_interrupt_exits_loop(self, monkeypatch):
        """Two interrupted steps within the double-tap window exit the loop."""
        rec = _Recorder(
            monkeypatch,
            [
                StepResult(kind="agent_turn", text="x", interrupted=True),
                StepResult(kind="agent_turn", text="y", interrupted=True),
                StepResult(kind="agent_turn", text="never"),
            ],
            target_iterations=10,  # ample budget; double-tap should exit first
        )
        # Freeze monotonic at 0 so both interrupts are within the window.
        monkeypatch.setattr(agent.time, "monotonic", lambda: 0.0)
        monkeypatch.setattr(agent.fmt, "repl_answer", lambda t: None)

        ctx = _make_ctx()
        result = agent._execute_loop("5s foo", ctx, mode="repl")

        assert result.kind == "state_change"
        # Loop should exit after the 2nd interrupted iteration.
        assert len(rec.calls) == 2

    def test_oneshot_streams_stdout_and_final_text_none(self, monkeypatch, capsys):
        rec = _Recorder(
            monkeypatch,
            [
                StepResult(kind="agent_turn", text="iter A"),
                StepResult(kind="agent_turn", text="iter B"),
                StepResult(kind="agent_turn", text="iter C"),
            ],
        )

        ctx = _make_ctx()
        result = agent._execute_loop("5s probe", ctx, mode="oneshot")

        assert result.kind == "state_change"
        assert result.text is None

        captured = capsys.readouterr()
        # Each iteration prints its text plus a blank-line separator.
        assert "iter A" in captured.out
        assert "iter B" in captured.out
        assert "iter C" in captured.out
        # The final stop summary should be on stderr, not stdout.
        assert "loop stopped" not in captured.out

        assert rec.calls == ["probe", "probe", "probe"]

    def test_single_interrupt_skips_iteration(self, monkeypatch):
        """A single interrupted step skips that iteration; loop continues."""
        rec = _Recorder(
            monkeypatch,
            [
                StepResult(kind="agent_turn", text="ok"),
                StepResult(kind="agent_turn", text=None, interrupted=True),
                StepResult(kind="agent_turn", text="ok 2"),
            ],
        )

        # Advance monotonic by 10s per call so consecutive interrupts are
        # well outside the double-tap window.
        clock = {"t": 0.0}

        def fake_monotonic():
            clock["t"] += 10.0
            return clock["t"]

        monkeypatch.setattr(agent.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(agent.fmt, "repl_answer", lambda t: None)

        ctx = _make_ctx()
        result = agent._execute_loop("5s foo", ctx, mode="repl")

        assert result.kind == "state_change"
        # All three iterations dispatched; the middle one is a single-tap
        # interrupt which only skips, not exits.
        assert len(rec.calls) == 3
        _ = rec

    def test_sigterm_exits_between_iterations(self, monkeypatch):
        """SIGTERM in one-shot mode should stop the loop cleanly."""

        responses = [StepResult(kind="agent_turn", text=f"iter {i}") for i in range(5)]
        call_log: list[str] = []

        def fake_execute_input(parsed, ctx, *, mode="repl"):
            call_log.append(parsed.raw)
            if len(call_log) == 2:
                os.kill(os.getpid(), signal.SIGTERM)
            return responses[len(call_log) - 1]

        monkeypatch.setattr(agent, "execute_input", fake_execute_input)
        monkeypatch.setattr(
            agent,
            "_loop_interruptible_sleep",
            lambda seconds, stop_event: not stop_event.is_set(),
        )

        prior = signal.getsignal(signal.SIGTERM)
        try:
            ctx = _make_ctx()
            result = agent._execute_loop("5s foo", ctx, mode="oneshot")
        finally:
            signal.signal(signal.SIGTERM, prior)

        assert result.kind == "state_change"
        assert result.text is None
        assert len(call_log) == 2
        assert signal.getsignal(signal.SIGTERM) is prior

    def test_turns_accumulate_across_iterations(self, monkeypatch):
        """Per-iteration turn counts should sum into ctx.turn_state."""
        per_iter_turns = iter([3, 5, 2])
        responses = [StepResult(kind="agent_turn", text=f"r{i}") for i in range(3)]
        ctx = _make_ctx()

        def fake_execute_input(parsed, ctx_arg, *, mode="repl"):
            # Simulate what run_agent_loop does: overwrite turns_used with
            # this iteration's count.
            ctx_arg.turn_state["turns_used"] = next(per_iter_turns)
            return responses.pop(0)

        monkeypatch.setattr(agent, "execute_input", fake_execute_input)
        monkeypatch.setattr(
            agent,
            "_loop_interruptible_sleep",
            lambda seconds, stop_flag: bool(responses),  # exit when empty
        )
        monkeypatch.setattr(agent.fmt, "repl_answer", lambda t: None)

        agent._execute_loop("5s foo", ctx, mode="repl")

        # 3 + 5 + 2 = 10
        assert ctx.turn_state["turns_used"] == 10

    def test_turn_offset_advances_with_report(self, monkeypatch):
        """The report's max_turn_seen should propagate to loop_kwargs."""
        responses = [StepResult(kind="agent_turn", text="x") for _ in range(2)]
        ctx = _make_ctx()
        max_turn_seen_seq = iter([4, 9])
        report = types.SimpleNamespace()

        def fake_execute_input(parsed, ctx_arg, *, mode="repl"):
            report.max_turn_seen = next(max_turn_seen_seq)
            return responses.pop(0)

        ctx.loop_kwargs["report"] = report
        monkeypatch.setattr(agent, "execute_input", fake_execute_input)
        monkeypatch.setattr(
            agent,
            "_loop_interruptible_sleep",
            lambda seconds, stop_flag: bool(responses),
        )
        monkeypatch.setattr(agent.fmt, "repl_answer", lambda t: None)

        agent._execute_loop("5s foo", ctx, mode="repl")
        assert ctx.loop_kwargs["turn_offset"] == 9

    def test_oneshot_error_does_not_appear_on_stdout(self, monkeypatch, capsys):
        """An is_error iteration result must not be streamed to stdout."""
        rec = _Recorder(
            monkeypatch,
            [
                StepResult(
                    kind="agent_turn",
                    text="error: provider exploded",
                    is_error=True,
                ),
            ],
        )

        ctx = _make_ctx()
        agent._execute_loop("5s probe", ctx, mode="oneshot")

        captured = capsys.readouterr()
        assert "error: provider exploded" not in captured.out
        assert "error: provider exploded" in captured.err
        _ = rec

    def test_step_stop_exits_loop(self, monkeypatch):
        """A loop-body step with stop=True (e.g. /exit) must end the loop."""
        rec = _Recorder(
            monkeypatch,
            [
                StepResult(kind="agent_turn", text="first"),
                StepResult(kind="flow_control", text=None, stop=True),
                StepResult(kind="agent_turn", text="never"),
            ],
            target_iterations=10,
        )
        monkeypatch.setattr(agent.fmt, "repl_answer", lambda t: None)

        ctx = _make_ctx()
        result = agent._execute_loop("5s foo", ctx, mode="repl")
        assert result.kind == "state_change"
        assert len(rec.calls) == 2

    def test_second_sigterm_raises_system_exit(self, monkeypatch):
        """A second SIGTERM during an iteration should raise SystemExit(143)."""
        # First call: send SIGTERM #1 (sets stop_flag), then second SIGTERM
        # within the same iteration to trigger the force-exit path.
        call_log: list[str] = []

        def fake_execute_input(parsed, ctx_arg, *, mode="oneshot"):
            call_log.append(parsed.raw)
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
            return StepResult(kind="agent_turn", text="never seen")

        monkeypatch.setattr(agent, "execute_input", fake_execute_input)
        monkeypatch.setattr(
            agent,
            "_loop_interruptible_sleep",
            lambda seconds, stop_event: not stop_event.is_set(),
        )

        prior = signal.getsignal(signal.SIGTERM)
        try:
            ctx = _make_ctx()
            with pytest.raises(SystemExit) as exc_info:
                agent._execute_loop("5s foo", ctx, mode="oneshot")
            assert exc_info.value.code == 143
        finally:
            signal.signal(signal.SIGTERM, prior)
        assert call_log == ["foo"]

    def test_repl_does_not_install_sigterm_handler(self, monkeypatch):
        """REPL mode must leave the global SIGTERM handler untouched."""

        rec = _Recorder(
            monkeypatch,
            [StepResult(kind="agent_turn", text="x")],
        )
        monkeypatch.setattr(agent.fmt, "repl_answer", lambda t: None)

        sentinel = lambda *a, **k: None  # noqa: E731
        prior = signal.signal(signal.SIGTERM, sentinel)
        try:
            ctx = _make_ctx()
            agent._execute_loop("5s foo", ctx, mode="repl")
            assert signal.getsignal(signal.SIGTERM) is sentinel
        finally:
            signal.signal(signal.SIGTERM, prior)
        _ = rec


# ---------------------------------------------------------------------------
# StepResult / script-runner interaction
# ---------------------------------------------------------------------------


class TestStepResultInterrupted:
    def test_default_false(self):
        assert StepResult(kind="agent_turn").interrupted is False

    def test_set_true(self):
        assert StepResult(kind="agent_turn", interrupted=True).interrupted is True


class TestScriptRunnerInterruptHandling:
    """run_input_script aborts on any agent_turn with text=None (interrupt
    or provider error), but /loop's state_change+text=None does not trip
    that guard."""

    def test_interrupted_agent_turn_stops_script(self, monkeypatch):
        steps = iter(
            [
                StepResult(kind="agent_turn", text=None, interrupted=True),
                StepResult(kind="agent_turn", text="never"),
            ]
        )
        dispatched: list[str] = []

        def fake_execute_input(parsed, ctx, *, mode="oneshot"):
            dispatched.append(parsed.raw)
            return next(steps)

        monkeypatch.setattr(agent, "execute_input", fake_execute_input)

        ctx = _make_ctx()
        agent.run_input_script("foo\nbar", ctx, mode="oneshot")
        assert dispatched == ["foo"]

    def test_failed_agent_turn_stops_script(self, monkeypatch):
        """A non-interrupted agent_turn with text=None (e.g., provider
        error caught by _invoke_agent_turn) must still stop the script."""
        steps = iter(
            [
                StepResult(kind="agent_turn", text=None, interrupted=False),
                StepResult(kind="agent_turn", text="never"),
            ]
        )
        dispatched: list[str] = []

        def fake_execute_input(parsed, ctx, *, mode="oneshot"):
            dispatched.append(parsed.raw)
            return next(steps)

        monkeypatch.setattr(agent, "execute_input", fake_execute_input)

        ctx = _make_ctx()
        agent.run_input_script("foo\nbar", ctx, mode="oneshot")
        assert dispatched == ["foo"]

    def test_state_change_text_none_continues_script(self, monkeypatch):
        """/loop returns kind=state_change + text=None in one-shot mode.
        That should not abort the enclosing script."""
        steps = iter(
            [
                StepResult(kind="state_change", text=None),
                StepResult(kind="agent_turn", text="last"),
            ]
        )
        dispatched: list[str] = []

        def fake_execute_input(parsed, ctx, *, mode="oneshot"):
            dispatched.append(parsed.raw)
            return next(steps)

        monkeypatch.setattr(agent, "execute_input", fake_execute_input)

        ctx = _make_ctx()
        result = agent.run_input_script("/loop 5s probe\nbar", ctx, mode="oneshot")
        assert dispatched == ["/loop 5s probe", "bar"]
        assert result.text == "last"


# ---------------------------------------------------------------------------
# Interruptible sleep
# ---------------------------------------------------------------------------


class TestInterruptibleSleep:
    def test_returns_true_after_full_sleep(self):
        event = threading.Event()
        assert agent._loop_interruptible_sleep(0.01, event) is True

    def test_returns_false_when_event_already_set(self):
        event = threading.Event()
        event.set()
        assert agent._loop_interruptible_sleep(60, event) is False

    def test_returns_false_when_event_set_during_wait(self):
        event = threading.Event()
        timer = threading.Timer(0.01, event.set)
        timer.start()
        try:
            assert agent._loop_interruptible_sleep(60, event) is False
        finally:
            timer.cancel()

    def test_returns_false_on_keyboard_interrupt(self, monkeypatch):
        event = threading.Event()

        def raising_wait(timeout=None):
            raise KeyboardInterrupt()

        monkeypatch.setattr(event, "wait", raising_wait)
        assert agent._loop_interruptible_sleep(60, event) is False


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------


class TestLoopDispatchEntry:
    def test_loop_routes_through_execute_input(self, monkeypatch):
        called = {}

        def fake_execute_loop(cmd_arg, ctx, *, mode):
            called["cmd_arg"] = cmd_arg
            called["mode"] = mode
            return StepResult(kind="state_change", text="ok")

        monkeypatch.setattr(agent, "_execute_loop", fake_execute_loop)

        ctx = _make_ctx()
        parsed = parse_input_line("/loop 5m hello")
        result = agent.execute_input(parsed, ctx, mode="repl")

        assert called["cmd_arg"] == "5m hello"
        assert called["mode"] == "repl"
        assert result.text == "ok"
