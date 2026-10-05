"""PostToolUse callback enforcement layer.

CCA Principle #1: Programmatic enforcement beats prompt-based guidance.
Business rules are enforced here in code — deterministically — not in system prompts.

Escalation thresholds (from CCA rules):
  - amount > $500 (requires_review flag set by check_policy_callback)
  - account closure flag
  - VIP tier
  - legal complaint keywords in user message

Compliance:
  - PII redaction: credit card numbers redacted to ****-****-****-NNNN pattern in
    every free-text field that reaches a persistent store (audit log and
    escalation queue), before the write happens
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from customer_service.services.container import ServiceContainer
from customer_service.tools.definitions import ALL_REDACTED_FIELDS, REDACTED_FIELDS

# ---------------------------------------------------------------------------
# Legal keyword detection (CCA: deterministic rules, not LLM confidence)
# ---------------------------------------------------------------------------

LEGAL_KEYWORDS: list[str] = ["lawsuit", "attorney", "lawyer", "legal action", "sue", "court"]

# ---------------------------------------------------------------------------
# Escalation flags (CCA: deterministic rules, not LLM confidence)
# Set in context by the enrichment callbacks; any active flag means a human
# must see this case. Used by escalation_callback (blocks process_refund) and
# by the agent loop (forces escalate_to_human if Claude ends its turn without
# having escalated).
# ---------------------------------------------------------------------------

ESCALATION_FLAGS: dict[str, str] = {
    "vip": "VIP account requires human review",
    "account_closure": "Account closure in progress requires human review",
    "legal_complaint": "Legal complaint detected — escalate immediately",
    "requires_review": "Refund amount exceeds $500 review threshold",
}

# ---------------------------------------------------------------------------
# PCI compliance: credit card regex
# Matches 16-digit card numbers with dashes or spaces as separators.
# Groups: (first-12-digits)(separator)(last-4-digits)
# ---------------------------------------------------------------------------

CARD_PATTERN: re.Pattern[str] = re.compile(r"\b(\d{4}[-\s]\d{4}[-\s]\d{4}[-\s])(\d{4})\b")

# Nested containers the tool handlers wrap their stored object in:
#   log_interaction   -> {"status": "logged",    "entry":  {...}}
#   escalate_to_human -> {"status": "escalated", "record": {...}}
_NESTED_KEYS: tuple[str, ...] = ("entry", "record")


# ---------------------------------------------------------------------------
# CallbackResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class CallbackResult:
    """Result returned by a PostToolUse callback function.

    Attributes:
        action: What the dispatcher should do with the tool result.
            "allow"          -> return original tool result unchanged
            "replace_result" -> return replacement instead of original
            "block"          -> veto the operation; return replacement as error
        replacement: JSON string to return in place of original result (required when
            action is "block" or "replace_result").
        reason: Human-readable explanation (used for logging/debugging).
    """

    action: Literal["allow", "replace_result", "block"]
    replacement: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Per-tool callback functions
# Each has signature: (tool_name, input_dict, result_dict, context, services) -> CallbackResult
# ---------------------------------------------------------------------------

CallbackFn = Callable[
    [str, dict, dict, dict, ServiceContainer],
    CallbackResult,
]


def lookup_customer_callback(
    tool_name: str,
    input_dict: dict,
    result_dict: dict,
    context: dict,
    services: ServiceContainer,
) -> CallbackResult:
    """Set context flags from customer profile after lookup_customer executes.

    Sets context["vip"] and context["account_closure"] from the customer tier/flags.
    Also scans context["user_message"] for legal keywords to set context["legal_complaint"].

    CCA Rule: Programmatic detection of escalation conditions, not LLM confidence.
    Returns action="allow" — this callback only enriches context, never blocks.
    """
    # Set VIP flag from customer tier
    tier = result_dict.get("tier", "")
    if tier == "vip":
        context["vip"] = True

    # Set account_closure flag from customer flags list
    flags = result_dict.get("flags", [])
    if "account_closure" in flags:
        context["account_closure"] = True

    # Scan user message for legal keywords (CCA: deterministic, not LLM confidence)
    user_message = context.get("user_message", "").lower()
    if any(keyword in user_message for keyword in LEGAL_KEYWORDS):
        context["legal_complaint"] = True

    return CallbackResult(action="allow")


def check_policy_callback(
    tool_name: str,
    input_dict: dict,
    result_dict: dict,
    context: dict,
    services: ServiceContainer,
) -> CallbackResult:
    """Set context requires_review flag from policy check result.

    CCA Rule: requires_review is amount > $500 regardless of tier.
    Returns action="allow" — this callback only enriches context, never blocks.
    """
    if result_dict.get("requires_review"):
        context["requires_review"] = True

    return CallbackResult(action="allow")


def escalation_callback(
    tool_name: str,
    input_dict: dict,
    result_dict: dict,
    context: dict,
    services: ServiceContainer,
) -> CallbackResult:
    """Enforce escalation rules for process_refund.

    CCA Rule: Deterministic business rules in code (PostToolUse callbacks), NEVER
    self-reported LLM confidence scores for routing.

    Escalation triggers (any one is sufficient):
    - vip: Customer is VIP tier
    - account_closure: Customer account is flagged for closure
    - legal_complaint: User message contained legal keywords
    - requires_review: Refund amount > $500 threshold

    Returns action="block" with structured error JSON if any trigger is active.
    Returns action="allow" if no escalation conditions are met.
    """
    for flag, reason in ESCALATION_FLAGS.items():
        if context.get(flag):
            blocked_result = {
                "status": "blocked",
                "reason": reason,
                "flag_triggered": flag,
                "action_required": "escalate_to_human",
            }
            return CallbackResult(
                action="block",
                replacement=json.dumps(blocked_result),
                reason=reason,
            )

    return CallbackResult(action="allow")


def _redact_fields(data: dict, fields: tuple[str, ...]) -> tuple[dict, int]:
    """Return a copy of data with card numbers redacted in the named string fields."""
    redacted = dict(data)
    total = 0
    for name in fields:
        value = data.get(name)
        if not isinstance(value, str):
            continue
        new_value, count = CARD_PATTERN.subn(r"****-****-****-\2", value)
        if count:
            redacted[name] = new_value
            total += count
    return redacted, total


def compliance_callback(
    tool_name: str,
    input_dict: dict,
    result_dict: dict,
    context: dict,
    services: ServiceContainer,
) -> CallbackResult:
    """Enforce PII redaction for every free-text field that reaches a store.

    CCA Rule: Programmatic redaction enforces PCI compliance — system prompt instructions
    alone are unreliable. The fields scrubbed per tool are listed in REDACTED_FIELDS.

    Credit card numbers matching NNNN-NNNN-NNNN-NNNN (dash or space separated) are
    replaced with ****-****-****-NNNN (preserving last 4 digits for reference).

    Handles two result shapes:
    - Flat: the fields sit at the top level of result_dict. This is the shape
      dispatch() passes BEFORE the handler runs (input_dict as result_dict), so the
      redacted values can be written to the store instead of the originals.
    - Nested: {"status": ..., "entry": {...}} or {"status": ..., "record": {...}}
      (the handlers' output shape). Redacted nested fields are also exposed at the
      top level for test assertions and audit inspection.

    Returns action="replace_result" with redacted JSON if any card numbers found.
    Returns action="allow" if no PII detected.
    """
    fields = REDACTED_FIELDS.get(tool_name, ALL_REDACTED_FIELDS)

    redacted_result, total_count = _redact_fields(result_dict, fields)

    for key in _NESTED_KEYS:
        nested = result_dict.get(key)
        if not isinstance(nested, dict):
            continue
        redacted_nested, count = _redact_fields(nested, fields)
        if count:
            redacted_result[key] = redacted_nested
            for name in fields:
                if name in nested and redacted_nested[name] != nested[name]:
                    redacted_result[name] = redacted_nested[name]
            total_count += count

    if total_count == 0:
        return CallbackResult(action="allow")

    return CallbackResult(
        action="replace_result",
        replacement=json.dumps(redacted_result),
        reason=f"Redacted {total_count} credit card number(s) from {tool_name} fields",
    )


# ---------------------------------------------------------------------------
# build_callbacks() factory
# ---------------------------------------------------------------------------


def build_callbacks() -> dict[str, CallbackFn]:
    """Build and return the per-tool callback registry.

    CCA Rule: Per-tool dispatch — a callback runs only for the tools it is registered
    under. compliance_callback is registered for every tool that persists free text
    (see REDACTED_FIELDS); the others are registered for exactly one tool.

    Returns:
        Dict mapping tool_name -> callback function.
    """
    return {
        "lookup_customer": lookup_customer_callback,
        "check_policy": check_policy_callback,
        "process_refund": escalation_callback,
        "log_interaction": compliance_callback,
        "escalate_to_human": compliance_callback,
    }
