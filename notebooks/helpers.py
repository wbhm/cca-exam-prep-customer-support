"""Notebook helper utilities for token accounting and result comparison.

Importable by all notebooks via sys.path.insert(0, str(Path(".").resolve())).
"""

from __future__ import annotations

from tabulate import tabulate

# ---------------------------------------------------------------------------
# Claude Sonnet 4.6 pricing (per million tokens) as of 2026-03-25
# Source: https://www.anthropic.com/pricing
# ---------------------------------------------------------------------------
_PRICE_INPUT = 3.00  # $ per 1M input tokens
_PRICE_OUTPUT = 15.00  # $ per 1M output tokens
_PRICE_CACHE_READ = 0.30  # $ per 1M cache-read tokens (10% of input)
_PRICE_CACHE_WRITE = 3.75  # $ per 1M cache-write tokens (125% of input)


def print_usage(response: object, model: str = "claude-sonnet-4-6") -> None:
    """Print a formatted token-usage summary with estimated USD cost.

    Args:
        response: Anthropic messages.create() response object with a .usage attribute.
        model: Model name string to display. Defaults to claude-sonnet-4-6.

    Token accounting:
        total_input = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
        Do NOT double-count — cache fields are not additive on top of input_tokens.
    """
    u = response.usage  # type: ignore[attr-defined]
    inp = u.input_tokens
    out = u.output_tokens
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    total = inp + out + cr + cw

    # Estimated cost
    cost = (
        inp * _PRICE_INPUT / 1_000_000
        + out * _PRICE_OUTPUT / 1_000_000
        + cr * _PRICE_CACHE_READ / 1_000_000
        + cw * _PRICE_CACHE_WRITE / 1_000_000
    )

    print(f"\n{'Token Usage':\u2500<40}")
    print(f"  {'Input tokens:':<28} {inp:>8,}")
    print(f"  {'Output tokens:':<28} {out:>8,}")
    if cr:
        print(f"  {'Cache read tokens:':<28} {cr:>8,}")
    if cw:
        print(f"  {'Cache write tokens:':<28} {cw:>8,}")
    print(f"  {'Total tokens:':<28} {total:>8,}")
    print(f"  {'Model:':<28} {model}")
    print(f"  {'Estimated cost:':<28} ${cost:.6f}")


def compare_results(anti_result: dict, correct_result: dict) -> None:
    """Print a side-by-side comparison table of anti-pattern vs correct-pattern results.

    Args:
        anti_result:    Dict of metric_name -> value for the anti-pattern implementation.
        correct_result: Dict of metric_name -> value for the correct implementation.

    Supports:
        - Numeric values: shows percentage change from anti to correct.
          If anti value is 0, delta shows "N/A" to avoid division by zero.
        - Boolean values: shows True/False; delta shows FIXED, REGRESSED, or same.

    Table format: "simple" (tabulate built-in, clean for Jupyter notebook output).
    """
    rows = []
    all_keys = list(anti_result.keys()) + [k for k in correct_result if k not in anti_result]

    for key in all_keys:
        anti_val = anti_result.get(key, "N/A")
        correct_val = correct_result.get(key, "N/A")

        if isinstance(anti_val, bool) or isinstance(correct_val, bool):
            # Boolean metric
            if anti_val is False and correct_val is True:
                delta = "FIXED"
            elif anti_val is True and correct_val is False:
                delta = "REGRESSED"
            else:
                delta = "same"
        elif isinstance(anti_val, (int, float)) and isinstance(correct_val, (int, float)):
            # Numeric metric
            if anti_val == 0:
                delta = "N/A"
            else:
                pct = (correct_val - anti_val) / abs(anti_val) * 100
                sign = "+" if pct >= 0 else ""
                delta = f"{sign}{pct:.1f}%"
        else:
            delta = "-"

        rows.append([key, anti_val, correct_val, delta])

    headers = ["Metric", "Anti-Pattern", "Correct", "Delta"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


# ---------------------------------------------------------------------------
# Scripted client for deterministic replays (no API calls)
#
# A live model mostly behaves well, so a guarantee cannot be demonstrated by
# sampling it. These helpers replay a fixed transcript through run_agent_loop
# so the exact turn where two designs diverge is visible on every run.
# ---------------------------------------------------------------------------

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )


def tool_turn(name: str, input_dict: dict, tool_id: str) -> SimpleNamespace:
    """One scripted assistant turn in which Claude calls a single tool."""
    block = SimpleNamespace(type="tool_use", name=name, input=input_dict, id=tool_id)
    return SimpleNamespace(stop_reason="tool_use", content=[block], usage=_usage())


def text_turn(text: str) -> SimpleNamespace:
    """One scripted assistant turn in which Claude replies with text and stops."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(stop_reason="end_turn", content=[block], usage=_usage())


def escalation_turn(customer_id: str, amount: float, reason: str) -> SimpleNamespace:
    """A scripted escalate_to_human call, used as the reply to a forced tool_choice."""
    return tool_turn(
        "escalate_to_human",
        {
            "customer_id": customer_id,
            "customer_tier": "regular",
            "issue_type": "refund",
            "disputed_amount": amount,
            "escalation_reason": reason,
            "recommended_action": "Human agent to review refund request",
            "conversation_summary": f"Customer {customer_id} requested ${amount:.0f} refund",
            "turns_elapsed": 3,
        },
        "toolu_forced",
    )


def scripted_client(transcript: list, on_forced: SimpleNamespace) -> SimpleNamespace:
    """Build a fake Anthropic client that replays `transcript` turn by turn.

    Any call that pins `tool_choice` (the agent loop's forced escalation) returns
    `on_forced` instead of the next transcript turn. The same transcript can
    therefore be replayed under the anti-pattern (which never forces a call) and
    the correct pattern (which does when a rule requires it). Calls made after
    the transcript is exhausted also return `on_forced`. Every call's kwargs are
    recorded in `.calls` for inspection.
    """
    remaining = list(transcript)
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if "tool_choice" in kwargs or not remaining:
            return on_forced
        return remaining.pop(0)

    client = SimpleNamespace(messages=SimpleNamespace(create=create), calls=calls)
    return client


def tool_result_for(result: object, tool_name: str) -> dict | None:
    """Return the parsed JSON tool_result that the loop sent back for `tool_name`.

    Looks up the tool_use id from result.tool_calls, then finds the matching
    tool_result block in result.messages. Returns None if the tool was never called.
    """
    ids = [tc["id"] for tc in result.tool_calls if tc["name"] == tool_name]  # type: ignore[attr-defined]
    if not ids:
        return None
    for msg in result.messages:  # type: ignore[attr-defined]
        if msg["role"] != "user" or not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("tool_use_id") == ids[0]:
                return json.loads(block["content"])
    return None
