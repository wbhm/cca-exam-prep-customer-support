# CCA Customer Support Resolution Agent: A Deep Tutorial

This tutorial walks through every module in the Customer Support Resolution Agent project, explaining the architecture, the code, and — critically — the **six CCA exam patterns** each piece demonstrates. By the end, you will understand how to build a production-quality Claude-powered agent that enforces business rules programmatically, manages costs, and hands off to humans safely.

The project has two layers:

1. **The Python package** (`src/customer_service/`) — production-quality implementation with only correct patterns.
2. **The anti-patterns** (`src/customer_service/anti_patterns/`) — deliberately wrong implementations that demonstrate what happens when you ignore CCA guidance.

We will cover both, because understanding *why* the wrong way fails is how you internalize the right way before exam day.

---

## Table of Contents

1. [Project Architecture Overview](#1-project-architecture-overview)
2. [Data Models — The Foundation](#2-data-models--the-foundation)
3. [Services — Simulated Business Systems](#3-services--simulated-business-systems)
4. [Seed Data — Customers and Scenarios](#4-seed-data--customers-and-scenarios)
5. [Tool Definitions — Telling Claude What It Can Do](#5-tool-definitions--telling-claude-what-it-can-do)
6. [Tool Handlers — Executing Tool Calls](#6-tool-handlers--executing-tool-calls)
7. [Pattern 1: Escalation — Deterministic Rules vs. LLM Confidence](#7-pattern-1-escalation--deterministic-rules-vs-llm-confidence)
8. [Pattern 2: Compliance — Programmatic Hooks vs. Prompt Instructions](#8-pattern-2-compliance--programmatic-hooks-vs-prompt-instructions)
9. [Pattern 3: Tool Design — 5 Focused Tools vs. 15-Tool Swiss Army Knife](#9-pattern-3-tool-design--5-focused-tools-vs-15-tool-swiss-army-knife)
10. [Pattern 4: Context Management — Structured Summaries vs. Raw Transcripts](#10-pattern-4-context-management--structured-summaries-vs-raw-transcripts)
11. [Pattern 5: Cost Optimization — Prompt Caching vs. Batch API](#11-pattern-5-cost-optimization--prompt-caching-vs-batch-api)
12. [Pattern 6: Handoffs — Structured Records vs. Raw Conversation Dumps](#12-pattern-6-handoffs--structured-records-vs-raw-conversation-dumps)
13. [The Agent Loop — Putting It All Together](#13-the-agent-loop--putting-it-all-together)
14. [The Coordinator — Multi-Topic Queries](#14-the-coordinator--multi-topic-queries)
15. [Notebook Helpers — Cost Tracking and Comparison](#15-notebook-helpers--cost-tracking-and-comparison)
16. [How the Pieces Connect — End-to-End Data Flow](#16-how-the-pieces-connect--end-to-end-data-flow)

---

## 1. Project Architecture Overview

```
src/customer_service/
  models/          Pydantic data models (the nouns)
  services/        5 simulated business services (the verbs)
  data/            Seed customers and test scenarios
  tools/           Claude API tool schemas + per-tool handlers
  agent/           The agentic loop, callbacks, prompts, context, coordinator
  anti_patterns/   6 deliberately wrong implementations
```

The data flows like this:

```
Customer message
  -> agent_loop.py calls client.messages.create()
  -> Claude returns tool_use blocks
  -> callbacks.py validates each tool call against business rules
  -> if approved: handlers.py dispatches to the correct tool handler -> service call
  -> if blocked: structured 'blocked' result with action_required=escalate_to_human
     -> agent_loop.py forces escalate_to_human with tool_choice
  -> loop continues until stop_reason != 'tool_use'
  -> at turn end: if an escalation flag is set and nothing is queued, force escalation
```

Every layer enforces a CCA principle:

| Layer | CCA Principle |
|-------|--------------|
| Models | Type safety via Pydantic — invalid data is rejected at construction |
| Services | Deterministic business logic — no LLM reasoning in policy checks |
| Tools | Exactly 5 focused tools with negative-bound descriptions |
| Callbacks | Programmatic enforcement — business rules in code, not prompts |
| Agent Loop | Stop-reason-controlled loop with forced escalation on blocked refunds |
| Coordinator | Context isolation — subagents see only explicit context strings |

---

## 2. Data Models — The Foundation

**File:** `src/customer_service/models/customer.py`

Every data structure in the system is a Pydantic `BaseModel`. This is not optional — Pydantic provides runtime type validation, which means invalid data fails at construction time rather than silently corrupting downstream logic.

### CustomerTier

```python
class CustomerTier(StrEnum):
    BASIC = "basic"
    REGULAR = "regular"
    PREMIUM = "premium"
    VIP = "vip"
```

`StrEnum` (Python 3.11+) means each variant is both an enum member *and* a string. You can pass `CustomerTier.VIP` anywhere a string is expected, and it serializes cleanly to JSON. The tier drives policy limits — BASIC/REGULAR get $100, PREMIUM gets $500, VIP gets $5,000.

### CustomerProfile

```python
class CustomerProfile(BaseModel):
    customer_id: str
    name: str
    email: str
    tier: CustomerTier
    account_open: bool = True
    flags: list[str] = Field(default_factory=list)
```

The `flags` field is where non-tier escalation triggers live. A customer flagged with `"account_closure"` triggers immediate escalation regardless of refund amount. This is a deliberate design choice: the escalation logic in `callbacks.py` reads flags from the profile, making the trigger *data-driven* rather than hardcoded to specific customer IDs.

### RefundRequest

```python
class RefundRequest(BaseModel):
    customer_id: str
    order_id: str
    amount: float = Field(gt=0)
    reason: str
```

Notice `gt=0` on the amount field. Pydantic enforces this at construction — you cannot create a `RefundRequest(amount=-50)`. This is boundary validation at the model layer, exactly where it belongs.

### PolicyResult

```python
class PolicyResult(BaseModel):
    approved: bool
    limit: float
    requires_review: bool
```

Three booleans from the PolicyEngine: is this within the tier limit? What is the limit? Does it exceed the $500 review threshold? The `requires_review` flag is independent of `approved` — a VIP requesting a $4,000 refund is approved (under the $5,000 limit) but still requires review (above $500).

### EscalationRecord

```python
class EscalationRecord(BaseModel):
    customer_id: str
    customer_tier: str
    issue_type: str
    disputed_amount: float
    escalation_reason: str
    recommended_action: str
    conversation_summary: str
    turns_elapsed: int
```

This is the CCA handoff pattern in data form. When a case escalates to a human agent, they receive exactly these 8 fields — not a raw conversation dump. Every field is purposeful:

- `customer_tier` tells the human agent what service level applies
- `escalation_reason` explains *why* the AI couldn't handle it
- `recommended_action` gives the human a starting point
- `turns_elapsed` signals conversation fatigue

### InteractionLog

```python
class InteractionLog(BaseModel):
    customer_id: str
    action: str
    details: str
    timestamp: str
```

Every tool call gets logged. The `details` field is a JSON string — not a raw dict — because the compliance callback needs to regex-scan it for PII *before* it hits the audit log. If details contained a nested Pydantic model, the redaction would need to understand model structure. A JSON string keeps redaction simple and reliable.

---

## 3. Services — Simulated Business Systems

**Directory:** `src/customer_service/services/`

The project simulates five business services that a real customer support system would integrate with. All are in-memory — students need zero infrastructure setup.

### CustomerDatabase

```python
class CustomerDatabase:
    def __init__(self, customers: dict[str, CustomerProfile]) -> None:
        self._customers = {k: v.model_copy() for k, v in customers.items()}

    def get_customer(self, customer_id: str) -> CustomerProfile | None:
        profile = self._customers.get(customer_id)
        return profile.model_copy() if profile is not None else None
```

Two defensive copies here. The constructor copies each profile so the original `CUSTOMERS` dict is never mutated. The `get_customer` method returns a copy so callers cannot accidentally modify the database. This prevents cross-scenario contamination when running multiple notebook cells.

Why not just use a raw dict? Because `CustomerDatabase` establishes the *contract*: you look up customers by ID, you get a `CustomerProfile` or `None`. This is the interface that tool handlers depend on. If we later replaced this with a real database, only this class changes.

### PolicyEngine

```python
class PolicyEngine:
    _REFUND_LIMITS: dict[CustomerTier, float] = {
        CustomerTier.BASIC: 100.0,
        CustomerTier.REGULAR: 100.0,
        CustomerTier.PREMIUM: 500.0,
        CustomerTier.VIP: 5000.0,
    }
    _REVIEW_THRESHOLD = 500.0

    def check_policy(self, tier: CustomerTier, requested_amount: float) -> PolicyResult:
        limit = self._REFUND_LIMITS[tier]
        approved = requested_amount <= limit
        requires_review = requested_amount > self._REVIEW_THRESHOLD
        return PolicyResult(approved=approved, limit=limit, requires_review=requires_review)
```

This is pure deterministic logic. No LLM reasoning, no probability, no prompt. Given a tier and amount, the result is always the same. This is the CCA escalation principle in its purest form: **business rules belong in code, not in system prompts.**

The `_REVIEW_THRESHOLD` is 500.0 and applies to *all* tiers. A VIP requesting $4,000 is approved (under $5,000 limit) but still flagged for review (above $500). The escalation callback in `callbacks.py` reads `requires_review` and blocks the refund, forcing escalation.

### FinancialSystem

```python
class FinancialSystem:
    def __init__(self) -> None:
        self._processed: list[dict] = []

    def process_refund(self, customer_id, order_id, amount, policy_approved=True) -> dict:
        if policy_approved:
            result = {"status": "approved", ..., "refund_id": f"REF-{len(self._processed) + 1:04d}"}
        else:
            result = {"status": "rejected", ..., "reason": "Policy check failed"}
        self._processed.append(result)
        return result
```

The FinancialSystem *trusts the caller*. It does not re-check policy — it accepts a `policy_approved` boolean and acts accordingly. This is a deliberate design decision: the PolicyEngine is the single source of truth for policy, and FinancialSystem is the single source of truth for financial state. Separation of concerns.

The `_processed` list is the persistent state that tests verify. When we test that a blocked refund doesn't write to FinancialSystem, we check `len(services.financial_system.get_processed()) == 0`. This is the behavior-first testing principle: **test the store, not the API response.**

### EscalationQueue

```python
class EscalationQueue:
    def __init__(self) -> None:
        self._queue: list[EscalationRecord] = []

    def add_escalation(self, record: EscalationRecord) -> None:
        self._queue.append(record)
```

Simple append-only queue. When the `escalate_to_human` tool fires, the structured `EscalationRecord` lands here. Tests verify the queue has the expected entries with the expected fields.

### AuditLog

```python
class AuditLog:
    def __init__(self) -> None:
        self._entries: list[InteractionLog] = []

    def log(self, entry: InteractionLog) -> None:
        self._entries.append(entry)
```

Append-only compliance trail. The compliance callback redacts PII *before* entries reach this log. Tests verify that no credit card numbers appear in `audit_log.get_entries()` — they check the store, not the tool response.

### ServiceContainer

```python
@dataclass(frozen=True)
class ServiceContainer:
    customer_db: CustomerDatabase
    policy_engine: PolicyEngine
    financial_system: FinancialSystem
    escalation_queue: EscalationQueue
    audit_log: AuditLog
```

A frozen dataclass holding all five services. `frozen=True` means you cannot reassign fields after construction — the container is immutable. Every tool handler receives this single object, and accesses exactly the services it needs. Services are never imported directly in tool modules.

This is dependency injection. If you want to swap `CustomerDatabase` for a real database in production, you construct a different `ServiceContainer`. The tool handlers don't change.

---

## 4. Seed Data — Customers and Scenarios

**Directory:** `src/customer_service/data/`

### CUSTOMERS

```python
CUSTOMERS: dict[str, CustomerProfile] = {
    "C001": CustomerProfile(customer_id="C001", name="Alice Johnson",
                            email="alice@example.com", tier=CustomerTier.REGULAR),
    "C002": CustomerProfile(customer_id="C002", name="Bob Chen",
                            email="bob@example.com", tier=CustomerTier.VIP),
    "C003": CustomerProfile(customer_id="C003", name="Carol Martinez",
                            email="carol@example.com", tier=CustomerTier.REGULAR),
    "C004": CustomerProfile(customer_id="C004", name="David Kim",
                            email="david@example.com", tier=CustomerTier.REGULAR,
                            flags=["account_closure"]),
    "C005": CustomerProfile(customer_id="C005", name="Eva Nowak",
                            email="eva@example.com", tier=CustomerTier.REGULAR),
    "C006": CustomerProfile(customer_id="C006", name="Frank Osei",
                            email="frank@example.com", tier=CustomerTier.VIP,
                            flags=["account_closure"]),
}
```

Six customers, each designed to trigger specific escalation paths:

| ID | Tier | Flags | Purpose |
|----|------|-------|---------|
| C001 | Regular | — | Happy path ($50 refund within $100 limit) |
| C002 | VIP | — | VIP escalation trigger |
| C003 | Regular | — | Amount threshold ($600 > $500 review) |
| C004 | Regular | account_closure | Account closure escalation |
| C005 | Regular | — | Legal keyword escalation (in message) |
| C006 | VIP | account_closure | Multi-trigger (VIP + closure + amount) |

### SCENARIOS

```python
SCENARIOS: dict[str, dict] = {
    "happy_path": {
        "customer_id": "C001",
        "message": "I'd like a $50 refund for order #ORD-001. The item was defective.",
        "expected_tools": ["lookup_customer", "check_policy", "process_refund", "log_interaction"],
        "expected_outcome": "refund_approved",
    },
    "amount_threshold": {
        "customer_id": "C003",
        "message": "I need a $600 refund for my damaged order.",
        "expected_tools": ["lookup_customer", "check_policy", "escalate_to_human"],
        "expected_outcome": "escalated_amount",
    },
    # ... 4 more scenarios
}
```

Each scenario documents its expected tool chain and outcome. This makes scenarios both teaching artifacts (students can trace the expected flow) and test oracles (automated tests can verify the actual flow matches).

Notice the `amount_threshold` scenario: $600 for a REGULAR customer. The $100 tier limit means `approved=False`. The $500 review threshold means `requires_review=True`. Both trigger escalation. The scenario message now includes the customer ID prefix so Claude calls `lookup_customer` immediately without asking.

---

## 5. Tool Definitions — Telling Claude What It Can Do

**File:** `src/customer_service/tools/definitions.py`

This file defines the 5 tools the agent can use. The CCA exam tests whether you know the right number (4-5 per agent) and whether your descriptions include negative bounds.

### Schema Generation from Pydantic

```python
class LookupCustomerInput(BaseModel):
    customer_id: str = Field(description="Customer ID to look up (e.g., 'C001')")

def _make_tool(name: str, description: str, model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)  # Claude API rejects top-level 'title'
    return {"name": name, "description": description, "input_schema": schema}
```

Each tool's input schema is a Pydantic model. The `_make_tool` helper converts these to Claude API format. The `schema.pop("title", None)` is important — Pydantic's `model_json_schema()` adds a top-level `"title"` key that the Claude API does not accept.

### Negative-Bound Descriptions

```python
LOOKUP_CUSTOMER_TOOL = _make_tool(
    name="lookup_customer",
    description=(
        "Look up customer profile by ID. Returns customer tier, account status, and flags. "
        "does NOT modify customer data or process any requests."
    ),
    model=LookupCustomerInput,
)
```

Every tool description says what the tool does *and what it does not do*. This is the CCA negative-bound pattern. Without "does NOT modify customer data," Claude might call `lookup_customer` when it wants to update a profile. Without "does NOT check policy eligibility — use check_policy first" on `process_refund`, Claude might skip the policy check.

The `does NOT` phrases use lowercase `does` — this is a deliberate style choice that matches how the CCA exam phrases its tool descriptions.

### The 5-Tool Set

```python
TOOLS: list[dict] = [
    LOOKUP_CUSTOMER_TOOL,    # Read customer data
    CHECK_POLICY_TOOL,       # Evaluate refund eligibility
    PROCESS_REFUND_TOOL,     # Execute approved refund
    ESCALATE_TO_HUMAN_TOOL,  # Transfer to human queue
    LOG_INTERACTION_TOOL,    # Compliance audit trail
]
```

Exactly 5 tools. The CCA exam guidance is 4-5 focused tools per agent. If you need more, use the coordinator-subagent pattern (covered in section 14). The anti-pattern alternative (15+ tools) degrades Claude's tool selection accuracy — more on this in Pattern 3.

---

## 6. Tool Handlers — Executing Tool Calls

**Directory:** `src/customer_service/tools/`

Each tool has its own handler module. All handlers follow the same signature:

```python
def handle_<tool_name>(input_dict: dict, services: ServiceContainer) -> str:
```

Input is a dict (from Claude's `tool_use` block's `input` field). Output is always a JSON string — matching the Claude API's `tool_result` content format.

### lookup_customer.py

```python
def handle_lookup_customer(input_dict: dict, services: ServiceContainer) -> str:
    customer_id = input_dict.get("customer_id", "")
    customer = services.customer_db.get_customer(customer_id)
    if customer is None:
        return json.dumps({"error": f"Customer not found: {customer_id}"})
    return json.dumps(customer.model_dump())
```

Straightforward: look up, return JSON. The `.model_dump()` converts the Pydantic model to a dict, then `json.dumps` serializes it. Errors return structured JSON with an `"error"` key — never raw exceptions.

### check_policy.py

```python
def handle_check_policy(input_dict: dict, services: ServiceContainer) -> str:
    customer_id = input_dict.get("customer_id", "")
    customer = services.customer_db.get_customer(customer_id)
    if customer is None:
        return json.dumps({"error": f"Customer not found: {customer_id}"})
    requested_amount = input_dict.get("requested_amount", 0.0)
    result = services.policy_engine.check_policy(customer.tier, requested_amount)
    return json.dumps(result.model_dump())
```

Looks up the customer (to get the tier), then delegates to `PolicyEngine`. The handler does not contain policy logic — it is a thin adapter between Claude's tool call and the service layer.

### process_refund.py — The Two-Step Vetoable Pattern

This is the most architecturally interesting handler. It implements a two-step process:

```python
def propose_refund(input_dict: dict, services: ServiceContainer) -> dict:
    """Step 1: Compute result WITHOUT writing to FinancialSystem."""
    customer_id = input_dict.get("customer_id", "")
    customer = services.customer_db.get_customer(customer_id)
    if customer is None:
        return {"error": f"Customer not found: {customer_id}"}
    order_id = input_dict.get("order_id", "")
    amount = input_dict.get("amount", 0.0)
    policy_result = services.policy_engine.check_policy(customer.tier, amount)
    return {
        "status": "proposed",
        "customer_id": customer_id,
        "order_id": order_id,
        "amount": amount,
        "policy_approved": policy_result.approved,
        "requires_review": policy_result.requires_review,
    }

def commit_refund(customer_id, order_id, amount, policy_approved, services) -> str:
    """Step 2: Write to FinancialSystem (only if callback allows)."""
    result = services.financial_system.process_refund(
        customer_id=customer_id, order_id=order_id,
        amount=amount, policy_approved=policy_approved,
    )
    return json.dumps(result)
```

Why two steps? Because the callback needs to inspect the *proposed* result before any financial write occurs. If the callback decides to block (e.g., amount > $500 requires review), the `commit_refund` step never runs. The FinancialSystem is never written to. This is the **CCA veto guarantee**: a blocked refund leaves zero trace in the financial system.

The `handle_process_refund` function is the simple path (no callbacks):

```python
def handle_process_refund(input_dict: dict, services: ServiceContainer) -> str:
    proposed = propose_refund(input_dict, services)
    if "error" in proposed:
        return json.dumps(proposed)
    return commit_refund(...)
```

When callbacks are active, the `dispatch()` function in `handlers.py` uses `_dispatch_process_refund_with_callback()` instead.

### escalate_to_human.py

```python
def handle_escalate_to_human(input_dict: dict, services: ServiceContainer) -> str:
    record = EscalationRecord(
        customer_id=input_dict["customer_id"],
        customer_tier=input_dict["customer_tier"],
        issue_type=input_dict["issue_type"],
        disputed_amount=input_dict["disputed_amount"],
        escalation_reason=input_dict["escalation_reason"],
        recommended_action=input_dict["recommended_action"],
        conversation_summary=input_dict["conversation_summary"],
        turns_elapsed=input_dict["turns_elapsed"],
    )
    services.escalation_queue.add_escalation(record)
    return json.dumps({"status": "escalated", "record": record.model_dump()})
```

Creates a structured `EscalationRecord` and adds it to the queue. The input fields come from Claude — the agent fills in all 8 fields based on conversation context. Compare this to the anti-pattern in section 12, which dumps raw conversation JSON.

### log_interaction.py

```python
def handle_log_interaction(input_dict: dict, services: ServiceContainer) -> str:
    entry = InteractionLog(
        customer_id=input_dict["customer_id"],
        action=input_dict["action"],
        details=input_dict["details"],
        timestamp=datetime.now(UTC).isoformat(),
    )
    services.audit_log.log(entry)
    return json.dumps({"status": "logged", "entry": entry.model_dump()})
```

Logs an interaction for compliance. The `details` field may contain PII — the compliance callback redacts it *before* this handler runs.

### The Dispatch Registry

**File:** `src/customer_service/tools/handlers.py`

```python
DISPATCH: dict[str, Callable[[dict, ServiceContainer], str]] = {
    "lookup_customer": handle_lookup_customer,
    "check_policy": handle_check_policy,
    "process_refund": handle_process_refund,
    "escalate_to_human": handle_escalate_to_human,
    "log_interaction": handle_log_interaction,
}
```

A dict mapping tool names to handler functions. Dict-based dispatch is deterministic and auditable — you can see every tool and its handler in one place.

The `dispatch()` function adds callback support:

```python
def dispatch(tool_name, input_dict, services, context=None, callbacks=None) -> str:
    handler = DISPATCH.get(tool_name)
    if handler is None:
        return json.dumps({"status": "error", "error_type": "unknown_tool", ...})

    # Special case: process_refund with callback uses two-step dispatch
    if tool_name == "process_refund" and callbacks and "process_refund" in callbacks:
        return _dispatch_process_refund_with_callback(input_dict, services, ctx, callbacks["process_refund"])

    # Special case: log_interaction callback runs BEFORE handler (pre-handler redaction)
    if tool_name == "log_interaction" and callbacks and "log_interaction" in callbacks:
        # Redact PII in input_dict["details"] before handler writes to audit log
        ...

    # Standard: run handler, then optional post-handler callback
    result = handler(input_dict, services)
    if callbacks and tool_name in callbacks:
        cb_result = callbacks[tool_name](tool_name, input_dict, result_dict, ctx, services)
        if cb_result.action == "replace_result":
            return cb_result.replacement
    return result
```

Three dispatch patterns:
1. **process_refund**: propose -> callback -> commit/block (two-step vetoable)
2. **log_interaction**: callback -> handler (pre-handler redaction)
3. **Everything else**: handler -> callback (post-handler inspection)

Error handling returns structured JSON with CCA-required fields (`status`, `error_type`, `source`, `retry_eligible`, `fallback_available`, `partial_data`).

---

## 7. Pattern 1: Escalation — Deterministic Rules vs. LLM Confidence

This is the most important CCA pattern. The exam question typically presents two choices: (a) let Claude self-assess its confidence and escalate when uncertain, or (b) use deterministic business rules in code to decide when to escalate. The correct answer is always (b).

### The Anti-Pattern: Confidence-Based Escalation

**File:** `src/customer_service/anti_patterns/confidence_escalation.py`

```python
CONFIDENCE_SYSTEM_PROMPT: str = (
    "You are a customer support agent for an online retail company. "
    "..."  # same tool list and instructions as get_system_prompt(), plus:
    "ESCALATION RULE:\n"
    "Before taking any action on a refund or escalation, rate your confidence "
    "from 0-100 that you can handle this case correctly without human assistance. "
    "If your confidence is below 70, use escalate_to_human to transfer the case. "
    "If your confidence is 70 or above, proceed to handle the case yourself, "
    "including processing refunds directly.\n\n"
    "..."
)
```

The problem is not that Claude reports high confidence and pays out. Measured over 35 live runs of the $600 refund for Regular customer C003, it never processed the refund. It escalated 33 times, and on 2 runs it ended its turn with a clarifying question (asking for an order ID) and never called `escalate_to_human` or `log_interaction`. The customer received a polite, professional reply. The escalation queue was empty. No human was notified.

Two things drive that result. First, the prompt tells Claude to check policy, and `check_policy` returns `{"approved": false, "limit": 100.0, "requires_review": true}` for this case, so Claude usually escalates on the strength of the tool result rather than its confidence score. Second, in a single-turn agent loop, a clarifying question is a terminal state: nothing in code notices that a $600 case just ended without reaching a human. The confidence rating never determines the outcome either way. Claude does not know your business rules unless you enforce them in code, and even when a tool happens to tell it the rule, nothing guarantees it acts on it.

The `run_confidence_agent()` function runs the agent with this prompt and the standard 5 tools (no callbacks):

```python
def run_confidence_agent(
    client: object,
    services: ServiceContainer,
    user_message: str,
    model: str = "claude-sonnet-4-6",
) -> AgentResult:
    return run_agent_loop(
        client=client,
        services=services,
        user_message=user_message,
        system_prompt=CONFIDENCE_SYSTEM_PROMPT,
        model=model,
    )
    # NOTE: No callbacks passed. This is the anti-pattern.
```

Without callbacks, there is no programmatic check on the outcome. If Claude attempts `process_refund`, the handler runs. For C003 the handler's own tier check rejects the amount, so the money does not go out in this codebase, but nothing escalates and the case is dropped. If Claude instead stops to ask a question, the case is dropped the same way. A control that holds on 95% of runs is not a control, and this one fails quietly rather than loudly.

### The Correct Pattern: Deterministic Callback Rules

**File:** `src/customer_service/agent/callbacks.py`

The escalation callback implements four deterministic rules as a flag table. The same table is used by the agent loop's turn-end check:

```python
ESCALATION_FLAGS: dict[str, str] = {
    "vip": "VIP account requires human review",
    "account_closure": "Account closure in progress requires human review",
    "legal_complaint": "Legal complaint detected — escalate immediately",
    "requires_review": "Refund amount exceeds $500 review threshold",
}

def escalation_callback(
    tool_name: str, input_dict: dict, result_dict: dict,
    context: dict, services: ServiceContainer,
) -> CallbackResult:
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
```

Each rule checks a context flag that was set by an earlier callback. The `lookup_customer_callback` sets `vip`, `account_closure`, and `legal_complaint` flags when it inspects the customer profile and the original user message:

```python
def lookup_customer_callback(
    tool_name: str, input_dict: dict, result_dict: dict,
    context: dict, services: ServiceContainer,
) -> CallbackResult:
    tier = result_dict.get("tier", "")
    if tier == "vip":
        context["vip"] = True

    flags = result_dict.get("flags", [])
    if "account_closure" in flags:
        context["account_closure"] = True

    user_msg = context.get("user_message", "").lower()
    if any(kw in user_msg for kw in LEGAL_KEYWORDS):
        context["legal_complaint"] = True

    return CallbackResult(action="allow")
```

And the `check_policy_callback` sets `requires_review`:

```python
def check_policy_callback(
    tool_name: str, input_dict: dict, result_dict: dict,
    context: dict, services: ServiceContainer,
) -> CallbackResult:
    if result_dict.get("requires_review"):
        context["requires_review"] = True
    return CallbackResult(action="allow")
```

The flow is:
1. Claude calls `lookup_customer` -> `lookup_customer_callback` sets VIP/closure/legal flags
2. Claude calls `check_policy` -> `check_policy_callback` sets `requires_review` flag
3. Claude calls `process_refund` -> `escalation_callback` checks all flags, blocks if any are set
4. Claude receives a `"blocked"` result with `action_required: "escalate_to_human"`
5. The agent loop detects this and forces a `tool_choice` of `escalate_to_human`

There is a second enforcement point. A PostToolUse callback only runs after a tool call, so it cannot catch the run where Claude never calls `process_refund` and instead ends its turn with a question. When Claude stops, the loop checks the same flag table (`ESCALATION_FLAGS` in `callbacks.py`) against the escalation queue. If a flag is set and nothing has been queued, the loop appends an `escalation_required` notice and forces `escalate_to_human` the same way. Claude's customer-facing text is kept on the result.

No LLM reasoning is involved in the escalation decision. The rules are deterministic and testable, and they are checked both after the refund attempt and at turn end.

### What a live run actually shows

On a typical live run the correct pattern looks the same as the anti-pattern: Claude reads the policy result and escalates voluntarily, and the callback is never exercised. That is expected. The guarantee is about the atypical run, and you cannot summon one from a live model on demand. Notebook 01 therefore replays two fixed transcripts through `run_agent_loop` with a scripted client, no API calls: one where Claude asks for an order ID and stops, and one where Claude attempts the refund. Under the anti-pattern both end with an empty escalation queue. Under the correct pattern both end with one queued record, every time.

### The Callback Registry

```python
def build_callbacks() -> dict[str, CallbackFn]:
    return {
        "lookup_customer": lookup_customer_callback,
        "check_policy": check_policy_callback,
        "process_refund": escalation_callback,
        "log_interaction": compliance_callback,
    }
```

Per-tool dispatch. The registry takes no arguments; the loop creates the shared `context` dict itself and passes it to every callback. Each callback fires only for its registered tool. This prevents cross-tool bugs — a callback meant for `process_refund` cannot accidentally fire on `lookup_customer`.

### CallbackResult

```python
@dataclass
class CallbackResult:
    action: Literal["allow", "replace_result", "block"]
    replacement: str | None = None   # required for "block" and "replace_result"
    reason: str | None = None        # human-readable, for logging
```

Three possible actions:
- **allow**: tool execution proceeds normally
- **replace_result**: substitute the result returned to Claude (used for PII redaction)
- **block**: veto the tool call entirely (used for escalation)

---

## 8. Pattern 2: Compliance — Programmatic Hooks vs. Prompt Instructions

### The Anti-Pattern: Prompt-Only Compliance

**File:** `src/customer_service/anti_patterns/prompt_compliance.py`

```python
PROMPT_COMPLIANCE_SYSTEM_PROMPT: str = (
    "You are a customer support agent for an online retail company. "
    "..."  # same tool list and instructions as get_system_prompt(), plus:
    "IMPORTANT COMPLIANCE RULE: Never log credit card numbers, Social Security "
    "numbers, or other personally identifiable information (PII) in interaction "
    "logs. Always redact sensitive data before calling log_interaction. "
    "Replace credit card numbers with the format ****-****-****-NNNN where NNNN "
    "is the last four digits. Failure to redact PII is a compliance violation.\n\n"
    "..."
)
```

The problem: Claude follows these instructions on nearly every live run, which is exactly why a live demo cannot show the difference. But "nearly every" is not "every," and PCI-DSS does not accept "usually compliant." Notebook 02 replays a fixed transcript in which the card is written into a log call, so the one run that matters can be shown on demand.

### The Correct Pattern: Programmatic PII Redaction

**File:** `src/customer_service/agent/callbacks.py`

```python
CARD_PATTERN = re.compile(r"\b(\d{4}[-\s]\d{4}[-\s]\d{4}[-\s])(\d{4})\b")

def compliance_callback(
    tool_name: str, input_dict: dict, result_dict: dict,
    context: dict, services: ServiceContainer,
) -> CallbackResult:
    total_count = 0
    redacted_result = dict(result_dict)

    # Flat shape: {"details": "..."} — what dispatch passes before the handler runs
    if "details" in result_dict:
        redacted_details, count = CARD_PATTERN.subn(r"****-****-****-\2", result_dict["details"])
        if count > 0:
            redacted_result["details"] = redacted_details
            total_count += count

    # Nested shape: {"status": "logged", "entry": {"details": "..."}} — handler output
    entry = result_dict.get("entry")
    if isinstance(entry, dict) and "details" in entry:
        ...  # same substitution on entry["details"]

    if total_count == 0:
        return CallbackResult(action="allow")
    return CallbackResult(
        action="replace_result",
        replacement=json.dumps(redacted_result),
        reason=f"Redacted {total_count} credit card number(s) from log details",
    )
```

The regex `\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b` matches credit card numbers in any common format (with spaces, dashes, or neither). The replacement preserves only the last 4 digits.

This callback runs *before* the `log_interaction` handler writes to the audit log. The dispatch function replaces `input_dict["details"]` with the redacted version, then calls the handler. The audit log never sees the raw card number.

The key insight: **the system prompt still tells Claude to redact PII** (good for the common case), but the callback guarantees it (catches the edge case). Defense in depth: prompt for guidance, code for enforcement.

### Why Pre-Handler, Not Post-Handler?

Most callbacks run *after* the handler. The compliance callback is special — it runs *before*. Here is why:

The pattern requires separators between the four groups, so `4111-1111-1111-1234` and `4111 1111 1111 1234` are redacted while an unseparated `4111111111111234` passes through. Notebook 02 shows that limit; widening the regex is the fix.

If the callback ran after `log_interaction`, the handler would have already written the raw card number to the `AuditLog`. Even if the callback then returned a redacted result to Claude, the damage is done — the audit log contains PII. By running the callback first, the handler receives already-redacted input. The audit log is clean from the start.

This is the behavior-first testing principle applied to architecture: **PII must never reach the audit log — redact before write, not after.**

---

## 9. Pattern 3: Tool Design — 5 Focused Tools vs. 15-Tool Swiss Army Knife

### The Anti-Pattern: Swiss Army Agent

**File:** `src/customer_service/anti_patterns/swiss_army_agent.py`

```python
_DISTRACTOR_TOOLS: list[dict] = [
    # 10 tools that overlap with, or distract from, the 5 core tools
    {"name": "check_shipping_status", ...},
    {"name": "lookup_order_history", ...},
    {"name": "check_inventory", ...},
    {"name": "update_billing_info", ...},
    {"name": "file_billing_dispute", ...},    # overlaps with process_refund
    {"name": "create_support_ticket", ...},   # overlaps with escalate_to_human
    {"name": "reset_password", ...},
    {"name": "update_account_settings", ...},
    {"name": "search_knowledge_base", ...},
    {"name": "send_feedback_survey", ...},
]

SWISS_ARMY_TOOLS: list[dict] = TOOLS + _DISTRACTOR_TOOLS  # 15 tools
```

15 tools. The canonical misroutes are:
- `file_billing_dispute` overlaps with `process_refund` — Claude may call the wrong one
- `create_support_ticket` overlaps with `escalate_to_human` — ticket creation instead of structured handoff
- `transfer_to_department` overlaps with `escalate_to_human` — unstructured transfer

Research shows tool selection accuracy degrades beyond 4-5 tools. With 15 tools, Claude spends more tokens reasoning about which tool to use, makes more mistakes, and the system becomes harder to test and audit.

### The Correct Pattern: 5 Focused Tools

The correct pattern uses exactly 5 tools (defined in `definitions.py`) with clear, non-overlapping responsibilities and negative-bound descriptions. When you need more capabilities, you use the coordinator-subagent pattern (section 14) to split work across multiple focused agents.

---

## 10. Pattern 4: Context Management — Structured Summaries vs. Raw Transcripts

### The Anti-Pattern: Unbounded Raw Transcript

**File:** `src/customer_service/anti_patterns/raw_transcript.py`

```python
class RawTranscriptContext:
    def __init__(self) -> None:
        self.transcript: str = ""

    def append(self, role: str, content: str) -> None:
        self.transcript += f"\n[{role.upper()}]: {content}"

    def to_context_string(self) -> str:
        return f"CONVERSATION HISTORY:\n{self.transcript}"

    def token_estimate(self) -> int:
        return len(self.transcript) // 4
```

Every turn appends to a growing list. Token usage grows O(n) with turn count. After 5-6 turns, the context becomes so large that Claude experiences the "lost in the middle" effect — information buried in the middle of a long context is less likely to be used.

No compaction. No budget. No structure. The entire conversation history is dumped into the system prompt on every API call.

### The Correct Pattern: ContextSummary with Budget Compaction

**File:** `src/customer_service/agent/context_manager.py`

```python
TOKEN_BUDGET = 300  # estimated tokens (len // 4), about 1,200 characters of rendered context

@dataclass
class ContextSummary:
    customer_id: str = ""
    issue_type: str = ""
    tools_called: list[str] = field(default_factory=list)
    decisions_made: list[str] = field(default_factory=list)
    pending_actions: list[str] = field(default_factory=list)
    turn_count: int = 0
    token_estimate: int = 0
```

Structured fields instead of a flat string. Each field captures a specific dimension of the conversation state.

The `update()` method records one tool call and triggers compaction when the budget is exceeded. It is the only method that refreshes `token_estimate`; setting a field directly does not (the notebooks call this Pitfall 5). Facts that are not tool calls, such as a deadline the customer mentioned, go into `pending_actions` by whatever extraction step you write; nothing extracts them automatically:

```python
def update(self, tool_name: str, result_summary: str) -> None:
    self.tools_called.append(tool_name)
    self.decisions_made.append(result_summary)
    self.turn_count += 1
    self._update_token_estimate()
    if self.token_estimate > TOKEN_BUDGET:
        self._compact()
```

Compaction keeps the last 2 decisions and, if still over budget, only the last pending action. It drops whole entries and never shortens one, so two entries longer than about half the budget cannot be compacted under it. Keep entries short:

```python
def _compact(self) -> None:
    self.decisions_made = self.decisions_made[-2:]
    self._update_token_estimate()
    if self.token_estimate > TOKEN_BUDGET:
        self.pending_actions = self.pending_actions[-1:] if self.pending_actions else []
        self._update_token_estimate()
```

The `to_system_context()` method renders a structured text block for injection into the system prompt. It shows only the last 5 tools and last 3 decisions, which is what keeps the rendered size flat between compactions:

```python
def to_system_context(self) -> str:
    return (
        f"SESSION CONTEXT:\n"
        f"Customer: {self.customer_id} | Issue: {self.issue_type}\n"
        f"Turn: {self.turn_count} | Tools used: {', '.join(self.tools_called[-5:])}\n"
        f"Decisions: {'; '.join(self.decisions_made[-3:])}\n"
        f"Pending: {'; '.join(self.pending_actions)}"
    )
```

Note `tools_called[-5:]` — the display shows only the last 5 tools, but the internal list keeps the full history. The token estimate uses `len(text) // 4` as a rough character-to-token heuristic.

The result: context stays within budget regardless of conversation length. Important information (customer ID, pending actions, recent decisions) is always at the top. Compaction fires around turn 7-8, well before the context becomes unwieldy.

---

## 11. Pattern 5: Cost Optimization — Prompt Caching vs. Batch API

### The Anti-Pattern: Batch API for Live Support

**File:** `src/customer_service/anti_patterns/batch_api_live.py`

This file is documentation, not executable code. It explains why Batch API is wrong for live customer support:

1. **Latency**: Batch API has up to 24-hour turnaround. Customers expect real-time responses.
2. **No ZDR eligibility**: Batch requests do not qualify for Zero Data Retention.
3. **Wrong cost lever**: Batch API gives 50% discount on compute, but the real cost driver in customer support is *repeated context* (policy documents, customer data). Prompt caching addresses this directly.

### The Correct Pattern: Prompt Caching with POLICY_DOCUMENT

**File:** `src/customer_service/agent/system_prompts.py`

```python
POLICY_DOCUMENT = """
=============================================================================
CUSTOMER SUPPORT POLICY REFERENCE — CONFIDENTIAL INTERNAL DOCUMENT
Version 4.2 | Effective Date: January 1, 2025 | Supersedes: Version 4.1
=============================================================================

SECTION 1: CUSTOMER TIER DEFINITIONS AND ELIGIBILITY
1.1  STANDARD TIER ... 1.2  PREMIUM TIER ... 1.3  VIP TIER
SECTION 2: REFUND PROCESSING RULES
SECTION 3: DAMAGE AND DEFECT CATEGORIES
SECTION 4: COMPLIANCE REQUIREMENTS
SECTION 5: ESCALATION CRITERIA AND PROCEDURES
SECTION 6: EDGE CASES AND SPECIAL HANDLING
...
"""
```

This is a large policy document: about 4,100 tokens by the characters-divided-by-four estimate, and 5,102 tokens as the API reports it in `cache_creation_input_tokens` (Notebook 04). The 2,048-token minimum for caching is important — below it, `cache_creation_input_tokens` stays 0 with no error. We target well above the minimum.

The basic system prompt:

```python
def get_system_prompt() -> str:
    return (
        "You are a customer support agent for an online retail company. "
        "Your role is to help customers with refund requests, account inquiries, "
        "and general support issues.\n\n"
        "You have access to 5 tools:\n"
        "1. lookup_customer - Find customer profile by ID\n"
        "..."
        "Always look up the customer first before taking any action. "
        "Check policy before processing refunds. "
        "Log every interaction for compliance purposes.\n\n"
        "..."
    )
```

The cached version:

```python
def get_system_prompt_with_caching() -> list[dict]:
    return [
        {
            "type": "text",
            "text": get_system_prompt(),
            # No cache_control on the small instructions block
        },
        {
            "type": "text",
            "text": POLICY_DOCUMENT,
            "cache_control": {"type": "ephemeral"},
        },
    ]
```

The `cache_control: {"type": "ephemeral"}` marker tells the Claude API to cache everything up to and including the POLICY_DOCUMENT block, which is why the large static block goes last and the marker goes on it. On the first request, you pay 125% of the input cost (cache write). On subsequent requests within the cache TTL, you pay only 10% of the input cost (cache read). For a 4,079-token document repeated across hundreds of customer interactions, this is up to 90% savings.

The agent loop accepts `system_prompt` as either `str` or `list[dict]`:

```python
def run_agent_loop(client, services, user_message, system_prompt, ...):
    # system_prompt can be str or list[dict] — SDK handles both
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        tools=active_tools,
        messages=messages,
    )
```

No conditional logic needed — the Anthropic SDK accepts both formats natively.

---

## 12. Pattern 6: Handoffs — Structured Records vs. Raw Conversation Dumps

### The Anti-Pattern: Raw Conversation Dump

**File:** `src/customer_service/anti_patterns/raw_handoff.py`

```python
def format_raw_handoff(messages: list) -> str:
    return json.dumps(messages, indent=2, default=str)
```

This serializes the entire `messages` list — including tool_use blocks, tool_result blocks, and all the JSON artifacts from every tool call. A typical conversation produces 2,000+ tokens of raw JSON. A human agent receiving this has to dig through tool artifacts to find the 8 pieces of information they actually need.

### The Correct Pattern: Structured EscalationRecord

When escalation is triggered, Claude calls `escalate_to_human` with structured fields:

```python
record = EscalationRecord(
    customer_id="C003",
    customer_tier="regular",
    issue_type="refund",
    disputed_amount=600.0,
    escalation_reason="Amount $600 exceeds $500 review threshold",
    recommended_action="Review refund request with supervisor",
    conversation_summary="Customer requested $600 refund for damaged order. Policy check showed amount exceeds tier limit and review threshold.",
    turns_elapsed=4,
)
```

Compare the size: for a four-call run the raw dump is about 5,600 characters, roughly 7x the structured record (Notebook 06 measures it live). It also carries whatever the customer typed, card numbers included. The human agent gets exactly the 8 fields they need — customer identity, tier, issue, amount, why it escalated, what to do next, what happened so far, and how long the customer has been waiting.

### Forced Escalation via tool_choice

The agent loop implements forced escalation for blocked refunds:

```python
def _has_escalation_required(tool_results: list[dict]) -> bool:
    """Return True if any tool_result contains action_required == 'escalate_to_human'."""
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
```

The check runs on the tool results of the current iteration, not on the whole message history. When it returns True, the loop makes a second API call with `tool_choice={"type": "tool", "name": "escalate_to_human"}`, forcing Claude to call the escalation tool. The loop applies the same forced call when Claude ends its turn with an escalation flag set in context and nothing yet in the escalation queue, which covers the run where Claude asks a question instead of attempting the refund. Together these ensure that a case requiring escalation always results in a structured handoff, never an abandoned conversation.

---

## 13. The Agent Loop — Putting It All Together

**File:** `src/customer_service/agent/agent_loop.py`

This is the core of the system. It implements the CCA agentic loop pattern.

### AgentResult and UsageSummary

```python
@dataclass
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

@dataclass
class AgentResult:
    stop_reason: str            # "end_turn", "escalated", "max_iterations", ...
    messages: list = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)   # {"name", "input", "id"}
    final_text: str = ""
    usage: UsageSummary = field(default_factory=UsageSummary)
```

`UsageSummary` accumulates tokens across all loop iterations. `AgentResult` captures everything the caller needs: why the loop stopped, the full message history, which tools were called, the final text response, and token usage.

### The Loop

```python
def run_agent_loop(
    client, services, user_message, system_prompt,
    model="claude-sonnet-4-6", max_tokens=4096, max_iterations=10,
    tools=None, callbacks=None,
) -> AgentResult:
    active_tools = tools if tools is not None else TOOLS
    context: dict = {"user_message": user_message}   # shared with every callback
    messages: list[dict] = [{"role": "user", "content": user_message}]
    tool_calls: list[dict] = []
    usage = UsageSummary()

    def _force_escalation() -> None:
        # One API call with tool_choice pinned to escalate_to_human; dispatch the result.
        forced = client.messages.create(
            model=model, max_tokens=max_tokens, system=system_prompt,
            tools=active_tools, messages=messages,
            tool_choice={"type": "tool", "name": "escalate_to_human"},
        )
        _add_usage(usage, forced.usage)
        messages.append({"role": "assistant", "content": forced.content})
        ...  # dispatch the tool_use block, append its tool_result as a user turn

    for _iteration in range(max_iterations):
        response = client.messages.create(
            model=model, max_tokens=max_tokens, system=system_prompt,
            tools=active_tools, messages=messages,
        )
        _add_usage(usage, response.usage)
        messages.append({"role": "assistant", "content": response.content})

        # CCA RULE: check stop_reason, never content block types
        if response.stop_reason != "tool_use":
            final_text = ...  # first text block, if any

            # TURN-END ENFORCEMENT: Claude stopped (e.g. asked a question) while a
            # flag is set and nothing is queued. No tool ran, so no callback could
            # catch this. Checked against the store, not against Claude's text.
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
                return AgentResult(stop_reason="escalated", messages=messages,
                                   tool_calls=tool_calls, final_text=final_text, usage=usage)

            return AgentResult(stop_reason=response.stop_reason, messages=messages,
                               tool_calls=tool_calls, final_text=final_text, usage=usage)

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
                {"type": "tool_result", "tool_use_id": block.id, "content": result_content}
            )

        # CCA PITFALL: send ONLY tool_result blocks — no text alongside them
        messages.append({"role": "user", "content": tool_results})

        # HANDOFF-01: a blocked refund forces escalation immediately
        if _has_escalation_required(tool_results):
            _force_escalation()
            return AgentResult(stop_reason="escalated", messages=messages,
                               tool_calls=tool_calls, final_text="", usage=usage)

    return AgentResult(stop_reason="max_iterations", messages=messages,
                       tool_calls=tool_calls, final_text="", usage=usage)
```

Key design decisions:

1. **Stop on `stop_reason != "tool_use"`**: Not `== "end_turn"`. This handles `max_tokens` and other stop reasons gracefully.

2. **Tool results are user messages with ONLY `tool_result` blocks**: No text alongside. Mixing text and `tool_result` in the same message is a Claude API pitfall.

3. **Forced escalation, two triggers**: When a tool result says `action_required: "escalate_to_human"`, or when Claude ends its turn with an escalation flag set and the queue empty, the loop makes one more API call with `tool_choice` pinned to the escalation tool. Both paths return `stop_reason="escalated"`. On most live runs neither fires, because Claude escalates on its own after reading the policy result; the guarantee is for the runs where it does not.

4. **Max iterations**: 10 iterations prevents infinite loops. In practice, most conversations complete in 3-4 iterations.

5. **Context sharing**: The `context` dict is created once and shared across all tool calls in a session. Callbacks set flags (VIP, closure, legal, review) that accumulate across iterations.

6. **Callbacks are opt-in**: `callbacks=None` means no callbacks at all, which is how the anti-pattern modules run. The correct pattern passes `callbacks=build_callbacks()` explicitly.

---

## 14. The Coordinator — Multi-Topic Queries

**File:** `src/customer_service/agent/coordinator.py`

When a customer asks about multiple topics ("I want a refund AND a shipping update AND to close my account"), a single 5-tool agent struggles. The coordinator pattern splits the query into subtasks, delegates each to a focused subagent, and synthesizes the results.

### Three-Step Pattern

```python
def run_coordinator(
    client, services, user_message, customer_id="", customer_tier="",
    model="claude-sonnet-4-6",
) -> CoordinatorResult:
    # Step 1: DECOMPOSE — Coordinator splits message into subtasks
    decompose_response = client.messages.create(
        model=model, max_tokens=512,
        system=COORDINATOR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    subtasks = _parse_subtasks(decompose_response)  # [{"topic", "relevant_details"}, ...]

    # Step 2: DELEGATE — Each subagent gets ONLY an explicit context string
    subagent_results: list[AgentResult] = []
    for subtask in subtasks:
        topic = subtask.get("topic", "refund")
        details = subtask.get("relevant_details", user_message)
        subagent_context = (
            f"Customer ID: {customer_id}\n"
            f"Customer tier: {customer_tier}\n"
            f"Task: {topic}\n"
            f"Details: {details}\n"
        )
        agent_result = run_agent_loop(
            client=client, services=services,
            user_message=subagent_context,
            system_prompt=SUBAGENT_PROMPTS.get(topic, REFUND_AGENT_PROMPT),
            model=model, callbacks=build_callbacks(),
        )
        subagent_results.append(agent_result)

    # Step 3: SYNTHESIZE — Combine subagent outputs into one reply
    synthesis_prompt = f"Customer message: {user_message}\n\nSpecialist responses:\n..."
    synthesis_response = client.messages.create(model=model, max_tokens=512, ...)
    synthesis_text = ...  # first text block

    return CoordinatorResult(subagent_results=subagent_results, synthesis=synthesis_text)
```

### Context Isolation (CCA Rule)

The critical line is:

```python
subagent_context = (
    f"Customer ID: {customer_id}\n"
    f"Customer tier: {customer_tier}\n"
    f"Task: {topic}\n"
    f"Details: {details}\n"
)
agent_result = run_agent_loop(
    client=client,
    services=services,
    user_message=subagent_context,
    system_prompt=SUBAGENT_PROMPTS.get(topic, REFUND_AGENT_PROMPT),
    ...
)
```

The subagent receives **only the explicit context string** — never the coordinator's messages list, never the coordinator's system prompt. The customer ID and tier come from the caller's arguments, not from anything the coordinator model extracted. Each subagent runs with the full callback registry, so the escalation and compliance rules apply inside every subtask. This is the CCA isolation rule: subagents operate with minimal context to prevent information leakage between subtasks.

### Per-Topic Subagent Prompts

```python
SUBAGENT_PROMPTS = {
    "refund": REFUND_AGENT_PROMPT,
    "shipping": SHIPPING_AGENT_PROMPT,
    "account": ACCOUNT_AGENT_PROMPT,
}
```

Each subagent gets a focused prompt for its domain. The refund agent knows about refund policies. The shipping agent knows about delivery tracking. The account agent knows about account management. None of them know about the others' domains.

---

## 15. Notebook Helpers — Cost Tracking and Comparison

**File:** `notebooks/helpers.py`

### print_usage

```python
_PRICE_INPUT = 3.00    # $ per 1M input tokens
_PRICE_OUTPUT = 15.00  # $ per 1M output tokens
_PRICE_CACHE_READ = 0.30   # 10% of input price
_PRICE_CACHE_WRITE = 3.75  # 125% of input price

def estimate_cost(usage) -> float:
    """USD cost of one usage record: an API response's .usage or a loop's UsageSummary."""
    inp = usage.input_tokens
    out = usage.output_tokens
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (inp * _PRICE_INPUT / 1_000_000
            + out * _PRICE_OUTPUT / 1_000_000
            + cr * _PRICE_CACHE_READ / 1_000_000
            + cw * _PRICE_CACHE_WRITE / 1_000_000)

def print_usage(response, model="claude-sonnet-4-6"):
    u = response.usage
    ...  # prints input, output, cache read, cache write, total, and estimate_cost(u)
```

Pricing constants are hardcoded for student visibility — students can see and modify the rates. `estimate_cost` is the one place the arithmetic lives; the notebooks call it directly when they need a cost row in a comparison. The function handles cache tokens gracefully with `getattr(..., 0) or 0`, since cache fields may not exist on all response objects.

Token accounting rule: `total = input + output + cache_read + cache_write`. Cache fields are *not* additive on top of `input_tokens` — they are separate components.

### compare_results

```python
def compare_results(anti_result: dict, correct_result: dict):
    # Boolean metrics: FIXED / REGRESSED / same
    # Numeric metrics: percentage change
    # Renders as a tabulate table
```

Used in notebooks to show side-by-side comparisons of anti-pattern vs correct pattern results. For example:

```
Metric             Anti-Pattern    Correct    Delta
-----------------  -------------   --------   -----
escalation_fired   False           True       FIXED
pii_in_audit_log   True            False      FIXED
tool_count         15              5          -66.7%
token_usage        2847            1203       -57.7%
```

---

## 16. How the Pieces Connect — End-to-End Data Flow

Let's trace a complete request through the system. The scenario: Customer C003 (Carol Martinez, Regular tier) requests a $600 refund.

### Step 1: Setup

```python
from customer_service.data.customers import CUSTOMERS
from customer_service.services import *

services = ServiceContainer(
    customer_db=CustomerDatabase(CUSTOMERS),
    policy_engine=PolicyEngine(),
    financial_system=FinancialSystem(),
    escalation_queue=EscalationQueue(),
    audit_log=AuditLog(),
)
```

All 5 services are constructed and injected into a frozen `ServiceContainer`. The `CustomerDatabase` receives the `CUSTOMERS` dict with all 6 pre-built profiles.

### Step 2: Agent Loop Starts

```python
result = run_agent_loop(
    client=anthropic.Anthropic(),
    services=services,
    user_message="Customer ID: C003. I need a $600 refund for my damaged order.",
    system_prompt=get_system_prompt(),
    callbacks=build_callbacks(),
)
```

The context dict is created: `{"user_message": "Customer ID: C003. I need a $600 refund..."}`.
Callbacks come from `build_callbacks()` and are passed in explicitly. Leaving `callbacks` out runs the loop with no enforcement at all.

### Step 3: Iteration 1 — Claude calls lookup_customer

Claude sees the customer ID in the message and calls `lookup_customer(customer_id="C003")`.

**Handler**: Returns Carol's profile: `{"customer_id": "C003", "tier": "regular", "flags": []}`.

**Callback** (`lookup_customer_callback`): Checks the profile.
- Tier is `regular`, not VIP -> `vip` not set
- No `account_closure` flag -> `account_closure` not set
- Checks user message for legal keywords ("lawsuit", "attorney", etc.) -> none found
- Returns `CallbackResult(action="allow")`

### Step 4: Iteration 1 — Claude calls check_policy

Claude calls `check_policy(customer_id="C003", requested_amount=600.0)`.

**Handler**: Looks up Carol (Regular tier, $100 limit). Returns: `{"approved": false, "limit": 100.0, "requires_review": true}`.

**Callback** (`check_policy_callback`): Sees `requires_review=True` -> sets `context["requires_review"] = True`.

### Step 5: Iteration 2 — Claude calls process_refund

This trace follows the run where Claude attempts the refund anyway. On most live runs it does not: it reads the policy result and calls `escalate_to_human` itself, and the record lands in the queue without the callback ever blocking anything. The callback is the backstop for the other runs.

**Dispatch**: Because `process_refund` has a callback, dispatch uses the two-step vetoable path:

1. `propose_refund()` computes: `{"status": "proposed", "amount": 600.0, "policy_approved": False, "requires_review": True}`
2. `escalation_callback()` checks context:
   - Walks `ESCALATION_FLAGS`; `context["requires_review"]` is `True` -> reason "Refund amount exceeds $500 review threshold"
   - Returns `CallbackResult(action="block", replacement=blocked_json, reason=...)`
3. `commit_refund()` **never runs**. FinancialSystem is untouched.

Claude receives: `{"status": "blocked", "reason": "Refund amount exceeds $500 review threshold", "flag_triggered": "requires_review", "action_required": "escalate_to_human"}`.

### Step 6: Forced Escalation

The agent loop calls `_has_escalation_required(tool_results)` on this iteration's results -> finds `action_required: "escalate_to_human"` -> makes a second API call with `tool_choice={"type": "tool", "name": "escalate_to_human"}`.

Claude is forced to call `escalate_to_human` with structured fields:

```json
{
  "customer_id": "C003",
  "customer_tier": "regular",
  "issue_type": "refund",
  "disputed_amount": 600.0,
  "escalation_reason": "Amount $600 exceeds $500 review threshold",
  "recommended_action": "Review refund request with supervisor",
  "conversation_summary": "Customer requested $600 refund for damaged order...",
  "turns_elapsed": 3
}
```

**Handler**: Creates `EscalationRecord`, adds to `EscalationQueue`.

### Step 7: Loop Terminates

After the forced escalation, the loop returns an `AgentResult` with `stop_reason="escalated"`. Had Claude instead ended its turn after step 4 without attempting the refund, the turn-end check would have found `requires_review` set and the queue empty, and forced the same escalation before returning.

### What We Can Verify

```python
# FinancialSystem was NOT written to (veto guarantee)
assert len(services.financial_system.get_processed()) == 0

# EscalationQueue HAS the record
escalations = services.escalation_queue.get_escalations()
assert len(escalations) == 1
assert escalations[0].disputed_amount == 600.0
# escalation_reason is Claude's own text on the voluntary path and the
# ESCALATION_FLAGS text on the forced path, so do not assert on its wording.

# AuditLog has no raw PII (if cards were mentioned)
for entry in services.audit_log.get_entries():
    assert not CARD_PATTERN.search(entry.details)
```

This is behavior-first testing: test the stores, not the API responses.

---

## Summary: The Six Patterns at a Glance

| # | Pattern | Wrong Way | Right Way | Where in Code |
|---|---------|-----------|-----------|---------------|
| 1 | **Escalation** | LLM self-reports confidence | Deterministic rules in callbacks, forced at turn end | `callbacks.py` escalation_callback, `agent_loop.py` |
| 2 | **Compliance** | "Always redact PII" in prompt | Regex redaction before audit log write | `callbacks.py` compliance_callback |
| 3 | **Tool Design** | 15 overlapping tools | 5 focused tools with negative bounds | `definitions.py` TOOLS |
| 4 | **Context** | Raw transcript grows O(n) | ContextSummary with budget compaction | `context_manager.py` |
| 5 | **Cost** | Batch API (24h latency) | Prompt caching on POLICY_DOCUMENT | `system_prompts.py` |
| 6 | **Handoffs** | Raw conversation JSON dump | Structured 8-field EscalationRecord | `escalate_to_human.py` |

Each pattern follows the same meta-principle: **programmatic enforcement beats prompt-based guidance.** The system prompt tells Claude what to do. The code guarantees it happens.
