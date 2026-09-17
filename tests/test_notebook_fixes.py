"""Tests verifying Phase 7 notebook bug fixes (NBFIX-01, NBFIX-02, NBFIX-03).

These tests mimic what the notebooks do — constructing services, calling callbacks,
and using anti-pattern classes — without requiring live Anthropic API calls.

CCA Principle: "Test the store, not the API response" — we verify the same
code paths the notebooks exercise, using the same seed data and scenarios.
"""

import json
from pathlib import Path

import nbformat
import pytest

from customer_service.agent.agent_loop import AgentResult
from customer_service.agent.callbacks import (
    check_policy_callback,
    escalation_callback,
)
from customer_service.anti_patterns.raw_transcript import RawTranscriptContext
from customer_service.data.customers import CUSTOMERS
from customer_service.data.scenarios import SCENARIOS
from customer_service.services.audit_log import AuditLog
from customer_service.services.container import ServiceContainer
from customer_service.services.customer_db import CustomerDatabase
from customer_service.services.escalation_queue import EscalationQueue
from customer_service.services.financial_system import FinancialSystem
from customer_service.services.policy_engine import PolicyEngine

NOTEBOOKS_DIR = Path(__file__).parent.parent / "notebooks"


# ---------------------------------------------------------------------------
# NBFIX-01 / NBFIX-02: make_services() with seed data
# ---------------------------------------------------------------------------


def make_services() -> ServiceContainer:
    """Mirrors the make_services() helper used in NB04 and NB05 (after fix)."""
    return ServiceContainer(
        customer_db=CustomerDatabase(CUSTOMERS),
        policy_engine=PolicyEngine(),
        financial_system=FinancialSystem(),
        escalation_queue=EscalationQueue(),
        audit_log=AuditLog(),
    )


class TestMakeServicesWithSeedData:
    """NBFIX-01/02: Verify make_services() constructs without TypeError."""

    def test_make_services_succeeds(self) -> None:
        services = make_services()
        assert services.customer_db is not None
        assert services.policy_engine is not None

    def test_customer_db_has_seed_data(self) -> None:
        services = make_services()
        customer = services.customer_db.get_customer("C001")
        assert customer is not None
        assert customer.customer_id == "C001"
        assert customer.name == "Alice Johnson"

    def test_customer_db_has_all_scenario_customers(self) -> None:
        services = make_services()
        for scenario in SCENARIOS.values():
            cid = scenario["customer_id"]
            customer = services.customer_db.get_customer(cid)
            assert customer is not None, f"Missing customer {cid}"
            assert customer.customer_id == cid

    def test_nb04_imports_customers(self) -> None:
        """NB04 code cells must import CUSTOMERS from customer_service.data.customers."""
        nb = nbformat.read((NOTEBOOKS_DIR / "04_cost_optimization.ipynb").open(), as_version=4)
        code = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
        assert "from customer_service.data.customers import CUSTOMERS" in code

    def test_nb04_passes_customers_to_database(self) -> None:
        """NB04 make_services() must pass CUSTOMERS to CustomerDatabase."""
        nb = nbformat.read((NOTEBOOKS_DIR / "04_cost_optimization.ipynb").open(), as_version=4)
        code = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
        assert "CustomerDatabase(CUSTOMERS)" in code
        assert "CustomerDatabase()" not in code

    def test_nb05_imports_customers(self) -> None:
        """NB05 code cells must import CUSTOMERS from customer_service.data.customers."""
        nb = nbformat.read((NOTEBOOKS_DIR / "05_context_management.ipynb").open(), as_version=4)
        code = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
        assert "from customer_service.data.customers import CUSTOMERS" in code

    def test_nb05_passes_customers_to_database(self) -> None:
        """NB05 make_services() must pass CUSTOMERS to CustomerDatabase."""
        nb = nbformat.read((NOTEBOOKS_DIR / "05_context_management.ipynb").open(), as_version=4)
        code = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
        assert "CustomerDatabase(CUSTOMERS)" in code
        assert "CustomerDatabase()" not in code


# ---------------------------------------------------------------------------
# NBFIX-02 secondary: .final_text not .final_response
# ---------------------------------------------------------------------------


class TestAgentResultFinalText:
    """NBFIX-02: AgentResult uses .final_text, not .final_response."""

    def test_agent_result_has_final_text(self) -> None:
        result = AgentResult(stop_reason="end_turn", final_text="Hello")
        assert result.final_text == "Hello"

    def test_agent_result_no_final_response(self) -> None:
        result = AgentResult(stop_reason="end_turn")
        assert not hasattr(result, "final_response")

    def test_nb05_uses_final_text_not_final_response(self) -> None:
        """NB05 anti-pattern cells must use .final_text, not .final_response."""
        nb = nbformat.read((NOTEBOOKS_DIR / "05_context_management.ipynb").open(), as_version=4)
        code = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
        assert ".final_text" in code, "NB05 must use .final_text"
        assert ".final_response" not in code, "NB05 must NOT use .final_response"


# ---------------------------------------------------------------------------
# NBFIX-03: Escalation callback fires for $600 refund
# ---------------------------------------------------------------------------


class TestEscalationCallbackForAmountThreshold:
    """NBFIX-03: The >$500 callback chain fires correctly for the amount_threshold scenario."""

    @pytest.fixture
    def services(self) -> ServiceContainer:
        return make_services()

    def test_check_policy_sets_requires_review_for_600(self, services: ServiceContainer) -> None:
        """check_policy_callback must set context['requires_review'] when amount > $500."""
        context: dict = {}
        result_dict = {"requires_review": True, "approved": True}
        cb_result = check_policy_callback(
            tool_name="check_policy",
            input_dict={"customer_id": "C003", "amount": 600.0},
            result_dict=result_dict,
            context=context,
            services=services,
        )
        assert cb_result.action == "allow"
        assert context.get("requires_review") is True

    def test_escalation_callback_blocks_when_requires_review(
        self, services: ServiceContainer
    ) -> None:
        """escalation_callback must block process_refund when requires_review is set."""
        context = {"requires_review": True}
        cb_result = escalation_callback(
            tool_name="process_refund",
            input_dict={"customer_id": "C003", "amount": 600.0},
            result_dict={},
            context=context,
            services=services,
        )
        assert cb_result.action == "block"
        assert cb_result.replacement is not None
        blocked = json.loads(cb_result.replacement)
        assert blocked["action_required"] == "escalate_to_human"
        assert blocked["flag_triggered"] == "requires_review"

    def test_full_callback_chain_for_600_refund(self, services: ServiceContainer) -> None:
        """Simulate the full check_policy → process_refund callback chain for $600.

        This is exactly what NB01's correct-pattern cell exercises:
        1. Claude calls check_policy → check_policy_callback sets requires_review
        2. Claude calls process_refund → escalation_callback blocks it
        3. Claude should then call escalate_to_human
        """
        context: dict = {}

        # Step 1: check_policy callback (after check_policy tool returns)
        policy_result = {"requires_review": True, "approved": True}
        check_policy_callback(
            "check_policy",
            {"customer_id": "C003", "amount": 600.0},
            policy_result,
            context,
            services,
        )
        assert context["requires_review"] is True

        # Step 2: escalation_callback (before process_refund executes)
        esc_result = escalation_callback(
            "process_refund",
            {"customer_id": "C003", "amount": 600.0},
            {},
            context,
            services,
        )
        assert esc_result.action == "block"
        blocked = json.loads(esc_result.replacement)
        assert blocked["action_required"] == "escalate_to_human"
        assert "500" in blocked["reason"]

    def test_nb01_scenario_message_includes_customer_id(self) -> None:
        """NB01 correct-pattern cell must prepend customer_id to scenario message."""
        nb = nbformat.read((NOTEBOOKS_DIR / "01_escalation.ipynb").open(), as_version=4)
        code = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
        assert "scenario['customer_id']" in code or 'scenario["customer_id"]' in code, (
            "NB01 must include customer_id in the scenario message so Claude calls tools"
        )

    def test_amount_threshold_scenario_exists(self) -> None:
        """The amount_threshold scenario must exist with $600 and customer C003."""
        scenario = SCENARIOS["amount_threshold"]
        assert scenario["customer_id"] == "C003"
        assert "600" in scenario["message"]
        assert scenario["expected_outcome"] == "escalated_amount"


# ---------------------------------------------------------------------------
# RawTranscriptContext API (used by NB05 anti-pattern cells)
# ---------------------------------------------------------------------------


class TestRawTranscriptAPI:
    """Verify RawTranscriptContext has the methods NB05 calls."""

    def test_append_method(self) -> None:
        ctx = RawTranscriptContext()
        ctx.append("user", "Hello")
        ctx.append("assistant", "Hi there")
        assert "Hello" in ctx.to_context_string()
        assert "Hi there" in ctx.to_context_string()

    def test_token_estimate_grows(self) -> None:
        ctx = RawTranscriptContext()
        t0 = ctx.token_estimate()
        ctx.append("user", "A long message about a refund for order twelve")
        t1 = ctx.token_estimate()
        assert t1 > t0, "token_estimate must grow after append (O(n) growth)"

    def test_to_context_string_contains_history(self) -> None:
        ctx = RawTranscriptContext()
        ctx.append("user", "My birthday is March 15")
        output = ctx.to_context_string()
        assert "birthday" in output
        assert "CONVERSATION HISTORY" in output


# ---------------------------------------------------------------------------
# NB05: accumulated context must actually reach the model
# ---------------------------------------------------------------------------


class TestNB05ContextInjection:
    """NB05 must send each strategy's accumulated context on every turn."""

    def _code(self) -> str:
        nb = nbformat.read((NOTEBOOKS_DIR / "05_context_management.ipynb").open(), as_version=4)
        return "\n".join(c.source for c in nb.cells if c.cell_type == "code")

    def test_raw_transcript_is_sent_in_system_prompt(self) -> None:
        code = self._code()
        assert "raw_transcript.to_context_string()" in code
        assert 'get_system_prompt() + "\\n\\n" + context' in code

    def test_summary_is_sent_in_system_prompt(self) -> None:
        code = self._code()
        assert "summary.to_system_context()" in code

    def test_services_persist_across_turns(self) -> None:
        """A session shares one service container; a fresh one per turn hides state."""
        code = self._code()
        assert "run_agent_loop(client, make_services()" not in code

    def test_no_last_200_chars_check(self) -> None:
        """Recall is checked in the model's reply, not by a substring window."""
        code = self._code()
        assert "[-200:]" not in code


# ---------------------------------------------------------------------------
# NB08: meta-teaching claims must match the infrastructure they describe
# ---------------------------------------------------------------------------

PROJECT_ROOT = NOTEBOOKS_DIR.parent
SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "review-cca-compliance" / "SKILL.md"


def _claude_argv(ci_yml: str) -> list[str]:
    """Mirror the NB08 audit: drop comment lines, then tokenize the claude command."""
    import re

    code_only = "\n".join(line for line in ci_yml.splitlines() if not line.lstrip().startswith("#"))
    match = re.search(r"claude \\\n(.*?)\n\s*\"", code_only, re.DOTALL)
    assert match, "no claude command found"
    return match.group(1).replace("\\", " ").split()


class TestNB08MetaTeaching:
    """NB08 describes the skill layout and CI flags; both must be real."""

    def _nb(self):
        return nbformat.read((NOTEBOOKS_DIR / "08_meta_teaching.ipynb").open(), as_version=4)

    def test_skill_uses_discoverable_layout(self) -> None:
        """Claude Code discovers .claude/skills/<name>/SKILL.md, not a flat .md file."""
        assert SKILL_PATH.exists()
        assert not (PROJECT_ROOT / ".claude" / "skills" / "review-cca-compliance.md").exists()
        text = SKILL_PATH.read_text()
        assert text.startswith("---\n")
        assert "\nname: review-cca-compliance\n" in text
        assert "\ndescription: " in text

    def test_notebook_references_new_skill_path_only(self) -> None:
        source = "\n".join(c.source for c in self._nb().cells)
        assert ".claude/skills/review-cca-compliance/SKILL.md" in source
        assert "review-cca-compliance.md" not in source
        assert "Use the review-cca-compliance skill" not in source
        assert "/review-cca-compliance src/" in source

    def test_skill_check_count_matches_notebook_claim(self) -> None:
        """The notebook says 11 checks; count numbered headings, not every '###'."""
        import re

        checks = re.findall(r"^### \d+\. ", SKILL_PATH.read_text(), re.MULTILINE)
        assert len(checks) == 11
        source = "\n".join(c.source for c in self._nb().cells if c.cell_type == "markdown")
        assert "11 focused checks" in source

    def test_ci_command_has_audited_flags(self) -> None:
        """The flags must be on the claude command, not only in comments."""
        argv = _claude_argv((PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
        assert "-p" in argv
        assert "--bare" in argv
        assert argv[argv.index("--output-format") + 1] == "json"
        assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob"
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"

    def test_audit_ignores_flags_that_appear_only_in_comments(self) -> None:
        """A substring check would pass on comments alone; the parsed audit must not."""
        commented = "\n".join(
            "# " + line if not line.lstrip().startswith("#") else line
            for line in (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text().splitlines()
        )
        assert "--bare" in commented  # substring check would still say PASS
        with pytest.raises(AssertionError, match="no claude command"):
            _claude_argv(commented)

    def test_notebook_audit_does_not_use_substring_matching(self) -> None:
        code = "\n".join(c.source for c in self._nb().cells if c.cell_type == "code")
        assert "present = flag in ci_content" not in code
        assert 'startswith("#")' in code

    def test_hierarchy_claims_match_docs_and_repo(self) -> None:
        source = "\n".join(c.source for c in self._nb().cells if c.cell_type == "markdown")
        assert "`./CLAUDE.md` or `./.claude/CLAUDE.md`" in source
        assert "you add it to `.gitignore`" in source
        assert "CLAUDE.local.md" in (PROJECT_ROOT / ".gitignore").read_text()
        assert "No (gitignored)" not in source

    def test_summary_table_maps_coordinator_to_nb03(self) -> None:
        source = "\n".join(c.source for c in self._nb().cells if c.cell_type == "markdown")
        assert "| Coordinator-Subagent | NB03 |" in source
        assert "| Handoffs | NB06 |" in source
        assert "| Coordinator-Subagent | NB06 |" not in source
