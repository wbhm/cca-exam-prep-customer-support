"""Agentic tool-use loop for CCA Customer Support agent.

CCA Rules enforced here:
- Terminate on stop_reason, NEVER content-type checking (CCA agentic loop rule)
- stop_reason == 'end_turn' -> agent is done
- stop_reason == 'tool_use' -> dispatch tools, continue loop
- stop_reason == 'escalated' -> tool_choice forced escalate_to_human completed
- Escalation is enforced at two points: when a callback blocks process_refund,
  and when Claude ends its turn with an escalation flag set but nothing queued
  (callbacks only run after tool calls, so a turn that ends in a question
  would otherwise drop the case silently)
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
        'end_turn'       -> Claude finished normally
        'tool_use'       -> loop hit max_iterations while dispatching tools (should not occur)
        'max_iterations' -> safety limit exceeded
        'escalated'      -> tool_choice forced escalate_to_human completed successfully,
                            either after a blocked process_refund or because Claude
                            ended its turn with an escalation flag set and nothing queued.
                            final_text holds Claude's last customer-facing text, if any.
    """

    stop_reason: str
    messages: list = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    final_text: str = ""
    usage: UsageSummary = field(default_factory=UsageSummary)


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
    """Run the agentic tool-use loop until end_turn or max_iterations.

    CCA Rule: Terminate on stop_reason ONLY — never check content block types.
    This is deterministic: stop_reason drives all flow control.

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
    """
    active_tools = tools if tools is not None else TOOLS
    # Build context dict for callback enrichment (user_message + escalation flags)
    context: dict = {"user_message": user_message}
    messages: list[dict] = [{"role": "user", "content": user_message}]
    tool_calls: list[dict] = []
    usage = UsageSummary()

    def _force_escalation() -> None:
        """Make one API call with tool_choice pinned to escalate_to_human and dispatch it.

        Mutates messages, tool_calls, and usage. Caller must have already appended a
        user turn (tool_results or the escalation-required notice) so roles alternate.
        tool_choice may invalidate prompt cache — acceptable for a one-time call.
        """
        forced_response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=active_tools,
            messages=messages,
            tool_choice={"type": "tool", "name": "escalate_to_human"},
        )
        _add_usage(usage, forced_response.usage)
        messages.append({"role": "assistant", "content": forced_response.content})

        escalation_results = []
        for block in forced_response.content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue
            tool_calls.append({"name": block.name, "input": block.input, "id": block.id})
            result_content = dispatch(
                block.name, block.input, services, context=context, callbacks=callbacks
            )
            escalation_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result_content}
            )
        messages.append({"role": "user", "content": escalation_results})

    for _iteration in range(max_iterations):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=active_tools,
            messages=messages,
        )
        _add_usage(usage, response.usage)

        # Append assistant turn to message history
        messages.append({"role": "assistant", "content": response.content})

        # CCA RULE: Check stop_reason, NEVER content block types
        if response.stop_reason != "tool_use":
            # Extract final text from text blocks
            final_text = ""
            for block in response.content:
                if hasattr(block, "type") and block.type == "text":
                    final_text = block.text
                    break

            # TURN-END ENFORCEMENT: Claude stopped (e.g. asked the customer a question)
            # while a business rule requires escalation and nothing is in the queue.
            # PostToolUse callbacks cannot catch this — no tool was called — so the
            # loop enforces it here. Deterministic: driven by context flags + the store.
            flag = _pending_escalation_flag(context, services)
            if flag is not None:
                notice = {
                    "status": "escalation_required",
                    "reason": ESCALATION_FLAGS[flag],
                    "flag_triggered": flag,
                    "action_required": "escalate_to_human",
                }
                messages.append({"role": "user", "content": json.dumps(notice)})
                _force_escalation()
                return AgentResult(
                    stop_reason="escalated",
                    messages=messages,
                    tool_calls=tool_calls,
                    final_text=final_text,
                    usage=usage,
                )

            return AgentResult(
                stop_reason=response.stop_reason,
                messages=messages,
                tool_calls=tool_calls,
                final_text=final_text,
                usage=usage,
            )

        # Dispatch all tool_use blocks and collect results
        tool_results = []
        for block in response.content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue

            tool_calls.append({"name": block.name, "input": block.input, "id": block.id})
            result_content = dispatch(
                block.name, block.input, services, context=context, callbacks=callbacks
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_content,
                }
            )

        # CCA PITFALL: Send ONLY tool_result blocks — no text alongside them
        messages.append({"role": "user", "content": tool_results})

        # HANDOFF-01: Detect blocked refund — force tool_choice escalation immediately
        if _has_escalation_required(tool_results):
            _force_escalation()
            return AgentResult(
                stop_reason="escalated",
                messages=messages,
                tool_calls=tool_calls,
                final_text="",
                usage=usage,
            )

    # Safety limit exceeded
    return AgentResult(
        stop_reason="max_iterations",
        messages=messages,
        tool_calls=tool_calls,
        final_text="",
        usage=usage,
    )
