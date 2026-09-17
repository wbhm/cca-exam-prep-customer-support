# CCA Customer Support Resolution Agent

> Hands-on coding example for the **Claude Certified Architect -- Foundations (CCA-F)** exam prep course.

The **Customer Support Resolution Agent** is a key scenario in the CCA-F exam, designed to test real-world AI agent architecture for high-stakes customer service. This project demonstrates all 6 architectural patterns from the scenario through 9 Jupyter notebooks, pairing anti-patterns (the wrong way) with correct patterns (the right way) so students internalize the distinctions before exam day.

## The CCA Customer Support Scenario

### Core Architecture

This scenario involves a **single-agent loop** using the Anthropic Python SDK, integrated with **5 focused tools**:

| Tool | Type | Purpose |
|------|------|---------|
| `lookup_customer` | Read | Retrieves customer profile, tier, and account flags (the policy prompt orders it first; callbacks read its result to set escalation flags) |
| `check_policy` | Read | Checks refund eligibility against tier-based policy limits |
| `process_refund` | Write | Executes refund actions (two-step vetoable -- callbacks can block) |
| `escalate_to_human` | Write | Transfers conversation with structured EscalationRecord JSON |
| `log_interaction` | Write | Records interaction for compliance audit (PII redacted before write) |

The policy document prescribes the tool order: **lookup_customer -> check_policy -> process_refund -> escalate_to_human (if a mandatory trigger applies) -> log_interaction**. The order is prompt guidance; the callbacks enforce the outcomes (blocked refunds, forced escalation, redacted logs) regardless of the order Claude chooses.

### Key Exam Concepts

The scenario tests critical architectural tradeoffs:

1. **Programmatic Enforcement** -- The exam emphasizes using code (PostToolUse callbacks) over relying on prompts to prevent costly errors. In this project, `callbacks.py` enforces escalation rules deterministically -- amount > $500, VIP tier, account closure, legal keywords.

2. **Tool Description Quality** -- Clear, detailed tool descriptions with negative bounds ("does NOT modify customer data") prevent the agent from misrouting. The 15-tool Swiss Army anti-pattern demonstrates how tool overload degrades selection accuracy.

3. **Explicit Escalation Criteria** -- The agent escalates based on deterministic business rules, NOT self-reported confidence scores. The confidence escalation anti-pattern has no rule in code: on most live runs Claude escalates the $600 refund anyway, because the policy tool result tells it the refund is not approved, but roughly one run in twenty it ends its turn with a clarifying question and the case is silently dropped with no human notified. The self-rated confidence never decides the outcome. Notebook 01 replays that dropped-case transcript through both patterns with a scripted client so the difference is visible on every run.

4. **Structured Handoffs** -- The `escalate_to_human` tool schema enforces a complete EscalationRecord JSON (customer ID, tier, issue type, disputed amount, escalation reason, recommended action, conversation summary, turns elapsed). `tool_choice` is the backstop: when a business rule requires escalation and the turn ends without the tool call, the loop forces one. The raw handoff anti-pattern dumps the entire conversation as unstructured text.

5. **Context Management** -- The agent efficiently manages context through structured JSON summaries that stay under a token budget, while the raw transcript anti-pattern demonstrates unbounded growth and the lost-in-middle effect.

6. **Cost Optimization** -- Prompt caching with `cache_control` on static policy context (90% savings on reads). The Batch API anti-pattern shows why 50% savings with 24-hour latency is always wrong for live customer support.

## Quick Start

```bash
# Install Task runner (if not already installed)
brew install go-task

# One-command setup (installs deps, checks API key, opens setup notebook)
task setup

# Or manually:
poetry install --with notebooks
cp .env.example .env  # Add your ANTHROPIC_API_KEY
poetry run jupyter lab
```

### Available Commands

```bash
task setup      # Install deps + check API key + open setup notebook
task test       # Run the test suite
task lint       # Run ruff linter
task verify     # Full verification: tests + lint + import check
task notebook   # Launch Jupyter Lab
```

## Notebooks

| # | Notebook | CCA Pattern | Anti-Pattern |
|---|----------|-------------|--------------|
| 00 | Setup | Environment verification | -- |
| 01 | Escalation | Deterministic callbacks | LLM confidence routing |
| 02 | Compliance | Programmatic PII redaction | Prompt-only rules |
| 03 | Tool Design | 5 focused tools | 15-tool Swiss Army |
| 04 | Cost Optimization | Prompt caching | Batch API for live support |
| 05 | Context Management | Structured summaries | Raw transcript bloat |
| 06 | Handoffs | Schema-enforced EscalationRecord (tool_choice backstop) | Raw conversation dump |
| 07 | Integration | All 6 patterns in one scenario | -- |
| 08 | Meta-Teaching | Project as CCA example | -- |

Each notebook follows: **Setup -> Anti-Pattern (red box) -> Correct Pattern (green box) -> Compare**

Student TODO placeholders in notebooks 06 and 07 provide hands-on learning opportunities.

## Architecture

```
src/customer_service/
  models/        # Pydantic data models (CustomerProfile, EscalationRecord, etc.)
  services/      # 5 simulated in-memory services + frozen ServiceContainer
  tools/         # Tool schemas (from model_json_schema()), handlers, dispatch registry
  agent/         # Agentic loop, PostToolUse callbacks, context manager, coordinator
  anti_patterns/ # 6 deliberately wrong implementations (imported by notebooks only)
  data/          # Seed customers (C001-C006) and scenarios
```

### Critical Data Flow

```
Customer message
  -> agent_loop.py calls client.messages.create()
  -> Claude returns tool_use blocks
  -> handlers.py dispatches each tool_use to its handler -> service call
  -> callbacks.py runs as a PostToolUse hook on the result (business rules in code)
     - process_refund is two-step: propose -> escalation_callback -> commit or block
     - log_interaction is redacted BEFORE the handler writes to the audit log
     - lookup_customer / check_policy callbacks set escalation flags in the loop context
  -> if blocked: structured error returned as tool_result; the loop forces escalate_to_human
  -> loop ends on stop_reason: end_turn, escalated, or max_iterations
```

## CCA Meta-Patterns in This Project

This project doesn't just *teach* CCA patterns -- it *uses* them:

### CLAUDE.md Hierarchy (Level 2: Project)

- `.claude/CLAUDE.md` -- Team standards, architecture rules, and build commands (CCA exam: project-level is VCS-tracked, shared across team)
- `CLAUDE.local.md` -- Personal overrides, listed in `.gitignore` (Claude Code does not ignore it for you)

### CI/CD Pipeline Flags

- `.github/workflows/ci.yml` -- Uses `claude -p --bare --output-format json --allowedTools Read,Grep,Glob --permission-mode dontAsk`
- `-p`: required for any unattended run (without it, the run waits for input that never comes)
- `--bare`: reproducibility (skips hooks, skills, MCP servers, plugins, auto memory, and CLAUDE.md; also skips OAuth login, so the API key comes from a secret)
- `--allowedTools`: pre-approves only the read-only tools a review needs; `--permission-mode dontAsk` denies everything else
- A nightly cron trigger is written into the workflow but commented out; uncomment the `schedule` block to enable it

### Custom Skill

- `.claude/skills/review-cca-compliance/SKILL.md` -- run `/review-cca-compliance <path>` to review any code for CCA compliance
- Demonstrates custom skills as reusable, on-demand workflows

### Programmatic Enforcement

- `.pre-commit-config.yaml` -- nbstripout + ruff enforced on every commit
- Same principle as callbacks: code enforces rules, not human memory

## Testing

```bash
poetry run pytest              # Full suite, simulated services, no API calls
```

The tests never call the Anthropic API. Live behavior is exercised by the notebooks, which need `ANTHROPIC_API_KEY`.

Tests follow **behavior-first** verification:
- Test persistent stores (AuditLog, EscalationQueue, FinancialSystem), not just returned JSON
- Every completion claim maps to a specific test
- The PII redaction test checks the actual audit log, not the API response

## CCA Rules Reference

`.planning/CCA-RULES.md` contains the authoritative CCA exam patterns extracted from all 8 source articles. Every line of code in this project complies with these rules.

## Requirements

- Python 3.13+
- Poetry
- `ANTHROPIC_API_KEY` (that's it -- no other services needed)

## License

This project is part of the CCA Exam Prep course by Rick Hightower at Spillwave.
