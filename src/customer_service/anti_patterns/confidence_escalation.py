"""CCA Anti-Pattern 1: Confidence-Based Escalation.

WRONG PATTERN: System prompt tells Claude to self-rate confidence and route
based on a numeric threshold. This is the #1 CCA exam trap.

WHY IT FAILS:
- Nothing in code enforces the $500 rule; the outcome depends on what Claude
  decides to do on a given run
- Observed over 35 live runs on the $600 scenario: 33 escalated, 2 ended the
  turn with a clarifying question and never escalated or logged. No human was
  notified on those runs and nothing in code caught the omission
- The escalations that do happen cite the check_policy tool result
  (approved=false, requires_review=true), not the confidence score. The
  self-rated confidence never determines the outcome either way
- Self-reported confidence is NEVER a reliable routing signal

CCA CORRECT PATTERN (in agent/callbacks.py):
- Deterministic business rules check amount, tier, and flags in code
- process_refund callback blocks when amount > $500, Claude naturally escalates

This module is imported ONLY by notebooks to demonstrate the failure.
Do NOT add callbacks or programmatic enforcement here — the anti-pattern
must fail in its expected way for the teaching comparison to work.
"""

from customer_service.agent.agent_loop import AgentResult, run_agent_loop
from customer_service.services.container import ServiceContainer

# ---------------------------------------------------------------------------
# The anti-pattern: confidence routing in the system prompt
# ---------------------------------------------------------------------------

CONFIDENCE_SYSTEM_PROMPT: str = (
    "You are a customer support agent for an online retail company. "
    "Your role is to help customers with refund requests, account inquiries, "
    "and general support issues.\n\n"
    "You have access to 5 tools:\n"
    "1. lookup_customer - Find customer profile by ID\n"
    "2. check_policy - Check refund eligibility against policy\n"
    "3. process_refund - Process an approved refund\n"
    "4. escalate_to_human - Transfer to human agent when needed\n"
    "5. log_interaction - Record the interaction for audit\n\n"
    "ESCALATION RULE:\n"
    "Before taking any action on a refund or escalation, rate your confidence "
    "from 0-100 that you can handle this case correctly without human assistance. "
    "If your confidence is below 70, use escalate_to_human to transfer the case. "
    "If your confidence is 70 or above, proceed to handle the case yourself, "
    "including processing refunds directly.\n\n"
    "Always look up the customer first before taking any action. "
    "Check policy before processing refunds. "
    "Log every interaction for compliance purposes.\n\n"
    "Be professional, empathetic, and efficient."
)


def run_confidence_agent(
    client: object,
    services: ServiceContainer,
    user_message: str,
    model: str = "claude-sonnet-4-6",
) -> AgentResult:
    """Run the confidence-based escalation anti-pattern agent.

    ANTI-PATTERN: No callbacks. Routing relies on self-reported confidence
    in the system prompt. In practice Claude escalates on most runs because
    the check_policy tool result says the refund is not approved. On a
    minority of runs it ends the turn with a follow-up question instead and
    the case is silently dropped: no escalation record, no audit entry.

    Args:
        client: Anthropic API client
        services: Injected ServiceContainer
        user_message: Customer message
        model: Claude model identifier

    Returns:
        AgentResult — usually escalated, occasionally neither escalated nor
        refunded. Judge each run by services.escalation_queue, not by text
    """
    return run_agent_loop(
        client=client,
        services=services,
        user_message=user_message,
        system_prompt=CONFIDENCE_SYSTEM_PROMPT,
        model=model,
    )
    # NOTE: No callbacks passed. This is the anti-pattern.
    # The correct pattern (in agent/callbacks.py) uses deterministic rules.
