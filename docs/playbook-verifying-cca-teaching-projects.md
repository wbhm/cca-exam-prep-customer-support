# Playbook: Verifying a CCA-F Teaching Project

This document describes the review-and-repair process applied to the Customer Support
Resolution Agent project on 2026-09-17, in a form another coding agent can follow on a
different CCA-F exam-prep project (a different scenario, domain, or test application). It
covers the goal, the working agreements, the per-notebook audit loop, the fix recipes that
worked, the documentation sweep that followed, and the proof rules that gate every commit.

Read it top to bottom once, then use Section 9 as the checklist.

---

## 1. What kind of project this applies to

A CCA-F teaching project has this shape:

- A Python package implementing one scenario's agent correctly (tools, handlers,
  services, callbacks, an agent loop, usually a coordinator).
- An `anti_patterns/` package with deliberately wrong implementations.
- A series of notebooks, one per CCA pattern, each running an anti-pattern and the correct
  pattern side by side against the live Claude API and printing a comparison.
- An integration notebook, a "meta-teaching" notebook about the project's own
  infrastructure (CLAUDE.md, CI workflow, skills, pre-commit), a README, a long tutorial,
  and a rules reference extracted from the exam articles.
- A test suite that uses simulated services and never calls the API.

The domain does not matter. Refund policies, ticket triage, claims processing, order
management: the patterns and the failure modes below are the same.

## 2. The finding that drives the whole process

The notebooks were written on the assumption that running the anti-pattern live would show
harm (a refund paid out, a card number logged, a case dropped) and running the correct
pattern would show the fix. On current models that assumption fails on most runs. Claude
follows the prompt and the tool results, so the anti-pattern usually behaves well, and both
columns of the comparison table look the same.

That produces three kinds of defect, and every notebook had at least one:

1. **Pass conditions that depend on model behavior.** A cell asserts the anti-pattern did
   something bad. On most runs it did not, so the cell prints a false "no difference".
2. **Unfair or mislabeled comparisons.** The two runs differ in more than the one thing
   being taught (different prompts, different inputs, a fresh service container per turn,
   a cached prompt compared to nothing).
3. **Prose that describes what the author expected rather than what happens.** Markdown
   cells, docstrings, the tutorial, and the README all said the anti-pattern fails and the
   correct pattern's enforcement fires. Neither was reliably true.

The repair principle throughout: **make the guarantee visible without depending on the
model.** Test the store the rule protects, replay fixed transcripts through the real loop
with a scripted client, compare fair baselines, and say honestly which path a live run
took.

## 3. Working agreements

These came from the project owner during the work. Adopt them unless the new owner says
otherwise.

- **Scope to the named file.** When asked about one notebook, analyze that notebook and the
  code it calls. Do not widen into project-wide cleanups. Offer wider work in one line.
- **Assessment first, edits on request.** "Check X" or "same problem?" means report
  findings and stop. "Fix these then commit and push" means apply every listed finding,
  prove it, commit, push.
- **One headless run, not an experiment.** Execute a notebook once to see what it does. Do
  not launch repeated live runs to gather statistics unless asked. Live runs cost money and
  the owner decides when to spend it.
- **Never print secrets.** `.env` holds a real API key. Never cat it or echo it.
- **Commit messages describe the change only.** No AI attribution, no co-author trailers,
  no "generated with" lines. Conventional prefix, subject under 72 characters, bullets for
  the substance.
- **Notebooks are committed with outputs cleared.** Edit the notebook JSON, not an executed
  copy. Execute into a scratch directory for verification.
- **Leave what is not yours.** Pre-existing uncommitted changes and untracked files stay
  out of your commits.

## 4. Phase 0: orientation

1. Read the project `CLAUDE.md` for standards, especially any "verification rules" and
   the command to run tests.
2. Run the full test suite and lint once to establish a green baseline.
3. Read the core modules in dependency order: models, services, tool definitions,
   handlers and dispatch, callbacks, agent loop, coordinator, system prompts, context
   manager, anti-patterns, notebook helpers. Note every public signature. Docs will
   misquote them.
4. Learn how notebook execution works here:

   ```bash
   cd notebooks
   poetry run jupyter nbconvert --to notebook --execute NB.ipynb \
     --output-dir <scratch> --output NB_executed.ipynb \
     --ExecutePreprocessor.timeout=300 \
     --ExecutePreprocessor.skip_cells_with_tag=never-skip
   ```

   The last flag matters: nbconvert silently skips cells tagged `skip-execution` by
   default, and some notebooks tag their live API cells that way. Without the flag you
   will conclude a cell works when it never ran.
5. Find the existing tests that constrain notebooks (cell counts, required tags, phrases
   such as "CCA Exam Tip"). Those tests decide what you may restructure.

## 5. Phase 1: the per-notebook audit loop

Work through the notebooks in order. For each one, answer the owner's standing question:
"does the live demo actually show the anti-pattern's harm and the correct pattern's fix,
or does it only look like it does?"

### 5.1 Procedure

1. Dump every cell with its index, type, and tags (a short Python script over the JSON).
2. Read the code cells against the source. Check every function name, argument, field
   name, and constant. Check that the two runs differ only in the thing being taught.
3. Execute the notebook once headlessly and read the outputs of the comparison cells.
4. Classify each defect using the list in 5.2.
5. Report findings as an assessment: what is wrong, why, and what the fix would be. Do not
   edit yet.

### 5.2 Defect catalogue (what to look for)

Each item below was found at least once in this project.

- **Model-dependent pass condition.** A boolean metric such as `escalation_fired` or
  `pii_in_audit_log` that is true only if Claude misbehaved. On most runs it did not.
- **Unfair baseline.** The cached run compared against a run with a smaller prompt, so the
  savings were overstated. The uncached baseline must send the same content.
- **Input mismatch.** The correct run's message included the customer ID and the
  anti-pattern's did not, so the anti-pattern spent an extra turn asking for it.
- **Fresh state per turn.** A multi-turn demo built a new service container each turn, so
  no state persisted and the "context management" comparison measured nothing.
- **Context never sent.** The demo built a summary or transcript each turn but never put
  it into the system prompt, so the model saw nothing and the token counts were flat.
- **Wrong field names.** Markdown described record fields that the schema does not have.
- **Wrong attribution.** Text credited `tool_choice` with enforcing a structured record.
  The tool schema enforces the fields; `tool_choice` is the backstop for a turn that ends
  without the call, and it rarely fires live.
- **Path not reported.** A run could reach the goal by the voluntary path or the forced
  path. The notebook asserted the forced path and was usually wrong.
- **Negative-phrased metrics.** A comparison helper labels True-to-False as "REGRESSED".
  Metrics phrased as defects (`pii_in_log`) print "REGRESSED" when the fix works. Phrase
  them as properties (`free_of_pii`).
- **Substring audits.** An infrastructure notebook checked that flag names appear in a CI
  file. They also appear in the file's comments, so the check proves nothing.
- **Unit confusion.** A budget documented as characters that the code compares in
  estimated tokens, or vice versa.
- **Compaction that cannot fire.** A "stress" cell whose entries were too long for the
  compaction rule to ever bring the summary under budget, because compaction drops whole
  entries and never shortens one.
- **Duplicate or dead cells.** A markdown cell pasted twice, or a cell whose output is
  never referenced.

### 5.3 The two fix recipes that did most of the work

**Recipe A: replay a fixed transcript through the real loop.**
Build a fake client that returns scripted assistant turns and records every call. Replay
the same transcript under the anti-pattern (no callbacks) and the correct pattern
(callbacks on). The loop, dispatch, handlers, callbacks, and stores are all real; only the
model is scripted. The diverging turn is visible on every run, costs nothing, and needs no
API key. In this project the helpers were `tool_turn`, `text_turn`, `escalation_turn`,
`scripted_client(transcript, on_forced)`, and `tool_result_for`. The scripted client
returns `on_forced` for any call that pins `tool_choice`, which is exactly the call only
the correct pattern makes.

Use this for any guarantee that only matters on the rare run: a case dropped after a
clarifying question, a card number written into a log call, a refund attempted despite a
review flag.

**Recipe B: test the store, not the transcript.**
Every rule protects a persistent store: the escalation queue, the financial ledger, the
audit log. After a run, assert on the store. `len(financial_system.get_processed()) == 0`
proves the veto. `len(escalation_queue.get_escalations()) == 1` proves a human will see the
case. `CARD_PATTERN.search(entry.details) is None` for every audit entry proves redaction.
Never assert on Claude's wording, and never assert on the return value of a tool when the
store is available.

### 5.4 Smaller fixes that recur

- Put the customer identifier in the user message for both runs.
- Build the uncached baseline from the same prompt text as the cached run.
- Share one service container across turns of a session.
- Inject the accumulated context into the system prompt each turn, and measure the tokens
  actually sent.
- Print which path a live run took (`stop_reason == "escalated"` means forced; a
  non-empty queue with any other stop reason means voluntary) and say in markdown that the
  forced path rarely runs live.
- Use the real field names from the schema, and count them.
- Add a public `estimate_cost(usage)` helper and a cost row in cost comparisons.
- Keep any cell tags and phrase counts that the notebook tests require.

### 5.5 Where the code itself was wrong

One finding was a real gap, not a demo problem: a PostToolUse callback cannot fire when
no tool was called, so a run in which Claude ended its turn with a clarifying question
dropped a case that required escalation. The fix was in the agent loop, not the notebook:
at turn end, if any escalation flag is set and the queue is empty, force the escalation
call. Expect to find one or two of these. When you do, add the failing test first, fix the
code, then update every document that described the old behavior.

## 6. Phase 2: fix, prove, commit (per notebook)

1. Edit the notebook JSON with a script: locate cells by index or by a unique substring,
   replace source, clear outputs and execution counts. Assert each `old` string occurs
   exactly once before replacing.
2. Re-execute headlessly with the skip-tag override. Read the outputs of every cell you
   touched.
3. Add tests. For notebook fixes, structural tests over the cell source (the fixed
   construct is present, the broken one is absent) plus behavioral tests that mirror what
   the notebook does with simulated services.
4. Run the full suite and the linter.
5. Commit only the files for this notebook. Message: `fix(nbNN): <what changed>` with
   bullets.
6. Push.

Every claim in your report must name its proof: a test name, a command, or a cell output.

## 7. Phase 3: the documentation sweep

After the notebooks, the documents that describe them are stale by construction. Sweep
them in this order, one document per request from the owner, and report before editing.

### 7.1 Tutorial code excerpts

Diff every fenced code block against the source it claims to quote. The excerpts here
were wrong about the loop's signature, the callback registry's arguments, the flag table,
the regex, the policy document's headings, and a claim that callbacks were built by
default. Rewrite each block from the source. Keep the fence count unchanged as a sanity
check.

### 7.2 Tutorial prose

Read every sentence that makes a checkable claim and check it. The ones that failed here:
the data-flow diagram put callbacks before dispatch; a sentence described a regex with
optional separators next to an excerpt that requires them; a paragraph sat in the wrong
section; a tool name that does not exist; a compaction-timing claim that a twenty-turn
simulation disproved; two inconsistent size figures for the same measurement. Simulate
when a claim is numeric.

### 7.3 README

Check paths exist, commands exist, counts match, markers exist. Stale here: a root
`CLAUDE.md` that did not exist, a nightly cron that was commented out, a `pytest -m
integration` marker that no test carried, a hard-coded test count, and the same
before-execution and `tool_choice` claims as the tutorial. Drop counts that go stale on
every commit rather than updating them.

### 7.4 The meta-teaching notebook and the rules reference

These make claims about Claude Code itself. Verify them against the current docs, not
memory. The claims that were wrong in this project:

| Claim in the project | What the docs say |
|---|---|
| A skill lives at `.claude/skills/<name>.md` | `.claude/skills/<name>/SKILL.md`; flat files are not discovered |
| Skills are invoked by a sentence | `/<name>` slash command, or automatically from the `description` frontmatter |
| `--bare` means "no CLAUDE.md" | Skips hooks, skills, commands, subagents, plugins, MCP servers, auto memory, CLAUDE.md, and OAuth login |
| `--allowedTools` sandboxes | Pre-approves the listed tools; pair with `--permission-mode dontAsk` to deny the rest |
| `-p` is mandatory | Makes the run non-interactive; an unattended run without it waits for input |
| Project CLAUDE.md is only `.claude/CLAUDE.md` | `./CLAUDE.md` or `./.claude/CLAUDE.md` |
| `CLAUDE.local.md` is gitignored | You add it to `.gitignore` yourself |

Live proof is available for the skill layout: after moving the file, the skill appears in
the session's skill list.

Also replace any substring audit of a CI file with a parse of the actual command: strip
comment lines, extract the `claude` invocation, tokenize its arguments, and check flag
values. Add a test that a comment-only copy of the file fails the audit.

For the rules reference, which is an extract of the exam articles, keep each exam signal
as written and correct only the mechanics beside it.

### 7.5 Everything else

Historical planning documents (`.planning/phases/`, milestone audits) record what was true
when written. Leave them.

## 8. Proof rules

Adopted from the project's `CLAUDE.md` and applied to every commit:

- Test the store, not the API response.
- Every completion claim needs executable proof, stated as "claim: test name or command".
- Structural tests (file exists, cell count) are necessary, not sufficient. Add a
  behavioral test.
- Before marking work complete: persistent state tested, the actual runtime path tested,
  a failing test added before the fix, and any notebook or example calling only APIs that
  exist.
- Report outcomes faithfully. If a live run took the voluntary path, say so. If a step was
  skipped, say so.

## 9. Checklist

Per notebook:

- [ ] Dumped all cells; read code against source; every name and field verified
- [ ] Executed once headlessly with the skip-tag override; read comparison outputs
- [ ] Classified defects against Section 5.2
- [ ] Reported assessment; waited for "fix"
- [ ] Applied fixes by script; outputs cleared; required tags and phrases preserved
- [ ] Re-executed; outputs read
- [ ] Tests added (structural and behavioral); suite and lint green
- [ ] Committed only this notebook's files; pushed

Documentation sweep:

- [ ] Tutorial excerpts diffed against source; fence count unchanged
- [ ] Tutorial prose checked claim by claim; numeric claims simulated
- [ ] README paths, commands, counts, and markers verified
- [ ] Claude Code claims verified against current docs (Section 7.4 table)
- [ ] CI audit parses the command, not the file text
- [ ] Rules reference: signals kept, mechanics corrected

Always:

- [ ] Scope limited to the named file
- [ ] No repeated live runs without being asked
- [ ] No secrets printed
- [ ] Commit message without AI attribution
- [ ] Pre-existing changes and untracked files left out

## 10. Worked example: what was done on this project

Commits on 2026-09-17, in order. Each line is the change and the proof that gated it.

| Commit | Change | Proof |
|---|---|---|
| `7d7efca` | Notebooks load `.env` before creating the client | Notebook executes without a preset key |
| `d99938c` | NB01 markdown describes observed anti-pattern behavior | Live run outputs |
| `743cd6c` | Anti-pattern docstrings aligned with observed behavior | Read against runs |
| `7bdbfb6` | Agent loop enforces escalation at turn end; NB01 replays two fixed transcripts with a scripted client | Turn-end tests; queue length under both patterns |
| `55d97e0` | NB02 shows the compliance callback with a deterministic replay | Audit log store checked for the card pattern |
| `2404d94` | NB03 puts the customer ID in the message; compares input tokens | Fair runs; token rows |
| `dd85ffc` | NB04 fair uncached baseline; cost row; `estimate_cost` helper | Four helper tests; usage rows |
| `65ddda9` | NB05 sends accumulated context each turn; measures what is sent; stress cell sized so compaction can fire | Structural tests over cell source; per-turn table |
| `ecc0157` | NB06 attributes the record to the tool schema; real field names; reports which path ran | Positive-phrased metrics; tags preserved |
| `d62d1fb` | NB07 verifies each pattern against the store; runs with the cached prompt; removes a duplicate cell | Store assertions per pattern |
| `fcc7af6` | Tutorial code excerpts rewritten from source | Fence count unchanged |
| `fa1e42a` | NB08 and skill: SKILL.md layout with frontmatter, docs-accurate flags, `--permission-mode dontAsk` in CI, parsed flag audit, hierarchy table, coordinator row, root resolution | `TestNB08MetaTeaching`; skill appears in session list |
| `5b9f415` | README stale references removed | Suite green; strings grepped |
| `bd01d47` | Tutorial prose corrected | Fence count unchanged; simulation for compaction claim |
| `d17376f` | Rules reference mechanics aligned with docs; signals kept | Read against docs |

## 11. Reusable snippets

Dump cells:

```python
import json
nb = json.load(open("notebooks/NN.ipynb"))
for i, c in enumerate(nb["cells"]):
    print(f"===== CELL {i} [{c['cell_type']}] tags={c['metadata'].get('tags')}")
    print("".join(c["source"]))
```

Edit cells by script, clearing outputs:

```python
def set_src(i, text):
    lines = text.split("\n")
    cells[i]["source"] = [l + "\n" for l in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    if cells[i]["cell_type"] == "code":
        cells[i]["outputs"] = []
        cells[i]["execution_count"] = None

def rep(i, old, new):
    s = "".join(cells[i]["source"])
    assert s.count(old) == 1, (i, old[:60])
    set_src(i, s.replace(old, new))
```

Scripted client (shape; adapt tool names to the domain):

```python
def scripted_client(transcript, on_forced):
    remaining = list(transcript)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if "tool_choice" in kwargs or not remaining:
            return on_forced
        return remaining.pop(0)
    return SimpleNamespace(messages=SimpleNamespace(create=create), calls=calls)
```

Parse a CI command instead of grepping the file:

```python
code_only = "\n".join(l for l in ci.splitlines() if not l.lstrip().startswith("#"))
match = re.search(r"claude \\\n(.*?)\n\s*\"", code_only, re.DOTALL)
argv = match.group(1).replace("\\", " ").split()
assert argv[argv.index("--output-format") + 1] == "json"
```

Store assertions after a run:

```python
assert len(services.financial_system.get_processed()) == 0
assert len(services.escalation_queue.get_escalations()) == 1
for entry in services.audit_log.get_entries():
    assert CARD_PATTERN.search(entry.details) is None
```

Replace the store names, the pattern, and the tool names with the new domain's. The
shape of the proof does not change.
