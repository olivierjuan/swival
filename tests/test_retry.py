"""Tests for transient-error retry logic in call_llm / _completion_with_retry."""

import types
from unittest.mock import patch, MagicMock

import pytest

from swival.agent import (
    call_llm,
    _completion_with_retry,
    _is_transient,
    AgentError,
    ContextOverflowError,
)


def _make_response(content="hello"):
    msg = types.SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = types.SimpleNamespace(message=msg, finish_reason="stop")
    return types.SimpleNamespace(choices=[choice])


class TestIsTransient:
    def test_api_connection_error(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Connection reset by peer", llm_provider="openai", model="x"
        )
        assert _is_transient(exc) is True

    def test_timeout(self):
        import litellm

        exc = litellm.Timeout(message="timed out", model="x", llm_provider="openai")
        assert _is_transient(exc) is True

    def test_rate_limit(self):
        import litellm

        exc = litellm.RateLimitError(message="429", llm_provider="openai", model="x")
        assert _is_transient(exc) is True

    def test_internal_server_error(self):
        import litellm

        exc = litellm.InternalServerError(
            message="500", llm_provider="openai", model="x"
        )
        assert _is_transient(exc) is True

    def test_service_unavailable(self):
        import litellm

        exc = litellm.ServiceUnavailableError(
            message="upstream connect error", llm_provider="openai", model="x"
        )
        assert _is_transient(exc) is True

    def test_bad_request_not_transient(self):
        import litellm

        exc = litellm.BadRequestError(message="bad", llm_provider="openai", model="x")
        assert _is_transient(exc) is False

    def test_auth_error_not_transient(self):
        import litellm

        exc = litellm.AuthenticationError(
            message="unauthorized", llm_provider="openai", model="x"
        )
        assert _is_transient(exc) is False

    def test_generic_api_error_500(self):
        import litellm

        exc = litellm.APIError(
            status_code=500,
            message="internal server error",
            llm_provider="openai",
            model="x",
        )
        assert _is_transient(exc) is True

    def test_generic_api_error_400(self):
        import litellm

        exc = litellm.APIError(
            status_code=400, message="bad request", llm_provider="openai", model="x"
        )
        assert _is_transient(exc) is False

    def test_string_pattern_connection_reset(self):
        exc = OSError("[Errno 54] Connection reset by peer")
        assert _is_transient(exc) is True

    def test_unrelated_error_not_transient(self):
        exc = ValueError("something unrelated")
        assert _is_transient(exc) is False

    def test_sso_token_expired_not_transient(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Error when retrieving token from sso: "
            "Token has expired and refresh failed",
            llm_provider="bedrock",
            model="x",
        )
        assert _is_transient(exc) is False

    def test_sso_token_missing_not_transient(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Error loading SSO Token: Token for fastly does not exist",
            llm_provider="bedrock",
            model="x",
        )
        assert _is_transient(exc) is False

    def test_sso_retrieval_network_error_is_transient(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Error when retrieving token from sso: Connection reset by peer",
            llm_provider="bedrock",
            model="x",
        )
        assert _is_transient(exc) is True


class TestCompletionWithRetry:
    def test_succeeds_first_try(self):
        resp = _make_response()
        with patch("litellm.completion", return_value=resp):
            result, retries = _completion_with_retry(
                {"model": "x", "messages": []}, max_retries=5, verbose=False
            )
        assert result is resp
        assert retries == 0

    def test_succeeds_after_transient_errors(self):
        import litellm

        resp = _make_response()
        exc = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        mock = MagicMock(side_effect=[exc, exc, resp])
        with patch("litellm.completion", mock), patch("time.sleep"):
            result, retries = _completion_with_retry(
                {"model": "x", "messages": []}, max_retries=5, verbose=False
            )
        assert result is resp
        assert retries == 2
        assert mock.call_count == 3

    def test_exhausts_retries(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        mock = MagicMock(side_effect=[exc] * 3)
        with patch("litellm.completion", mock), patch("time.sleep"):
            with pytest.raises(litellm.APIConnectionError):
                _completion_with_retry(
                    {"model": "x", "messages": []}, max_retries=3, verbose=False
                )
        assert mock.call_count == 3

    def test_non_transient_not_retried(self):
        import litellm

        exc = litellm.BadRequestError(
            message="bad input", llm_provider="openai", model="x"
        )
        mock = MagicMock(side_effect=exc)
        with patch("litellm.completion", mock):
            with pytest.raises(litellm.BadRequestError):
                _completion_with_retry(
                    {"model": "x", "messages": []}, max_retries=5, verbose=False
                )
        assert mock.call_count == 1

    def test_context_overflow_propagates(self):
        import litellm

        mock = MagicMock(
            side_effect=litellm.ContextWindowExceededError(
                message="too long", llm_provider="openai", model="x"
            )
        )
        with patch("litellm.completion", mock):
            with pytest.raises(ContextOverflowError):
                _completion_with_retry(
                    {"model": "x", "messages": []}, max_retries=5, verbose=False
                )

    def test_sso_token_expired_no_retry_no_sleep(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Error when retrieving token from sso: "
            "Token has expired and refresh failed",
            llm_provider="bedrock",
            model="x",
        )
        with patch("litellm.completion", side_effect=exc):
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(litellm.APIConnectionError) as exc_info:
                    _completion_with_retry(
                        {"model": "x", "messages": []},
                        max_retries=5,
                        verbose=True,
                    )
                assert exc_info.value._provider_retries == 0
                mock_sleep.assert_not_called()


class TestCallLlmRetry:
    def test_returns_5_tuple(self):
        resp = _make_response()
        with patch("litellm.completion", return_value=resp):
            result = call_llm(
                "http://localhost:8080/v1",
                "my-model",
                [{"role": "user", "content": "hi"}],
                100,
                0.5,
                1.0,
                None,
                None,
                False,
                provider="generic",
                api_key="test",
            )
        assert len(result) == 5
        msg, finish_reason, cmd_activity, provider_retries, cache_stats = result
        assert msg.content == "hello"
        assert finish_reason == "stop"
        assert cmd_activity == []
        assert provider_retries == 0
        assert cache_stats == (0, 0)

    def test_provider_retries_reported(self):
        import litellm

        resp = _make_response()
        exc = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        mock = MagicMock(side_effect=[exc, resp])
        with patch("litellm.completion", mock), patch("time.sleep"):
            result = call_llm(
                "http://localhost:8080/v1",
                "my-model",
                [{"role": "user", "content": "hi"}],
                100,
                0.5,
                1.0,
                None,
                None,
                False,
                provider="generic",
                api_key="test",
            )
        assert result[3] == 1  # provider_retries

    def test_retries_1_no_retry(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        with patch("litellm.completion", side_effect=exc):
            with pytest.raises(AgentError, match="LLM call failed"):
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                    max_retries=1,
                )

    def test_context_overflow_not_wrapped(self):
        import litellm

        mock = MagicMock(
            side_effect=litellm.ContextWindowExceededError(
                message="too long", llm_provider="openai", model="x"
            )
        )
        with patch("litellm.completion", mock):
            with pytest.raises(ContextOverflowError):
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                )

    def test_command_provider_5_tuple(self):
        result = call_llm(
            None,
            "echo hello",
            [{"role": "user", "content": "hi"}],
            100,
            0.5,
            1.0,
            None,
            None,
            False,
            provider="command",
        )
        assert len(result) == 5
        assert result[4] == (0, 0)  # no cache stats for command provider
        assert result[3] == 0  # provider_retries

    def test_sanitization_retry_also_retries_transient(self):
        """Empty-assistant sanitization triggers a second _completion_with_retry
        call which should also handle transient errors."""
        import litellm

        bad_req = litellm.BadRequestError(
            message="must have either content or tool_calls",
            llm_provider="openai",
            model="x",
        )
        transient = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        resp = _make_response()
        # First call: BadRequestError (empty assistant)
        # Second call (after sanitize): transient
        # Third call: success
        mock = MagicMock(side_effect=[bad_req, transient, resp])

        # Need an assistant message with no content to trigger sanitization
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant"},
            {"role": "user", "content": "continue"},
        ]
        with patch("litellm.completion", mock), patch("time.sleep"):
            result = call_llm(
                "http://localhost:8080/v1",
                "my-model",
                messages,
                100,
                0.5,
                1.0,
                None,
                None,
                False,
                provider="generic",
                api_key="test",
                max_retries=5,
            )
        assert result[0].content == "hello"
        assert result[3] == 1  # one retry on the sanitization path

    def test_transient_then_empty_assistant_then_success(self):
        """Transient retry before BadRequestError(empty assistant) counts toward total."""
        import litellm

        transient = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        bad_req = litellm.BadRequestError(
            message="must have either content or tool_calls",
            llm_provider="openai",
            model="x",
        )
        resp = _make_response()
        # First call: transient → retry
        # Second call: BadRequestError (empty assistant) → sanitize
        # Third call (after sanitize): success
        mock = MagicMock(side_effect=[transient, bad_req, resp])

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant"},
            {"role": "user", "content": "continue"},
        ]
        with patch("litellm.completion", mock), patch("time.sleep"):
            result = call_llm(
                "http://localhost:8080/v1",
                "my-model",
                messages,
                100,
                0.5,
                1.0,
                None,
                None,
                False,
                provider="generic",
                api_key="test",
                max_retries=5,
            )
        assert result[0].content == "hello"
        # 1 transient retry in first helper + 0 in second helper = 1 total
        assert result[3] == 1

    def test_empty_assistant_then_context_overflow_raises_coe(self):
        """BadRequestError(empty assistant) followed by BadRequestError(context overflow)
        must raise ContextOverflowError, not AgentError, so the compaction pipeline runs."""
        import litellm

        empty_msg = litellm.BadRequestError(
            message="must have either content or tool_calls",
            llm_provider="openai",
            model="x",
        )
        overflow = litellm.BadRequestError(
            message="maximum context length exceeded",
            llm_provider="openai",
            model="x",
        )
        mock = MagicMock(side_effect=[empty_msg, overflow])

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant"},
            {"role": "user", "content": "continue"},
        ]
        with patch("litellm.completion", mock):
            with pytest.raises(ContextOverflowError, match="post-sanitization"):
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    messages,
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                )

    def test_provider_retries_on_failure(self):
        """When call_llm fails after retries, AgentError carries _provider_retries."""
        import litellm

        exc = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        mock = MagicMock(side_effect=[exc, exc, exc])
        with patch("litellm.completion", mock), patch("time.sleep"):
            with pytest.raises(AgentError) as exc_info:
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                    max_retries=3,
                )
        assert getattr(exc_info.value, "_provider_retries", None) == 2

    def test_provider_retries_on_context_overflow(self):
        """ContextOverflowError carries _provider_retries."""
        import litellm

        mock = MagicMock(
            side_effect=litellm.ContextWindowExceededError(
                message="too long", llm_provider="openai", model="x"
            )
        )
        with patch("litellm.completion", mock):
            with pytest.raises(ContextOverflowError) as exc_info:
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                )
        assert getattr(exc_info.value, "_provider_retries", None) == 0

    def test_max_retries_zero_clamps_to_one(self):
        """max_retries=0 is clamped to 1 (single attempt, no crash)."""
        import litellm

        exc = litellm.APIConnectionError(
            message="Connection reset", llm_provider="openai", model="x"
        )
        with patch("litellm.completion", side_effect=exc):
            with pytest.raises(AgentError, match="LLM call failed"):
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                    max_retries=0,
                )

    def test_null_choices_normal_path(self):
        """choices=None in response raises AgentError through normal call_llm path."""
        resp = types.SimpleNamespace(choices=None)
        with patch("litellm.completion", return_value=resp):
            with pytest.raises(AgentError, match="choices=None"):
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                )

    def test_null_choices_post_sanitization_path(self):
        """choices=None after empty-assistant sanitization raises AgentError."""
        import litellm

        bad_req = litellm.BadRequestError(
            message="must have either content or tool_calls",
            llm_provider="openai",
            model="x",
        )
        null_resp = types.SimpleNamespace(choices=None)
        mock = MagicMock(side_effect=[bad_req, null_resp])

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant"},
            {"role": "user", "content": "continue"},
        ]
        with patch("litellm.completion", mock):
            with pytest.raises(AgentError, match="choices=None"):
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    messages,
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                )

    def test_empty_choices_list(self):
        """choices=[] raises AgentError with 'empty choices list' (distinct from None)."""
        resp = types.SimpleNamespace(choices=[])
        with patch("litellm.completion", return_value=resp):
            with pytest.raises(AgentError, match="empty choices list"):
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                )

    def test_null_choices_retry_exhaustion_message(self):
        """InternalServerError from null choices retries 5 times; final error includes model_id."""
        import litellm

        exc = litellm.InternalServerError(
            message="Invalid response object: choices is None",
            llm_provider="openai",
            model="x",
        )
        mock = MagicMock(side_effect=[exc] * 5)
        with patch("litellm.completion", mock), patch("time.sleep"):
            with pytest.raises(AgentError, match=r"model: my-model") as exc_info:
                call_llm(
                    "http://localhost:8080/v1",
                    "my-model",
                    [{"role": "user", "content": "hi"}],
                    100,
                    0.5,
                    1.0,
                    None,
                    None,
                    False,
                    provider="generic",
                    api_key="test",
                    max_retries=5,
                )
        assert exc_info.value._provider_retries == 4
        assert mock.call_count == 5

    def test_sso_expiry_error_message_includes_profile(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Error when retrieving token from sso: "
            "Token has expired and refresh failed",
            llm_provider="bedrock",
            model="x",
        )
        with patch("litellm.completion", side_effect=exc):
            with pytest.raises(
                AgentError, match=r"aws sso login --profile=myprofile"
            ) as exc_info:
                call_llm(
                    None,
                    "anthropic.claude-opus-4-6-v1",
                    [{"role": "user", "content": "hi"}],
                    4096,
                    None,
                    None,
                    None,
                    None,
                    False,
                    provider="bedrock",
                    aws_profile="myprofile",
                )
            assert exc_info.value._provider_retries == 0

    def test_sso_expiry_uses_aws_profile_env(self, monkeypatch):
        import litellm

        monkeypatch.setenv("AWS_PROFILE", "envprofile")
        exc = litellm.APIConnectionError(
            message="Error when retrieving token from sso: "
            "Token has expired and refresh failed",
            llm_provider="bedrock",
            model="x",
        )
        with patch("litellm.completion", side_effect=exc):
            with pytest.raises(
                AgentError, match=r"aws sso login --profile=envprofile"
            ) as exc_info:
                call_llm(
                    None,
                    "anthropic.claude-opus-4-6-v1",
                    [{"role": "user", "content": "hi"}],
                    4096,
                    None,
                    None,
                    None,
                    None,
                    False,
                    provider="bedrock",
                )
            assert exc_info.value._provider_retries == 0

    def test_sso_missing_token_error_message(self):
        import litellm

        exc = litellm.APIConnectionError(
            message="Error loading SSO Token: Token for fastly does not exist",
            llm_provider="bedrock",
            model="x",
        )
        with patch("litellm.completion", side_effect=exc):
            with pytest.raises(
                AgentError, match=r"aws sso login --profile=myprofile"
            ) as exc_info:
                call_llm(
                    None,
                    "anthropic.claude-opus-4-6-v1",
                    [{"role": "user", "content": "hi"}],
                    4096,
                    None,
                    None,
                    None,
                    None,
                    False,
                    provider="bedrock",
                    aws_profile="myprofile",
                )
            assert exc_info.value._provider_retries == 0

    def test_sso_expiry_defaults_to_default_profile(self, monkeypatch):
        import litellm

        monkeypatch.delenv("AWS_PROFILE", raising=False)
        exc = litellm.APIConnectionError(
            message="Error when retrieving token from sso: "
            "Token has expired and refresh failed",
            llm_provider="bedrock",
            model="x",
        )
        with patch("litellm.completion", side_effect=exc):
            with pytest.raises(
                AgentError, match=r"aws sso login --profile=default"
            ) as exc_info:
                call_llm(
                    None,
                    "anthropic.claude-opus-4-6-v1",
                    [{"role": "user", "content": "hi"}],
                    4096,
                    None,
                    None,
                    None,
                    None,
                    False,
                    provider="bedrock",
                )
            assert exc_info.value._provider_retries == 0

    def test_session_rejects_retries_zero(self):
        """Session(retries=0) raises ValueError."""
        from swival.session import Session

        with pytest.raises(ValueError, match="retries must be >= 1"):
            Session(retries=0)


def _delta_chunk(text):
    delta = types.SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta)
    return types.SimpleNamespace(choices=[choice])


class TestCompletionViaStream:
    def test_stream_entered_only_after_handoff(self, monkeypatch):
        """The live stream display opens only once accumulated text crosses the
        handoff threshold, and on_stream_start fires exactly once right before
        the first update."""
        import contextlib
        from io import StringIO

        from rich.console import Console

        from swival import agent, fmt

        old_console = fmt._console
        fmt._console = Console(file=StringIO(), width=40, height=10)
        threshold = max(fmt._console.width, 40)

        events = []

        @contextlib.contextmanager
        def fake_stream_raw():
            events.append(("enter", None))
            yield lambda text: events.append(("update", len(text)))

        monkeypatch.setattr(fmt, "stream_raw", fake_stream_raw)

        # Three deltas below the threshold individually, crossing it cumulatively
        # on the third.
        step = threshold // 2
        chunks = [_delta_chunk("x" * step) for _ in range(3)]

        def fake_completion(**kwargs):
            assert kwargs.get("stream") is True
            return iter(chunks)

        starts = []
        try:
            with (
                patch("litellm.completion", fake_completion),
                patch(
                    "litellm.stream_chunk_builder",
                    lambda chunks, messages=None: "rebuilt",
                ),
            ):
                result = agent._completion_via_stream(
                    {"model": "x", "messages": []},
                    on_stream_start=lambda: starts.append(True),
                )
        finally:
            fmt._console = old_console

        assert result == "rebuilt"
        assert len(starts) == 1
        kinds = [k for k, _ in events]
        assert kinds.count("enter") == 1
        # Order: enter the live context, then the first update.
        assert kinds[0] == "enter"
        assert kinds[1] == "update"
        # Nothing was drawn before the threshold was crossed: the first update
        # already holds at least a threshold's worth of text.
        first_update_len = next(n for k, n in events if k == "update")
        assert first_update_len >= threshold
