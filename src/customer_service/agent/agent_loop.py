"""Agentic tool-use loop for CCA Customer Support agent.

CCA Rules enforced here:
- Terminate on stop_reason, NEVER content-type checking (CCA agentic loop rule)
- Every stop_reason the Messages API can return is matched explicitly and
  mapped to one of four actions. An unknown value raises instead of being
  mistaken for a finished turn:
    tool_use                 -> dispatch tools, continue the loop
    pause_turn               -> resend the same history, continue the loop
    end_turn / stop_sequence -> finished (turn-end escalation guard applies)
    max_tokens               -> finished but truncated (same guard; callers see
                                'max_tokens' so the text is never treated as complete)
    model_context_window_exceeded
                             -> finished but truncated because the conversation filled
                                the model's context window (same guard). Unlike
                                max_tokens, retrying the same history cannot succeed
    refusal                  -> blocked; force escalate_to_human
    anything else            -> UnexpectedStopReasonError
- Escalation is enforced at three points: when a callback blocks process_refund,
  when Claude ends its turn with an escalation flag set but nothing queued
  (callbacks only run after tool calls, so a turn that ends in a question
  would otherwise drop the case silently), and when Claude refuses to continue
- 'escalated' is a claim about the escalation_queue, verified against the store.
  A forced call that produces nothing returns 'escalation_failed', never 'escalated'
- Degraded outcomes (max_tokens, escalation_failed, max_iterations) carry the
  same structured error context the tool dispatcher returns, so a caller such as
  the coordinator can tell 'finished' from 'failed' without parsing strings
- Accumulate usage tokens across all iterations
- Safety limit: max_iterations guard returns 'max_iterations' stop_reason
"""

import json
from dataclasses import dataclass, field

from customer_service.agent.callbacks import ESCALATION_FLAGS
from customer_service.services.container import ServiceContainer
from customer_service.tools.definitions import TOOLS
from customer_service.tools.handlers import dispatch


@dataclass
class UsageSummary:
    """Accumulated token usage across all loop iterations."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class AgentResult:
    """Result returned by run_agent_loop.

    stop_reason values:
        'end_turn'          -> Claude finished normally
        'stop_sequence'     -> Claude hit a configured stop sequence; treated as end_turn
        'max_tokens'        -> Claude's reply was cut off by max_tokens. final_text is
                               partial and must not be shown as a complete answer.
        'model_context_window_exceeded'
                            -> Claude's reply was cut off because the conversation
                               filled the model's context window. final_text is partial.
                               A larger max_tokens does not help; the history must be
                               compacted (see context_manager) before any retry.
        'escalated'         -> escalate_to_human reached the escalation_queue (store
                               verified), after a blocked process_refund, a turn that
                               ended with an escalation flag set, or a refusal.
                               final_text holds Claude's last customer-facing text, if any.
        'escalation_failed' -> escalation was required but the forced call queued
                               nothing (for example it was itself refused or truncated).
                               A human has NOT seen this case; the caller must route it.
        'max_iterations'    -> safety limit exceeded

    'refusal', 'tool_use' and 'pause_turn' never appear here: refusal is converted
    into an escalation outcome, and the other two keep the loop running.

    error is None for 'end_turn', 'stop_sequence' and 'escalated'. For the degraded
    outcomes it is the CCA structured error context (status, error_type, source,
    message, retry_eligible, fallback_available, partial_data):
        'max_tokens'        -> error_type 'truncated', retry_eligible, partial final_text
        'model_context_window_exceeded'
                            -> error_type 'context_window_exceeded', NOT retryable as-is
                               (fallback_available: compact the history), partial final_text
        'escalation_failed' -> error_type 'escalation_failed', NOT retryable, names the
                               flag that required escalation
        'max_iterations'    -> error_type 'max_iterations', retry_eligible, tool call count
    """

    stop_reason: str
    messages: list = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    final_text: str = ""
    usage: UsageSummary = field(default_factory=UsageSummary)
    error: dict | None = None


class UnexpectedStopReasonError(RuntimeError):
    """The API returned a stop_reason this loop was not written to handle.

    Raised rather than returned so that a value added to the API after this code
    was written can never be mistaken for a finished customer turn. ``result``
    carries the partial run (messages, tool calls, usage) for logging.
    """

    def __init__(self, stop_reason: str, result: AgentResult) -> None:
        super().__init__(f"Unhandled stop_reason {stop_reason!r} from the Messages API")
        self.stop_reason = stop_reason
        self.result = result


def _structured_error(
    error_type: str,
    message: str,
    retry_eligible: bool,
    fallback_available: bool,
    partial_data: dict,
) -> dict:
    """Build the CCA structured error context for a degraded loop outcome.

    Same six-field shape as the tool dispatcher's error results (see handlers.py),
    so one consumer can handle both boundaries the same way.
    """
    return {
        "status": "error",
        "error_type": error_type,
        "source": "agent_loop",
        "message": message,
        "retry_eligible": retry_eligible,
        "fallback_available": fallback_available,
        "partial_data": partial_data,
    }


def _first_text(content: list) -> str:
    """Return the text of the first text block in a response, or '' if there is none."""
    for block in content:
        if hasattr(block, "type") and block.type == "text":
            return block.text
    return ""


def _has_escalation_required(tool_results: list[dict]) -> bool:
    """Return True if any tool_result contains action_required == 'escalate_to_human'.

    CCA Rule: Detect blocked refund deterministically from structured callback output.
    Parses each tool_result's 'content' field as JSON and checks action_required.

    Args:
        tool_results: List of tool_result dicts from the agent loop iteration.

    Returns:
        True if any result has action_required == 'escalate_to_human', False otherwise.
    """
    for tr in tool_results:
        content = tr.get("content", "")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            continue
        if parsed.get("action_required") == "escalate_to_human":
            return True
    return False


def _pending_escalation_flag(context: dict, services: ServiceContainer) -> str | None:
    """Return the first active escalation flag if no escalation has reached the queue.

    CCA Rule: the guarantee is "a human sees this case", checked against the store
    (escalation_queue), not against what Claude said. Flags are set in context by the
    enrichment callbacks; without callbacks (anti-pattern runs) none are ever set.

    Args:
        context: Loop context dict enriched by callbacks.
        services: ServiceContainer whose escalation_queue is the source of truth.

    Returns:
        The flag name, or None if no flag is active or an escalation is already queued.
    """
    if services.escalation_queue.get_escalations():
        return None
    for flag in ESCALATION_FLAGS:
        if context.get(flag):
            return flag
    return None


def _add_usage(usage: UsageSummary, response_usage: object) -> None:
    """Accumulate one API response's token usage into the running summary."""
    usage.input_tokens += response_usage.input_tokens
    usage.output_tokens += response_usage.output_tokens
    usage.cache_read_input_tokens += getattr(response_usage, "cache_read_input_tokens", 0) or 0
    usage.cache_creation_input_tokens += (
        getattr(response_usage, "cache_creation_input_tokens", 0) or 0
    )


def run_agent_loop(
    client: object,
    services: ServiceContainer,
    user_message: str,
    system_prompt: str | list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    max_iterations: int = 10,
    tools: list[dict] | None = None,
    callbacks: dict | None = None,
) -> AgentResult:
    """Run the agentic tool-use loop until Claude stops or max_iterations is hit.

    CCA Rule: Terminate on stop_reason ONLY — never check content block types.
    Every stop_reason is matched explicitly (see module docstring); an unknown
    value raises UnexpectedStopReasonError.

    Args:
        client: Anthropic API client (or mock in tests)
        services: Injected ServiceContainer with all 5 services
        user_message: Initial customer message
        system_prompt: System context (context only, rules enforced in callbacks).
            Accepts either a plain string OR a list of TextBlockParam dicts for
            prompt caching (OPTIM-01). Pass get_system_prompt_with_caching() to
            enable caching of the POLICY_DOCUMENT block. The Anthropic SDK's
            client.messages.create(system=...) natively accepts both forms.
        model: Claude model identifier
        max_tokens: Max output tokens per API call
        max_iterations: Safety limit to prevent infinite loops
        tools: Tool schemas to pass to the API. Defaults to TOOLS (5 correct tools).
            Anti-pattern modules may pass SWISS_ARMY_TOOLS (15 tools) here.
        callbacks: Per-tool PostToolUse callback registry from build_callbacks().
            CCA Principle #1: programmatic enforcement beats prompt-based guidance.

    Returns:
        AgentResult with stop_reason, all messages, tool_calls, final_text, and usage

    Raises:
        UnexpectedStopReasonError: the API returned a stop_reason not handled here.
    """
    active_tools = tools if tools is not None else TOOLS
    # Build context dict for callback enrichment (user_message + escalation flags)
    context: dict = {"user_message": user_message}
    messages: list[dict] = [{"role": "user", "content": user_message}]
    tool_calls: list[dict] = []
    usage = UsageSummary()

    def _result(stop_reason: str, final_text: str, error: dict | None = None) -> AgentResult:
        return AgentResult(
            stop_reason=stop_reason,
            messages=messages,
            tool_calls=tool_calls,
            final_text=final_text,
            usage=usage,
            error=error,
        )

    def _record_assistant_turn(content: list) -> None:
        """Append the assistant turn. An empty content list (possible on refusal)
        cannot be sent back to the API, so it is not recorded."""
        if content:
            messages.append({"role": "assistant", "content": content})

    def _dispatch_tool_blocks(content: list) -> list[dict]:
        """Dispatch every tool_use block in a response and return the tool_result blocks.

        Block types are read here only to EXTRACT tool calls, never for control flow.
        """
        tool_results = []
        for block in content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue
            tool_calls.append({"name": block.name, "input": block.input, "id": block.id})
            result_content = dispatch(
                block.name, block.input, services, context=context, callbacks=callbacks
            )
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result_content}
            )
        return tool_results

    def _force_escalation() -> bool:
        """Make one API call with tool_choice pinned to escalate_to_human and dispatch it.

        Mutates messages, tool_calls, and usage. Caller must have already appended a
        user turn (tool_results or an escalation-required notice) so roles alternate.
        tool_choice may invalidate prompt cache — acceptable for a one-time call.
        Forced tool_choice is rejected (HTTP 400) by Claude Opus 5.5, Sonnet 5.5 and
        Fable 5.1; this project pins an earlier model.

        Returns:
            True only if the escalation_queue grew. The store is the truth, not the
            API response: a forced call that is refused or truncated queues nothing.
        """
        queued_before = len(services.escalation_queue.get_escalations())
        forced_response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=active_tools,
            messages=messages,
            tool_choice={"type": "tool", "name": "escalate_to_human"},
        )
        _add_usage(usage, forced_response.usage)
        _record_assistant_turn(forced_response.content)

        escalation_results = _dispatch_tool_blocks(forced_response.content)
        if escalation_results:
            messages.append({"role": "user", "content": escalation_results})
        return len(services.escalation_queue.get_escalations()) > queued_before

    def _escalate(final_text: str, notice: dict | None) -> AgentResult:
        """Force escalation and report the verified outcome.

        notice is appended as a user turn when the preceding message is an assistant
        turn (turn-end and refusal paths). The blocked-refund path passes None because
        its tool_results already form the user turn.
        """
        if notice is not None:
            messages.append({"role": "user", "content": json.dumps(notice)})
        queued = _force_escalation()
        if queued:
            return _result("escalated", final_text)
        flag = notice["flag_triggered"] if notice is not None else "blocked_refund"
        return _result(
            "escalation_failed",
            final_text,
            error=_structured_error(
                error_type="escalation_failed",
                message="Escalation was required but the forced escalate_to_human call "
                "queued nothing; no human has seen this case",
                retry_eligible=False,
                fallback_available=False,
                partial_data={"flag_triggered": flag, "final_text": final_text},
            ),
        )

    def _finish_turn(stop_reason: str, final_text: str) -> AgentResult:
        """Claude stopped generating. Apply turn-end escalation enforcement.

        PostToolUse callbacks cannot catch a turn that ends without a tool call (for
        example Claude asking the customer a question), so the loop checks here:
        a business-rule flag set in context with nothing in the queue forces
        escalate_to_human. Deterministic: driven by context flags + the store.
        """
        flag = _pending_escalation_flag(context, services)
        if flag is None:
            error = None
            if stop_reason == "max_tokens":
                error = _structured_error(
                    error_type="truncated",
                    message="Reply cut off by max_tokens; final_text is incomplete",
                    retry_eligible=True,
                    fallback_available=True,
                    partial_data={"final_text": final_text},
                )
            elif stop_reason == "model_context_window_exceeded":
                # Not retryable as-is: the same history will overflow again. The
                # fallback is to compact it (context_manager) and start a fresh turn.
                error = _structured_error(
                    error_type="context_window_exceeded",
                    message="Reply cut off because the conversation filled the model's "
                    "context window; final_text is incomplete. Compact the history "
                    "before retrying",
                    retry_eligible=False,
                    fallback_available=True,
                    partial_data={"final_text": final_text},
                )
            return _result(stop_reason, final_text, error=error)
        notice = {
            "status": "escalation_required",
            "reason": ESCALATION_FLAGS[flag],
            "flag_triggered": flag,
            "action_required": "escalate_to_human",
        }
        return _escalate(final_text, notice)

    for _iteration in range(max_iterations):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=active_tools,
            messages=messages,
        )
        _add_usage(usage, response.usage)
        _record_assistant_turn(response.content)

        # CCA RULE: branch on stop_reason, NEVER on content block types.
        # Every value is named; the default branch is a tripwire, not "done".
        match response.stop_reason:
            case "tool_use":
                tool_results = _dispatch_tool_blocks(response.content)
                # CCA PITFALL: Send ONLY tool_result blocks — no text alongside them
                messages.append({"role": "user", "content": tool_results})
                # HANDOFF-01: a callback blocked process_refund — escalate immediately
                if _has_escalation_required(tool_results):
                    return _escalate(final_text="", notice=None)

            case "pause_turn":
                # A server-side tool paused mid-turn. The history already ends with
                # the assistant turn; resend it unchanged. Unreachable with this
                # project's client-side tools, but handled rather than mistaken for done.
                continue

            case "end_turn" | "stop_sequence":
                return _finish_turn(response.stop_reason, _first_text(response.content))

            case "max_tokens":
                # Degraded: the reply was cut off. The escalation guard still applies;
                # the passthrough stop_reason tells callers the text is incomplete.
                return _finish_turn("max_tokens", _first_text(response.content))

            case "model_context_window_exceeded":
                # Degraded: the conversation filled the context window mid-reply.
                # Same guard as max_tokens, but _finish_turn marks it NOT retryable:
                # a larger max_tokens cannot help, only compacting the history can.
                return _finish_turn("model_context_window_exceeded", _first_text(response.content))

            case "refusal":
                # Blocked: a safety classifier declined to continue. The customer is
                # not helped and no callback will fire, so hand the case to a human.
                notice = {
                    "status": "escalation_required",
                    "reason": "Model declined to continue (stop_reason: refusal)",
                    "flag_triggered": "refusal",
                    "action_required": "escalate_to_human",
                }
                return _escalate(_first_text(response.content), notice)

            case other:
                raise UnexpectedStopReasonError(
                    other, _result(other, _first_text(response.content))
                )

    # Safety limit exceeded
    return _result(
        "max_iterations",
        "",
        error=_structured_error(
            error_type="max_iterations",
            message=f"Loop exceeded max_iterations={max_iterations} without finishing",
            retry_eligible=True,
            fallback_available=True,
            partial_data={"tool_calls": len(tool_calls)},
        ),
    )
