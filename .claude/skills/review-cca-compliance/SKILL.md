---
name: review-cca-compliance
description: Review Python code, notebooks, or CI workflows for CCA (Claude Certification for Agentic systems) architectural patterns. Use when asked to audit code for CCA compliance or CCA anti-patterns.
---

# Skill: review-cca-compliance

Review any Python code, notebook, or workflow file for compliance with CCA (Claude Certification for Agentic systems) architectural patterns.

## Usage

Skills are discovered from `.claude/skills/<name>/SKILL.md`. Invoke this one directly as a slash command, with the file or directory to review as the argument:

```
/review-cca-compliance src/customer_service/agent/agent_loop.py
```

Or to review a directory:

```
/review-cca-compliance src/customer_service/agent/
```

Claude can also load this skill on its own when a request matches the `description` above, for example "audit this file for CCA anti-patterns".

Note: `claude -p --bare` skips skill discovery, so a CI run with `--bare` does not load this file. The CI workflow in `.github/workflows/ci.yml` inlines the same checklist in its prompt for that reason.

---

## CCA Compliance Checklist

Work through each section. For every check, report one of:
- **PASS** — requirement met, brief explanation
- **WARN** — marginal or unclear, flag for human review
- **FAIL** — clear CCA anti-pattern violation, explain why and how to fix

---

### 1. Tool Count (CCA Rule: 4-5 tools per agent)

- [ ] Count tools in each `TOOLS` list or tool definition. Report total.
- [ ] If count > 5: **FAIL** — list the tools and recommend splitting to coordinator-subagent pattern
- [ ] If count == 5: **PASS**
- [ ] If count 4-5: **PASS**
- [ ] Check: Does any tool list include HR, marketing, shipping, or other domain-crossing tools? → **FAIL** (Swiss Army anti-pattern)

**Anti-pattern signal:** `SWISS_ARMY_TOOLS`, tool lists > 5 entries, tool names like `send_marketing_email`, `update_hr_record`

---

### 2. Tool Descriptions — Negative Bounds (CCA Rule: descriptions drive routing)

- [ ] Read every tool description string
- [ ] Does each description contain "does NOT" or "does not"? If missing: **WARN**
- [ ] Is the description specific enough to prevent misrouting? Vague = **WARN**, e.g., "Get customer info" (bad) vs "Look up customer profile by ID; does NOT modify customer data" (good)
- [ ] Are parameter descriptions present and meaningful?

**Anti-pattern signal:** Descriptions under 20 words with no negative bounds

---

### 3. Escalation Logic (CCA Rule: deterministic code, NOT LLM confidence)

- [ ] Find where escalation routing decisions are made
- [ ] Is it a programmatic check (Python `if` on a flag, amount, or tier)? → **PASS**
- [ ] Is it based on a confidence score or LLM self-assessment? → **FAIL**
- [ ] Is escalation enforced in a callback/hook BEFORE the tool executes? → **PASS**
- [ ] Is escalation guidance only in the system prompt (not in code)? → **FAIL**

**Anti-pattern signal:** `confidence > 0.7`, `if response.confidence`, system prompt phrases like "escalate when unsure"

**Correct pattern signal:** `callbacks.py`, `PostToolUse`, `if context.get("requires_review")`, `if context.get("vip")`

**Thresholds to verify:** amount > $500, account closure flag, VIP tier, legal complaint keywords

---

### 4. Compliance Enforcement (CCA Rule: programmatic hooks, not prompt instructions)

- [ ] Is PII/PCI redaction done in code (regex, callback)? → **PASS**
- [ ] Is redaction instruction only in the system prompt ("never log credit card numbers")? → **FAIL**
- [ ] Is audit logging performed programmatically for every tool call? → **PASS**
- [ ] Does the callback execute BEFORE the tool result is written to storage? → **PASS** (critical: redaction must precede the write)

**Anti-pattern signal:** System prompt phrases like "never include credit card", "ensure PCI compliance", "do not log sensitive"

**Correct pattern signal:** `compliance_callback`, `re.sub(r'\b\d{4}[- ]\d{4}...`, `AuditLog.add_entry()`

---

### 5. Context Management (CCA Rule: structured JSON summaries, not raw transcripts)

- [ ] Find where conversation context is stored between turns
- [ ] Is it a structured object with named fields (customer_id, issue_type, turn_count)? → **PASS**
- [ ] Is it a raw list of messages or string concatenation? → **FAIL** (lost-in-middle risk)
- [ ] Does the context object have a token budget and compaction logic? → **PASS**
- [ ] Does `to_system_context()` or equivalent produce a compact string, not full history?

**Anti-pattern signal:** `transcript.append(message)`, `"\n".join(messages)`, O(n) growth patterns

**Correct pattern signal:** `ContextSummary`, `update(tool_name, result_summary)`, `token_estimate < TOKEN_BUDGET`

---

### 6. Cost Optimization (CCA Rule: Prompt Caching for repeated context, NOT Batch API for live support)

- [ ] Is Batch API (`client.beta.messages.batches`) used anywhere in live agent paths? → **FAIL**
- [ ] Is `cache_control: {"type": "ephemeral"}` applied to repeated policy documents? → **PASS**
- [ ] Is the cached block >= 2048 tokens (minimum threshold for claude-sonnet-4-6)? → check content size
- [ ] Is `cache_control` placed on the LAST static block (before dynamic content)? → **PASS**

**Anti-pattern signal:** `batches.create()` in any live support path, no `cache_control` on large policy documents

**Correct pattern signal:** `get_system_prompt_with_caching()`, `{"type": "text", "text": POLICY_DOCUMENT, "cache_control": {"type": "ephemeral"}}`

---

### 7. Handoff Pattern (CCA Rule: schema-enforced EscalationRecord JSON, tool_choice as backstop)

- [ ] Is the escalation output a structured JSON object with all required fields? → **PASS**
  - Required: `customer_id`, `customer_tier`, `issue_type`, `disputed_amount`, `escalation_reason`, `recommended_action`, `conversation_summary`, `turns_elapsed`
- [ ] Is the full conversation transcript passed to the human agent? → **FAIL**
- [ ] Are the fields enforced by the `escalate_to_human` tool schema (Pydantic input model), so every escalation carries them? → **PASS**
- [ ] Is `tool_choice={"type": "tool", "name": "escalate_to_human"}` used as the backstop when a business rule requires escalation but the model has not called the tool? → **PASS**

**Anti-pattern signal:** Passing `messages` list directly to human agent, raw conversation dumps, `format_raw_handoff()`

**Correct pattern signal:** `EscalationRecord`, `tool_choice`, `_has_escalation_required()`, `stop_reason == "escalated"`

---

### 8. Agentic Loop (CCA Rule: terminate on stop_reason, handle every stop_reason)

- [ ] Find the main agentic loop (`while True`, `for _ in range(max_iterations)`, or equivalent)
- [ ] Does it branch on `response.stop_reason`, never on content block types? → **PASS**
- [ ] Does it check `response.content[0].type == "text"` to decide whether to stop? → **FAIL** (content-type checking)
- [ ] Is every stop_reason handled explicitly, with its own outcome?
  - `tool_use` → dispatch tools, continue
  - `end_turn` / `stop_sequence` → finished
  - `max_tokens` → finished but marked truncated; the text is never returned as a complete answer
  - `refusal` → routed to escalation or returned as an error, never as a normal finish
  - `pause_turn` → resend history, continue
  - unknown value → raise or return a structured error, never "done"
  → **PASS** if all six are distinguished
- [ ] Does the loop collapse every non-`tool_use` value into "done" (e.g. `if stop_reason != "tool_use": return ...`, or passing `stop_reason=response.stop_reason` through with no branch for `max_tokens` or `refusal`)? → **FAIL** (a truncated reply or a refusal is shown to the customer as a finished answer)
- [ ] Does the loop dispatch ALL tool_use blocks before continuing? → **PASS**

**Anti-pattern signal:** `if response.content[0].type == "text": break`, `if "end_turn" in str(response):`, `if response.stop_reason != "tool_use": return`, a single `else` branch that treats every remaining value as finished

**Correct pattern signal:** `match response.stop_reason:` with a named case per value and `case _: raise UnexpectedStopReasonError`, or an `if/elif` chain that names each value and raises in the final `else`

---

### 9. Coordinator-Subagent Pattern (CCA Rule: explicit context passing, no inheritance)

- [ ] Do subagents receive all required context as explicit arguments? → **PASS**
- [ ] Do subagents share the coordinator's `messages` list or `system_prompt`? → **FAIL**
- [ ] Does each subagent have 4-5 focused tools (not the coordinator's full set)? → **PASS**
- [ ] Do subagents communicate directly with each other? → **FAIL** (must go through coordinator)
- [ ] Does the coordinator tell a failed subagent from a finished one before synthesis? → checked in Section 12

**Anti-pattern signal:** `subagent_messages = coordinator_messages`, passing `system_prompt=coordinator_system_prompt` to subagents

**Correct pattern signal:** `context_string = f"Customer ID: {customer_id}\nTask: {task}"`, subagent initialized with only task-specific data

---

### 10. CLAUDE.md Hierarchy (CCA Rule: project standards in .claude/CLAUDE.md)

- [ ] Does `.claude/CLAUDE.md` exist in the repo root? → **PASS**
- [ ] Is it committed to version control (not gitignored)? → **PASS**
- [ ] Does it contain team coding standards? → **PASS**
- [ ] Are secrets or personal preferences in `.claude/CLAUDE.md`? → **FAIL** (belongs in `~/.claude/CLAUDE.md`)

**Level reference:**
- System directory = org-managed, non-overridable
- `.claude/CLAUDE.md` = project, VCS, team standards ← correct for team rules
- `~/.claude/CLAUDE.md` = user, personal preferences
- `CLAUDE.local.md` = personal override, gitignored

---

### 11. CI/CD Flags (CCA Rule: -p for unattended runs, --bare for reproducibility)

- [ ] Does any CI workflow use `claude` without `-p`? → **FAIL** (an unattended run waits for input that never comes)
- [ ] Is `--bare` present? → **PASS** if yes, **WARN** if absent. `--bare` skips hooks, skills, commands, subagents, plugins, MCP servers, auto memory, and CLAUDE.md, so the run is the same on every machine. It also skips OAuth login, so `ANTHROPIC_API_KEY` must be set.
- [ ] Is `--output-format json` used when output is parsed programmatically? → **PASS**
- [ ] Is `jq -r '.result'` (or equivalent) used to extract text from the JSON envelope? → **PASS**
- [ ] Is `--allowedTools` limited to the tools the job needs (Read, Grep, Glob for a review)? → **PASS**. Note that `--allowedTools` pre-approves tools; it does not remove the others. Pair it with `--permission-mode dontAsk` so anything else is denied instead of waiting on a prompt.
- [ ] Is `ANTHROPIC_API_KEY` stored as a secret, never hardcoded? → check workflow env blocks

---

### 12. Silent Failure Prevention (CCA Rule: structured error context, never swallowed errors)

The structured error shape has six fields: `status`, `error_type`, `source`, `retry_eligible`, `fallback_available`, `partial_data`. Check it at every boundary where a caller must decide what to do next.

- [ ] **Tool boundary.** Does the dispatcher return that shape as JSON for an unknown tool name and for invalid input? → **PASS**. Does it raise, return `None`, or return a bare string? → **FAIL**
- [ ] **Loop boundary.** Does the agent loop's result carry that shape for every degraded outcome (`max_tokens`, a failed forced escalation, `max_iterations`)? → **PASS**. Is the only signal a `stop_reason` string the caller has to interpret? → **FAIL**
- [ ] Is `retry_eligible` meaningful (true for truncation and iteration limits, false for a failed escalation that a retry cannot fix)? → **PASS**
- [ ] Does `partial_data` carry what was recovered (partial text, tool call count, the flag that required escalation)? → **PASS**
- [ ] **Coordinator boundary.** Does the coordinator branch on each subagent result's error before synthesis, withhold degraded text from the synthesis prompt, and expose which subtasks degraded? → **PASS**. Does it join every subagent's `final_text` regardless of outcome? → **FAIL** (a truncated or empty answer is synthesized into a confident reply)
- [ ] Is any exception swallowed (`except: pass`, `except Exception: return ""`)? → **FAIL**

**Anti-pattern signal:** `return None` on error, `except: pass`, `"\n\n".join(r.final_text for r in results)` with no check on `r.error` or `r.stop_reason`, `AgentResult` with no error field

**Correct pattern signal:** `"retry_eligible"`, `"partial_data"`, `AgentResult.error`, `CoordinatorResult.degraded`, `if r.error is None:` before synthesis, `_structured_error(...)`

---

## Output Format

For each check, report findings in this format:

```
### [Section Name]

- [PASS/WARN/FAIL] [Check description]
  - File: <file path>
  - Line: <line number or range, if applicable>
  - Detail: <specific evidence from the code>
  - Fix: <what to change, if WARN or FAIL>
```

End with a summary table:

```
## Summary

| Section | Status | Issues |
|---------|--------|--------|
| Tool Count | PASS | 5 tools |
| Tool Descriptions | WARN | 2 tools missing negative bounds |
| Escalation Logic | PASS | deterministic callbacks |
| Agentic Loop | FAIL | max_tokens and refusal collapsed into "done" |
| Silent Failure Prevention | FAIL | coordinator joins final_text with no error check |
| ... | ... | ... |

**Overall: PASS / REVIEW NEEDED (N issues)**
```

A result of PASS means the code follows CCA architectural patterns and would score well on the CCA exam.
A result of REVIEW NEEDED means at least one anti-pattern was detected that could cost points on the exam.
