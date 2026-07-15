# GitLab Issue Validity Review — Design

A `/review-issues` command that fetches GitLab issues from a given project
(matching an ID range and a tag), checks each issue's **validity** against the
committed code, and produces a report classifying every issue as a real issue or
a false positive — then writes the verdict back to GitLab as labels.

It mirrors `/audit`'s staged, two-gate-confirmation approach, with one
fundamental inversion: **the issues are the proposed findings.** `/audit`'s
Phase 3 *discovers* candidate bugs in source; here the issues already *are* the
claims. The pipeline's job is to normalize each issue into a structured,
verifiable claim, then run the same two confirmation gates (grounding +
adversarial refutation) on the *verdict* before it is written back.

---

## 1. What changes from `/audit`, and what does not

### Reused from `audit.py` / `review.py`

| Concern | Symbol | Reuse |
|---|---|---|
| Git helpers | `_git`, `_git_show`, `_git_show_many` | as-is for HEAD; commit-parameterized variant for closed issues (§8.3) |
| Content cache | `_cached_git_show` pattern | as-is, scoped per-commit |
| Import/caller/symbol indices | `_build_context_indices` | as-is, rebuilt per commit baseline |
| Cross-file callee context | `_gather_callee_context`, `_gather_evidence` | as-is |
| Record parsing + repair | `PhaseSchema`, `RecordSchema`, `_parse_records_with_repair` | as-is |
| LLM call wrapper | `_call_audit_llm`, `_write_audit_trace` | as-is |
| Parallel batch | `_run_batch` | as-is |
| Isolated worktree | `_worktree` | variant that checks out a specific commit (§8.3) |
| Verdict parsing | `_parse_verdict_line`, `_TransientVerifierError` | as-is |
| Severity helpers | `_demote_only`, `_consensus_severity`, `_SEVERITIES` | as-is |
| Live UI | `AuditUI`, `PhaseHandle` | as-is, new phase titles |
| Resumable state | mirror `AuditRunState.save()/load()/find_resumable()` | new dataclass (§11) |

### New in `review_issues.py`

- **GitLab API client** — a thin wrapper over the GitLab REST v4 API (stdlib
  `urllib`, no new dependency) to fetch issues, closing commits, and write
  labels (§12). No MCP server required.
- **Issue-as-claim model** — issues are normalized into structured claims (§7),
  replacing `/audit`'s Phase 3 inventory/expansion.
- **Commit-parent verification** — closed issues are verified at the *parent of
  the closing commit* (was it real at the time?) and at the *closing commit*
  (is it actually fixed?), yielding the `real::fixed` vs `real::unfixed`
  distinction (§8).
- **Verdict space** — five outcomes plus `not-applicable` (§3).
- **Label write-back** — applies verdict labels to GitLab issues via the API
  client (§10).

---

## 2. Command surface

```
/review-issues <project> --tag <tag> [--from N] [--to M] [--state open|closed|all]
                [--workers N] [--resume] [--dry-run] [--debug]
```

- `<project>` — GitLab project path (`group/sub` or numeric ID).
- `--tag <tag>` — only issues carrying this label.
- `--from N` / `--to M` — inclusive issue-IID range (default: all matching).
- `--state` — `open` (default), `closed`, or `all`.
- `--workers N` — parallel verification workers (default 4).
- `--resume` — resume an interrupted run from its checkpoint.
- `--dry-run` — run the full pipeline and produce the local report, but **do not
  write labels back to GitLab**. (Recommended for a first pass.)
- `--debug` — JSONL debug log to `.swival/review-issues/debug.jsonl`.

Entry point mirrors `run_audit_command`:

```python
def run_review_issues_command(cmd_arg: str, ctx: InputContext) -> str: ...
```

Registered in `input_commands.py` as `"/review-issues"` and dispatched in
`agent.py` alongside `/audit`.

---

## 3. The validity verdict space

The central design decision. An issue is not simply "real" or "false positive" —
*when* it was real matters, and for closed issues *whether the fix actually
fixed it* matters.

### 3.1 Verdicts

| Verdict | State | Meaning | Evidence |
|---|---|---|---|
| `real::open` | open | The claim reproduces at HEAD; the issue describes a real current problem. | Claim confirmed at HEAD. |
| `real::fixed` | closed | The claim **was** real at the parent of the closing commit, and the closing commit **actually fixes** it (no longer reproduces). | Claim confirmed at parent C^; refuted at closing commit C. |
| `real::unfixed` | closed | The claim was real at the parent, and **still reproduces** at the closing commit — the issue was closed but not actually fixed. | Claim confirmed at parent C^ **and** at closing commit C. |
| `false-positive::never-real` | either | The claim does not hold against the code — not at HEAD (open), not even at the parent (closed). The issue was never a real problem. | Claim refuted at the baseline. |
| `not-applicable` | either | The issue is not a bug-type claim (feature request, question, docs, discussion). Not subject to validity. | Classification gate (§6). |

### 3.2 Why two checks for closed issues

The user's requirement (#3): a closed issue should still be checked for
legitimacy, by examining the closing commit **and its parent**. This yields two
independent questions:

1. **Was it real at the time?** — verify the claim against `C^` (the code state
   when the issue was open and being fixed). If the claim doesn't hold even
   there, the issue was a false positive that got closed (perhaps "won't fix" or
   closed by mistake).
2. **Did the fix actually fix it?** — verify the claim against `C` (the code
   state after the fix). If the claim *still* reproduces at `C`, the issue was
   real but the close was premature — a regression risk.

Combining the two:

| | real at C^ (parent) | not real at C^ |
|---|---|---|
| **not real at C** (fixed) | `real::fixed` | `false-positive::never-real` |
| **real at C** (unfixed) | `real::unfixed` | `false-positive::never-real` |

`false-positive::never-real` dominates: if the claim was never real, the
fix-status question is moot.

### 3.3 Label scheme (write-back)

Two orthogonal label axes, applied via MCP after adjudication:

- **`validity::real`** / **`validity::false-positive`** / **`validity::not-applicable`**
- **`fix-status::open`** / **`fix-status::fixed`** / **`fix-status::unfixed`**
  (omitted for `not-applicable`)

This composes cleanly: `real::open` → `validity::real` + `fix-status::open`;
`real::fixed` → `validity::real` + `fix-status::fixed`; `real::unfixed` →
`validity::real` + `fix-status::unfixed`; `false-positive` →
`validity::false-positive` (no fix-status). The design creates labels if absent
(§10.3) and is idempotent on re-run (re-applying an existing label is a no-op).

---

## 4. Pipeline overview

```
Phase 1  Fetch          fetch issues via the GitLab API (project, tag, IID range, state)
                        ───────────────────────────────────────► queued_issues
Phase 2  Classify       per issue: is this a bug-type claim?
                        auto-skip feature requests / questions / docs
                        ───────────────────────────────────────► candidate_issues
Phase 3  Extract        per candidate: normalize issue body+metadata
                        into a structured, verifiable claim
                        ───────────────────────────────────────► proposed_claims
Phase 4  Grounding      per claim: independent verifier checks against code
                           open  → verify at HEAD
                           closed → verify at parent C^ AND at closing commit C
                        CONFIRMED / REFUTED at each baseline
                        ───────────────────────────────────────► verified_claims (+ verdict)
Phase 4.5 Adjudication  per verdict: adversarial refutation panel
                        three lenses (grounding, fix-validity, verdict-fit)
                        default-to-reject; strict majority to keep
                        ───────────────────────────────────────► final verdicts
Phase 5  Write-back     apply GitLab labels via the API client + produce markdown report
```

State transitions are phase-gated as in `/audit`: each phase sets `state.phase`
and calls `state.save()` so `--resume` re-enters at the right point.

---

## 5. Phase 1 — Issue fetch (MCP)

### 5.1 The MCP adapter

All GitLab access goes through `ctx.mcp_manager.call_tool`. Because MCP tool
names vary by server installation, the adapter is **config-driven**: a
`[review-issues]` table in `swival.toml` (or sensible defaults) maps logical
operations to actual MCP tool names + argument shapes.

```toml
[review-issues]
gitlab_server = "gitlab"          # MCP server name (the mcp__ prefix)
tool_list_issues = "list_issues"  # tool name on that server
tool_get_issue  = "get_issue"
tool_get_issue_notes = "get_issue_notes"
tool_list_closed_by = "list_issue_merge_requests"
tool_get_merge_request = "get_merge_request"
tool_apply_label = "set_issue_labels"
```

The adapter (§12) exposes typed Python methods (`list_issues`, `get_issue`,
`closing_commit_for`, `apply_labels`) that internally call the configured MCP
tool and parse the returned text. This isolates the pipeline from MCP tool-name
drift and makes the design testable with a stub adapter.

### 5.2 Fetch

`adapter.list_issues(project, tag, state, iid_from, iid_to)` returns a list of
issue dicts (iid, title, description, state, labels, web_url). Each becomes a
`GitLabIssue` dataclass. Pagination is handled inside the adapter (page through
until exhausted). The phase reports how many issues matched.

---

## 6. Phase 2 — Classification (bug vs not-applicable)

A single LLM call per issue classifies its type, mirroring `/audit`'s triage
precision-over-recall posture but for a different purpose: deciding whether
validity *applies*.

### 6.1 Classification schema

```python
_ISSUE_CLASS_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="classification",
        required=("issue_type", "confidence"),
        enums={"issue_type": ("bug", "feature_request", "question",
                              "docs", "discussion", "other"),
               "confidence": ("high", "medium", "low")},
    ),
    cardinality="one",
    allow_none=False,
)
```

### 6.2 Classification prompt (essence)

> You are classifying one GitLab issue by type. Read the title and description.
> `bug` = reports a defect, incorrect behavior, crash, or broken functionality
> that is verifiable against source code. Everything else (feature requests,
> questions, docs, discussions, planning) is `not-applicable` for validity
> review. When unsure, lean `bug` only if the issue makes a concrete claim about
> code behavior that can be checked; otherwise classify by its primary intent.

Issues classified non-`bug` are tagged `not-applicable` and skip to Phase 5
(included in the report, no verification gates run). This implements the
user's requirement (#4): a noisy input tag doesn't waste verification effort on
feature requests.

---

## 7. Phase 3 — Claim extraction

The issue body is free text. Before it can be verified, it must be normalized
into a **structured claim** — the analogue of `/audit`'s Phase 3a/3b, but
operating on issue text instead of source code.

### 7.1 Claim schema

```python
_CLAIM_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="claim",
        required=("summary", "expected", "observed",
                  "suspected_location", "claim_statement"),
        repeated={"suspected_location": "suspected_locations"},
        multiline=("observed", "claim_statement"),
    ),
    cardinality="one",
    allow_none=False,
)
```

- `summary` — one-line restatement of what the issue claims is wrong.
- `expected` — what the issue says should happen.
- `observed` — what the issue says actually happens (crash, wrong output, etc.).
- `suspected_locations` — `path:line` or `path:symbol` references the issue
  mentions (may be empty; the verifier will search if absent).
- `claim_statement` — the falsifiable proposition: "X happens because Y, under
  conditions Z." This is what Phase 4 treats as the hypothesis.

### 7.2 Extraction prompt (essence)

> You are extracting a verifiable claim from one GitLab issue. Use only the
> issue title, description, and notes. Produce a structured claim a later phase
> will verify against source code. If the issue does not make a concrete,
> falsifiable claim about code behavior, set `claim_statement` to
> `NOT-FALSIFIABLE` — the issue is too vague to verify.

Issues extracted as `NOT-FALSIFIABLE` are routed to a `false-positive::never-real`
candidate (the claim cannot be verified because it is too vague) — but the
adjudication gate (§9) may downgrade this to `not-applicable` if the vagueness
means it was really a discussion, not a bug claim.

### 7.3 Evidence seeding

For each claim, Phase 3 also produces an initial **evidence seed**: the files
and symbols referenced in `suspected_locations`, resolved against the repo file
set. If the issue names no locations, the seed is empty and the Phase 4 verifier
must locate the relevant code itself (it has repo-wide search in its worktree).

---

## 8. Phase 4 — Grounding (first confirmation gate)

The core verification. Each claim is treated as a **hypothesis** and checked
against committed code. The baseline depends on issue state.

### 8.1 Open issues — verify at HEAD

For `state == open`, the claim is verified against current HEAD, exactly as
`/audit` Phase 4 verifies a finding:

- `_worktree` at HEAD + `_gather_evidence` (callee-context-enriched bundle).
- Verifier prompt treats the claim as a hypothesis; may inspect code and run
  small PoCs in the isolated worktree.
- Emits `CONFIRMED` (claim reproduces at HEAD → `real::open`) or `REFUTED`
  (claim does not hold → `false-positive::never-real`) on its own line.

### 8.2 The verdict prompt (open)

> You are verifying one GitLab issue's claim against the committed source at
> HEAD in an isolated worktree. Determine whether the claim describes a real
> defect that manifests in the current code. Treat the claim as a hypothesis,
> not ground truth.
>
> A proof counts if you can identify the trigger path, the failing operation or
> violated invariant, and the practical incorrect/crashing/corrupting outcome
> from the code, or demonstrate equivalent runtime evidence.
>
> Reject as REFUTED when the code does not support the claim, or when an
> existing guard already prevents the described behavior.
>
> End with exactly one token on its own line: `CONFIRMED` or `REFUTED`.

### 8.3 Closed issues — verify at parent AND closing commit

This implements the user's requirement (#3). Two sub-verifications:

**Step A — find the closing commit C.**

`adapter.closing_commit_for(issue)` resolves the commit that closed the issue, in
priority order:
1. GitLab `closed_by` merge requests → each MR's `merge_commit` (via MCP).
2. Commit-message references: `git log --all --grep="(?:Fixes|Closes|Resolves)\s+#<iid>"`
   (regex over the repo), taking the earliest matching commit.
3. If neither resolves, the issue is tagged `verdict::deferred` (cannot verify
   against a closing commit) and falls back to HEAD verification with a warning.
   It appears in the report as "closed but no closing commit found — verified at
   HEAD only."

**Step B — verify at the parent C^.**

`C^ = git rev-parse C^`. A **commit-parameterized worktree** checks out C^:

```python
class _worktree_at:
    """Context manager for a temporary git worktree at a specific commit."""
    def __init__(self, base_dir, work_dir, commit):
        ...
    def __enter__(self):
        _git(["worktree", "add", "--detach", str(self.work_dir), self.commit], ...)
        return self.work_dir
```

Evidence is gathered at C^ via a commit-parameterized `_git_show_at(path, commit,
base_dir)` (the only change from `_git_show` is `HEAD:` → `<commit>:`). The
verifier checks the claim against the code state *when the issue was open*. This
answers: **was it a legitimate issue at the time?**

**Step C — verify at the closing commit C.**

Same mechanism, worktree at C. The verifier checks whether the claim *still
reproduces* after the fix. This answers: **did the fix actually fix it?**

**Step D — combine.**

| Claim at C^ (parent) | Claim at C (closing) | Verdict |
|---|---|---|
| CONFIRMED | REFUTED | `real::fixed` — was real, now fixed |
| CONFIRMED | CONFIRMED | `real::unfixed` — closed but not actually fixed |
| REFUTED | (skip) | `false-positive::never-real` — never real |

When C^ is REFUTED, Step C is skipped (never-real dominates). Each sub-check is
an independent grounding call with its own worktree; both must pass the
adjudication gate (§9).

### 8.4 Retry & state

Mirrors `_verify_one_finding`: transient errors (`_TransientVerifierError`)
retry once; a verified claim becomes a `VerifiedClaim` carrying its verdict and
the commit baselines used. Refuted claims are recorded in
`verification_state[iid]`. The claim key is content-based (issue iid + claim
hash) so resume is stable.

---

## 9. Phase 4.5 — Adjudication (second confirmation gate)

The adversarial refutation panel, analogous to `_phase45_adjudicate`. Even after
grounding, a verdict is stress-tested: is it *justified*?

### 9.1 Three lenses

```python
_VALIDITY_LENSES = (
    "Lens: grounding & accuracy. Re-read the cited code at each baseline yourself. "
    "Is the claim actually confirmed/refuted as the verdict states? Did the "
    "verifier misread the code or miss a guard? If the verdict contradicts the "
    "code, it is a false_positive (i.e. reject the verdict).",

    "Lens: fix-validity (closed issues only). For a `real::fixed` verdict, "
    "confirm the closing commit genuinely addresses the described claim, not "
    "just an adjacent symptom. For `real::unfixed`, confirm the claim truly "
    "still reproduces at C and the close wasn't legitimate for a different "
    "reason. For open issues this lens is a no-op pass.",

    "Lens: verdict-fit & significance. Is the verdict label correct for the "
    "evidence, and does this issue deserve a validity verdict at all? A vague "
    "issue forced into `false-positive::never-real` might better be "
    "`not-applicable`. Recalibrate the verdict to the realistic evidence; "
    "tie-break toward the more conservative reading.",
)
```

### 9.2 Verdict schema & default-to-reject

```python
_VALIDITY_VERDICT_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="verdict",
        required=("verdict", "verdict_fit", "reason"),
        enums={"verdict": ("real::open", "real::fixed", "real::unfixed",
                           "false-positive::never-real", "not-applicable"),
               "verdict_fit": ("yes", "no")},
    ),
    cardinality="one",
    allow_none=False,
)
```

The system prompt mirrors `_PHASE45_REVIEW_SYSTEM`'s adversarial posture:
default to rejecting the *proposed verdict* unless the evidence forces it. A
strict majority of usable verdicts must confirm (`verdict_fit == "yes"`); ties
drop to the more conservative verdict. The panel may **recalibrate** the verdict
(e.g. downgrade `real::fixed` to `false-positive::never-real` if the
fix-validity lens shows the claim was never real), but only toward more
conservative outcomes — the **demote principle** carries over: a verdict may be
weakened, never strengthened.

### 9.3 Conservative-recalibration order

For verdicts, "more conservative" means *less accusatory toward the code*:

```
real::unfixed  →  real::fixed  →  real::open*  →  false-positive::never-real  →  not-applicable
(* real::open only for open issues)
```

The panel may move a verdict rightward (toward not-applicable) but never
leftward (toward "real and unfixed"). This mirrors `/audit`'s demote-only
severity: the gate exists to catch over-claims, not to inflate them.

---

## 10. Phase 5 — Label write-back & report

### 10.1 Label write-back (MCP)

For each final verdict, `adapter.apply_labels(issue_iid, labels)` calls the
configured `set_issue_labels` MCP tool. Label set per verdict (§3.3):

| Verdict | Labels applied |
|---|---|
| `real::open` | `validity::real`, `fix-status::open` |
| `real::fixed` | `validity::real`, `fix-status::fixed` |
| `real::unfixed` | `validity::real`, `fix-status::unfixed` |
| `false-positive::never-real` | `validity::false-positive` |
| `not-applicable` | `validity::not-applicable` |

`--dry-run` skips this step but still lists what *would* be applied.

### 10.2 Label creation

`adapter.ensure_labels_exist(project, label_names)` checks (via a list-labels
MCP call) whether each label exists in the project and creates it (via a
create-label MCP call) if absent, with a consistent color scheme:

- `validity::real` — red (#d9534f)
- `validity::false-positive` — gray (#6c757d)
- `validity::not-applicable` — light gray (#e9ecef)
- `fix-status::open` — orange (#f0ad4e)
- `fix-status::fixed` — green (#5cb85c)
- `fix-status::unfixed` — red (#d9534f)

### 10.3 Idempotency

Re-applying an existing GitLab label is a no-op at the API level, so re-runs
(verify a refreshed issue set) are safe. The adapter reads the issue's current
labels, computes the diff, and only calls the set-labels tool if labels would
change — minimizing write traffic.

### 10.4 Report

A markdown report (`review-issues-findings/README.md`) with one row per issue:

| IID | Title | State | Verdict | Baseline | Confidence | Link |
|---|---|---|---|---|---|---|
| 42 | Login crashes on empty email | open | `real::open` | HEAD | high | [→](url) |
| 17 | Off-by-one in pagination | closed | `real::fixed` | C^=abc123, C=def456 | high | [→](url) |
| 23 | Export fails on large files | closed | `real::unfixed` | C^=abc123, C=def456 | medium | [→](url) |
| 88 | Dark mode colors wrong | open | `false-positive::never-real` | HEAD | high | [→](url) |
| 91 | Add SSO support | open | `not-applicable` | — | high | [→](url) |

Grouped by verdict, with a summary tally. Each issue links to its GitLab URL.
The report is the command's return string (summary) plus the written file (full).

---

## 11. State & resumability

`IssueReviewRunState` mirrors `AuditRunState`:

```python
@dataclass
class IssueReviewRunState:
    run_id: str
    project: str
    tag: str
    iid_range: tuple[int, int | None]
    state_filter: str               # open | closed | all
    queued_issues: list[GitLabIssue]
    classified_issues: dict[int, IssueClassification]   # iid → classification
    candidate_iids: list[int]       # bug-type issues
    proposed_claims: dict[int, Claim]                   # iid → structured claim
    verified_claims: dict[int, VerifiedClaim]           # iid → verdict + baselines
    final_verdicts: dict[int, str]                      # iid → final verdict
    written_labels: dict[int, list[str]]                # iid → labels applied
    closing_commits: dict[int, str]                     # iid → closing commit C (closed issues)
    verification_state: dict[int, dict]                 # iid → per-baseline status
    adjudication_state: dict[int, dict]
    phase: str   # fetch → classify → extract → grounding → adjudication → writeback → done
    metrics: dict[str, int]
    dry_run: bool
    ...
```

`save()`/`load()`/`find_resumable()` are structurally identical to
`AuditRunState`'s — atomic temp-file rename, JSON blob, project+tag+range
matching for resume. State lives under `.swival/review-issues/<run_id>/state.json`.
Resume re-fetches issues only to confirm the set hasn't changed (iid+updated_at
fingerprint); it does not re-run completed phases.

---

## 12. The MCP adapter

A single class isolating all GitLab access, so the pipeline never calls
`ctx.mcp_manager` directly:

```python
class GitLabAdapter:
    """Config-driven wrapper over the GitLab MCP server."""

    def __init__(self, mcp_manager, config: dict):
        self._mcp = mcp_manager
        self._server = config["gitlab_server"]
        self._tools = config  # tool name mapping

    def list_issues(self, project, tag, state, iid_from, iid_to) -> list[dict]: ...
    def get_issue(self, project, iid) -> dict: ...
    def get_issue_notes(self, project, iid) -> list[dict]: ...
    def closing_commit_for(self, issue, base_dir) -> str | None: ...
    def ensure_labels_exist(self, project, label_names) -> None: ...
    def apply_labels(self, project, iid, labels) -> None: ...

    def _call(self, tool_key: str, **args) -> str:
        namespaced = f"mcp__{self._server}__{self._tools[tool_key]}"
        result, is_error = self._mcp.call_tool(namespaced, args)
        if is_error:
            raise GitLabAdapterError(result)
        return result
```

`closing_commit_for` is the one method that may fall back to local git
(`git log --grep`) when the MCP path doesn't resolve a merge commit — it takes
`base_dir` for that fallback. All other methods are pure MCP calls. The adapter
parses MCP text responses into dicts; parsing is lenient (best-effort field
extraction) because MCP tool response shapes vary.

If `ctx.mcp_manager` is None or the configured server is absent, the command
fails fast with a clear message: "no GitLab MCP server configured — add one to
swival.toml `[mcp_servers]` or `.swival/mcp.json`."

---

## 13. The confirmation guarantee, summarized

Every verdict written back to GitLab has passed **two independent gates**, each
defaulting to reject:

| Gate | Question | Mechanism | Default |
|---|---|---|---|
| Phase 4 — Grounding | "Does the claim actually hold against the code at the right baseline?" | Independent verifier in an isolated worktree; open→HEAD, closed→parent C^ **and** closing commit C. | REFUTED unless CONFIRMED |
| Phase 4.5 — Adjudication | "Is the verdict justified?" | Three-lens adversarial panel (grounding, fix-validity, verdict-fit), default-to-reject, strict majority, conservative-recalibrate-only. | reject the proposed verdict unless confirmed |

Plus the structural safeguards inherited from `/audit`:
- **Commit-parent verification** — closed issues are checked at *two* baselines,
  so "was it ever real" and "is it actually fixed" are answered independently.
  This is the design's distinctive contribution: it distinguishes a
  well-closed real bug (`real::fixed`) from a prematurely-closed one
  (`real::unfixed`) from a false positive that wasted a fix cycle
  (`false-positive::never-real`).
- **Conservative-recalibrate-only** — the panel may weaken a verdict (toward
  `not-applicable`), never strengthen it (toward `real::unfixed`).
- **Evidence-grounded prompts** — every phase gets committed source at the
  correct commit; no speculation.
- **Non-bug auto-skip** — feature requests and questions never enter the
  verification gates; they're classified `not-applicable` up front.
- **Idempotent, diff-based write-back** — labels are only written when they'd
  change; re-runs are safe.
- **Resumable, phase-gated state** — a verdict is written back only after both
  gates are recorded in state; resume cannot skip a gate or skip the write-back.
- **`--dry-run`** — the full pipeline runs and the report is produced, but no
  labels touch GitLab. Recommended for the first pass on any project.

This applies `/audit`'s "confirm each finding before keeping it" discipline to a
new question: *was this issue ever a real bug, and if it's closed, did the fix
actually fix it?* — answered against the code at the commit that matters, not
the issue author's claim.
