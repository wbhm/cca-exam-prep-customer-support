"""Tests for agentic loop (CORE-06)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from customer_service.agent.agent_loop import (
    AgentResult,
    UnexpectedStopReasonError,
    UsageSummary,
    run_agent_loop,
)
from customer_service.agent.callbacks import build_callbacks
from customer_service.agent.system_prompts import get_system_prompt


def _make_usage(inp=100, out=50, cr=0, cw=0):
    """Create a mock usage object matching Anthropic SDK shape."""
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cr,
        cache_creation_input_tokens=cw,
    )


def _make_text_block(text="Done"):
    return SimpleNamespace(type="text", text=text)


def _make_tool_use_block(name="lookup_customer", input_dict=None, tool_id="toolu_01"):
    return SimpleNamespace(
        type="tool_use",
        name=name,
        input=input_dict or {"customer_id": "C001"},
        id=tool_id,
    )


def _make_response(stop_reason="end_turn", content=None, usage=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content or [_make_text_block()],
        usage=usage or _make_usage(),
    )


class TestAgentResult:
    def test_agent_result_fields(self):
        result = AgentResult(stop_reason="end_turn")
        assert result.stop_reason == "end_turn"
        assert result.messages == []
        assert result.tool_calls == []
        assert result.final_text == ""
        assert isinstance(result.usage, UsageSummary)

    def test_usage_summary_defaults(self):
        u = UsageSummary()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cache_read_input_tokens == 0
        assert u.cache_creation_input_tokens == 0


class TestAgentLoop:
    def test_loop_end_turn(self, services):
        """Agent returns immediately when stop_reason is end_turn."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="end_turn",
            content=[_make_text_block("I've helped you!")],
        )
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="Hello",
            system_prompt="You are helpful.",
        )
        assert result.stop_reason == "end_turn"
        assert result.final_text == "I've helped you!"
        assert mock_client.messages.create.call_count == 1

    def test_loop_tool_use_then_end(self, services):
        """Agent dispatches tool, then ends on second call."""
        mock_client = MagicMock()
        # First call: tool_use
        tool_response = _make_response(
            stop_reason="tool_use",
            content=[_make_tool_use_block("lookup_customer", {"customer_id": "C001"})],
        )
        # Second call: end_turn
        end_response = _make_response(
            stop_reason="end_turn",
            content=[_make_text_block("Found Alice!")],
        )
        mock_client.messages.create.side_effect = [tool_response, end_response]

        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="Look up C001",
            system_prompt="You are helpful.",
        )
        assert result.stop_reason == "end_turn"
        assert result.final_text == "Found Alice!"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "lookup_customer"
        assert mock_client.messages.create.call_count == 2

    def test_loop_max_iterations(self, services):
        """Loop stops at max_iterations with correct stop_reason."""
        mock_client = MagicMock()
        # Always return tool_use to trigger infinite loop
        mock_client.messages.create.return_value = _make_response(
            stop_reason="tool_use",
            content=[_make_tool_use_block()],
        )
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="Loop forever",
            system_prompt="test",
            max_iterations=3,
        )
        assert result.stop_reason == "max_iterations"
        assert mock_client.messages.create.call_count == 3

    def test_loop_usage_accumulation(self, services):
        """Usage tokens accumulated across iterations."""
        mock_client = MagicMock()
        tool_resp = _make_response(
            stop_reason="tool_use",
            content=[_make_tool_use_block()],
            usage=_make_usage(inp=100, out=50, cr=10, cw=5),
        )
        end_resp = _make_response(
            stop_reason="end_turn",
            content=[_make_text_block("Done")],
            usage=_make_usage(inp=80, out=30, cr=20, cw=0),
        )
        mock_client.messages.create.side_effect = [tool_resp, end_resp]

        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="test",
            system_prompt="test",
        )
        assert result.usage.input_tokens == 180  # 100 + 80
        assert result.usage.output_tokens == 80  # 50 + 30
        assert result.usage.cache_read_input_tokens == 30  # 10 + 20
        assert result.usage.cache_creation_input_tokens == 5  # 5 + 0

    def test_loop_handles_max_tokens_stop(self, services):
        """Non-tool stop reasons terminate gracefully."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="max_tokens",
            content=[_make_text_block("Truncat")],
        )
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="test",
            system_prompt="test",
        )
        assert result.stop_reason == "max_tokens"
        assert result.final_text == "Truncat"

    def test_loop_no_content_type_checking(self, services):
        """Verify loop uses stop_reason, NOT content block types."""
        mock_client = MagicMock()
        # Return end_turn even though content has tool_use block
        # (hypothetical edge case — loop must trust stop_reason)
        mock_client.messages.create.return_value = _make_response(
            stop_reason="end_turn",
            content=[_make_tool_use_block(), _make_text_block("Done")],
        )
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="test",
            system_prompt="test",
        )
        # Must end, not dispatch tools, because stop_reason says end_turn
        assert result.stop_reason == "end_turn"
        assert mock_client.messages.create.call_count == 1


class TestSystemPrompt:
    def test_get_system_prompt_returns_string(self):
        prompt = get_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 50

    def test_system_prompt_mentions_customer_support(self):
        prompt = get_system_prompt()
        assert "customer" in prompt.lower()


# ---------------------------------------------------------------------------
# TestStopReasonDispatch
# ---------------------------------------------------------------------------


def _escalation_input(amount=600.0):
    return {
        "customer_id": "C003",
        "customer_tier": "regular",
        "issue_type": "refund",
        "disputed_amount": amount,
        "escalation_reason": "Refund amount exceeds $500 review threshold",
        "recommended_action": "Review refund request",
        "conversation_summary": f"Customer requested refund of ${amount}",
        "turns_elapsed": 1,
    }


def _forced_escalation_response(tool_id="toolu_99"):
    """What the API returns for a tool_choice-forced escalate_to_human call."""
    return _make_response(
        stop_reason="tool_use",
        content=[_make_tool_use_block("escalate_to_human", _escalation_input(), tool_id)],
    )


def _c003_lookup_and_policy():
    """Scripted turns that make build_callbacks() set the requires_review flag."""
    lookup = _make_response(
        stop_reason="tool_use",
        content=[_make_tool_use_block("lookup_customer", {"customer_id": "C003"}, "toolu_01")],
    )
    policy = _make_response(
        stop_reason="tool_use",
        content=[
            _make_tool_use_block(
                "check_policy",
                {"customer_id": "C003", "customer_tier": "regular", "requested_amount": 600.0},
                "toolu_02",
            )
        ],
    )
    return [lookup, policy]


class TestStopReasonDispatch:
    """Every stop_reason the Messages API can return is handled explicitly.

    end_turn / stop_sequence -> finished; max_tokens -> finished but degraded;
    tool_use -> dispatch; pause_turn -> resend; refusal -> forced escalation;
    anything else -> UnexpectedStopReasonError. Nothing lands in "done" by default.
    """

    def test_stop_sequence_finishes_like_end_turn(self, services):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="stop_sequence",
            content=[_make_text_block("Done at the stop sequence")],
        )
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert result.stop_reason == "stop_sequence"
        assert result.final_text == "Done at the stop sequence"
        assert mock_client.messages.create.call_count == 1

    def test_pause_turn_resends_without_appending_a_user_turn(self, services):
        """pause_turn means 'call again with the same history', not 'done'."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_response(stop_reason="pause_turn", content=[_make_text_block("Searching...")]),
            _make_response(stop_reason="end_turn", content=[_make_text_block("Resumed")]),
        ]
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert result.stop_reason == "end_turn"
        assert result.final_text == "Resumed"
        assert mock_client.messages.create.call_count == 2
        second_call_messages = mock_client.messages.create.call_args_list[1][1]["messages"]
        assert second_call_messages[-1]["role"] == "assistant"
        assert result.tool_calls == []

    def test_refusal_forces_escalation_into_queue(self, services):
        """A refusal is a blocked customer: the loop must hand the case to a human."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_response(stop_reason="refusal", content=[]),
            _forced_escalation_response(),
        ]
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="Customer ID: C003. I need a $600 refund.",
            system_prompt=get_system_prompt(),
            callbacks=build_callbacks(),
        )
        # TEST THE STORE
        assert len(services.escalation_queue.get_escalations()) == 1
        assert result.stop_reason == "escalated"
        assert mock_client.messages.create.call_count == 2
        forced_kwargs = mock_client.messages.create.call_args_list[1][1]
        assert forced_kwargs["tool_choice"] == {"type": "tool", "name": "escalate_to_human"}
        # The forced call was preceded by a structured notice naming the refusal.
        # (Inspect the recorded history: the mock holds a reference to the live list.)
        notices = [
            m["content"]
            for m in result.messages
            if m["role"] == "user" and isinstance(m["content"], str)
        ][1:]  # skip the original user_message
        assert len(notices) == 1
        assert '"action_required": "escalate_to_human"' in notices[0]
        assert "refusal" in notices[0]

    def test_refusal_whose_forced_call_is_also_refused_reports_failure(self, services):
        """If the forced escalation produces nothing, the result must not claim success."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_response(stop_reason="refusal", content=[]),
            _make_response(stop_reason="refusal", content=[]),
        ]
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert services.escalation_queue.get_escalations() == []
        assert result.stop_reason == "escalation_failed"
        assert mock_client.messages.create.call_count == 2

    def test_max_tokens_with_pending_flag_still_escalates(self, services):
        """A truncated turn does not release the escalation guarantee."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            *_c003_lookup_and_policy(),
            _make_response(stop_reason="max_tokens", content=[_make_text_block("Your $6")]),
            _forced_escalation_response(),
        ]
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="Customer ID: C003. I need a $600 refund for my damaged order.",
            system_prompt=get_system_prompt(),
            callbacks=build_callbacks(),
        )
        assert len(services.escalation_queue.get_escalations()) == 1
        assert result.stop_reason == "escalated"
        assert result.final_text == "Your $6"
        assert mock_client.messages.create.call_count == 4

    def test_context_window_exceeded_with_pending_flag_still_escalates(self, services):
        """A turn cut off by the context window does not release the escalation guarantee."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            *_c003_lookup_and_policy(),
            _make_response(
                stop_reason="model_context_window_exceeded",
                content=[_make_text_block("Your $6")],
            ),
            _forced_escalation_response(),
        ]
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="Customer ID: C003. I need a $600 refund for my damaged order.",
            system_prompt=get_system_prompt(),
            callbacks=build_callbacks(),
        )
        assert len(services.escalation_queue.get_escalations()) == 1
        assert result.stop_reason == "escalated"
        assert result.final_text == "Your $6"
        assert mock_client.messages.create.call_count == 4

    def test_unknown_stop_reason_raises_instead_of_finishing(self, services):
        """A value this loop was not written for is an error, never a silent 'done'."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="some_future_value",
            content=[_make_text_block("??")],
            usage=_make_usage(inp=7, out=3),
        )
        with pytest.raises(UnexpectedStopReasonError) as exc_info:
            run_agent_loop(
                client=mock_client, services=services, user_message="hi", system_prompt="t"
            )
        err = exc_info.value
        assert err.stop_reason == "some_future_value"
        assert "some_future_value" in str(err)
        # The partial run is attached so callers can log it
        assert err.result.stop_reason == "some_future_value"
        assert len(err.result.messages) == 2
        assert err.result.usage.input_tokens == 7


# ---------------------------------------------------------------------------
# TestStructuredErrorOnDegradedResults
# ---------------------------------------------------------------------------

STRUCTURED_ERROR_FIELDS = {
    "status",
    "error_type",
    "source",
    "message",
    "retry_eligible",
    "fallback_available",
    "partial_data",
}


class TestStructuredErrorOnDegradedResults:
    """CCA silent-failure rule: a degraded outcome carries structured error context.

    A bare stop_reason string tells the caller WHAT happened. The error dict tells
    it what to DO: whether a retry can help, whether a fallback exists, and what
    partial state is available. Same six-field shape the tool dispatcher uses.
    """

    def test_end_turn_result_has_no_error(self, services):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="end_turn", content=[_make_text_block("All done")]
        )
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert result.stop_reason == "end_turn"
        assert result.error is None

    def test_max_tokens_result_carries_structured_error(self, services):
        """Truncated reply -> error says retry is possible and carries the partial text."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="max_tokens", content=[_make_text_block("Truncat")]
        )
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert result.stop_reason == "max_tokens"
        assert result.error is not None
        assert set(result.error) == STRUCTURED_ERROR_FIELDS
        assert result.error["status"] == "error"
        assert result.error["error_type"] == "truncated"
        assert result.error["source"] == "agent_loop"
        assert result.error["retry_eligible"] is True
        assert result.error["fallback_available"] is True
        assert result.error["partial_data"] == {"final_text": "Truncat"}

    def test_context_window_exceeded_result_carries_structured_error(self, services):
        """Context window full -> NOT retryable as-is (the history must be compacted first)."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="model_context_window_exceeded", content=[_make_text_block("Truncat")]
        )
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert result.stop_reason == "model_context_window_exceeded"
        assert result.final_text == "Truncat"
        assert result.error is not None
        assert set(result.error) == STRUCTURED_ERROR_FIELDS
        assert result.error["status"] == "error"
        assert result.error["error_type"] == "context_window_exceeded"
        assert result.error["source"] == "agent_loop"
        assert result.error["retry_eligible"] is False
        assert result.error["fallback_available"] is True
        assert result.error["partial_data"] == {"final_text": "Truncat"}
        assert mock_client.messages.create.call_count == 1

    def test_escalation_failed_result_carries_structured_error(self, services):
        """Forced escalation queued nothing -> not retryable, names the trigger."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_response(stop_reason="refusal", content=[]),
            _make_response(stop_reason="refusal", content=[]),
        ]
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert services.escalation_queue.get_escalations() == []
        assert result.stop_reason == "escalation_failed"
        assert result.error is not None
        assert set(result.error) == STRUCTURED_ERROR_FIELDS
        assert result.error["error_type"] == "escalation_failed"
        assert result.error["retry_eligible"] is False
        assert result.error["fallback_available"] is False
        assert result.error["partial_data"]["flag_triggered"] == "refusal"

    def test_max_iterations_result_carries_structured_error(self, services):
        """Safety limit hit -> retryable, reports how many tool calls were made."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            stop_reason="tool_use",
            content=[_make_tool_use_block("lookup_customer", {"customer_id": "C001"})],
        )
        result = run_agent_loop(
            client=mock_client,
            services=services,
            user_message="hi",
            system_prompt="t",
            max_iterations=3,
        )
        assert result.stop_reason == "max_iterations"
        assert result.error is not None
        assert set(result.error) == STRUCTURED_ERROR_FIELDS
        assert result.error["error_type"] == "max_iterations"
        assert result.error["retry_eligible"] is True
        assert result.error["partial_data"] == {"tool_calls": 3}

    def test_escalated_result_has_no_error(self, services):
        """A verified handoff is a success, not a degraded outcome."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_response(stop_reason="refusal", content=[]),
            _forced_escalation_response(),
        ]
        result = run_agent_loop(
            client=mock_client, services=services, user_message="hi", system_prompt="t"
        )
        assert result.stop_reason == "escalated"
        assert len(services.escalation_queue.get_escalations()) == 1
        assert result.error is None
