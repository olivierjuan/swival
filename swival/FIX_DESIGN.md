# Fix GitLab Issues — Design

A `/fix` command that fetches **open** GitLab issues from a given project
(matching a tag, optionally an IID range), **fixes** each one, **verifies** the
fix, checks that **documentation / spec** stay in sync, **commits** the fix to
the repository, **closes** the issue in GitLab, and updates the findings
**README**.

It is built by analogy with `/review-issues` and `/review`, reusing their
building blocks verbatim and swapping only the domain layer: the fixer, the
fix-verifier, and the commit/close/write-back step.

---

## 1. What changes from `/review-issues`, and what does not

### Reused unchanged (import from `review_issues.py`)

| Concern | Symbol | Reuse |
|---|---|---|
| GitLab access | `GitLabAdapter`, `GitLabAdapterError` | as-is, plus one new method (§7) |
| Config | `_DEFAULT_TOOL_CONFIG`, `_load_review_issues_config` | as-is; `[fix]` inherits `[review-issues]` (§8) |
| Issue model | `GitLabIssue` | as-is |
| Claim model + extraction | `Claim`, `_CLAIM_SCHEMA`, `_CLAIM_SYSTEM`, `_phase_extract_claim` | as-is — an issue is normalized into the same structured claim |
| Commit-parameterized worktree | `_worktree_at`, `_git_show_at`, `_git_rev_parse` | as-is |
| Evidence gathering | `_gather_claim_evidence` | as-is |
| Verifier verdict parsing | `_parse_validity_verdict_line` (`CONFIRMED`/`REFUTED`) | as-is |

### Reused unchanged (import from `audit.py`)

| Concern | Symbol | Reuse |
|---|---|---|
| Git helper | `_git` | as-is |
| Isolated agent loop | `_make_isolated_loop_kwargs` | as-is |
| Parallel batch | `_run_batch` | as-is |
| Transient retry sentinel | `_TransientVerifierError` | as-is |
| Trace logging | `_write_audit_trace` | as-is |
| Live UI | `AuditUI`, `PhaseHandle` (from `audit_ui`) | as-is, new phase titles |

### New in `fix.py`

- **`FixRunState`** — phase-gated, resumable state (§6).
- **Fixer prompt + agent loop** — generates a minimal fix *and* updates docs/spec
  in an isolated worktree (§4).
- **Fix-verifier prompt + agent loop** — confirms the defect no longer
  reproduces *and* docs/spec are in sync (§5).
- **Commit & close** — applies the verified patch to the real repo, commits,
  closes the GitLab issue, applies `fix-status::fixed` (§7).
- **README** — per-issue fix/verify/commit/close status table (§9).

---

## 2. Command surface

```
/fix <project> --tag <tag> [--from N] [--to M] [--workers N]
              [--patch-max-turns N] [--resume] [--dry-run] [--debug]
```

- `<project>` — GitLab project path (`group/sub` or numeric ID).
- `--tag <tag>` — only **open** issues carrying this label.
- `--from N` / `--to M` — inclusive issue-IID range (default: all matching).
- `--workers N` — parallel fix/verify workers (default 4).
- `--patch-max-turns N` — fix-generation turn budget (default 50).
- `--resume` — resume an interrupted run from its checkpoint.
- `--dry-run` — run the full pipeline and produce the report, but **do not
  commit, close issues, or write labels**.
- `--debug` — JSONL debug log to `.swival/fix/debug.jsonl`.

The state filter is fixed to `opened`: `/fix` only fixes open issues. Entry
point mirrors `run_review_issues_command`:

```python
def run_fix_command(cmd_arg: str, ctx: InputContext) -> str: ...
```

Registered in `input_commands.py` as `"/fix"` and dispatched in `agent.py`
alongside `/review-issues` and `/review`.

---

## 3. Pipeline overview

```
Phase 1  Fetch          list open issues (project, tag, IID range) via MCP
                         ───────────────────────────────────────► queued_issues
Phase 2  Extract        per issue: normalize body+notes into a structured Claim
                         (reuses /review-issues claim extraction)
                         ───────────────────────────────────────► claims
Phase 3  Fix            per claim: agent loop in an isolated worktree@HEAD
                         produces the minimal fix + doc/spec updates;
                         capture git diff
                         ───────────────────────────────────────► fix_patches
Phase 4  Verify         per fix: apply patch to a fresh worktree@HEAD;
                         verifier confirms the defect no longer reproduces
                         AND docs/spec are in sync
                         CONFIRMED → fixed; REFUTED → not fixed / out of sync
                         ───────────────────────────────────────► verify_results
Phase 5  Commit & Close per verified fix: apply patch to the real repo,
                         git commit "Fix #N … Fixes #N", close the GitLab
                         issue, apply fix-status::fixed, update README
                         ───────────────────────────────────────► commit_results → done
```

State transitions are phase-gated exactly as in `/audit` and `/review-issues`:
each phase sets `state.phase` and calls `state.save()` so `--resume` re-enters
at the right point. State lives under `.swival/fix/<run_id>/state.json`;
artifacts under `fix-findings/`.

---

## 4. Phase 3 — Fix

The fixer runs in an **isolated worktree at HEAD** (reusing `_worktree_at`),
so generated changes are contained and never touch the real repository until
Phase 5. `ctx.tools` (including `edit_file`) are scoped to the worktree via
`_make_isolated_loop_kwargs`.

### 4.1 Fixer prompt (essence)

> You are fixing one GitLab issue in an isolated git worktree. The issue has
> been normalized into a structured claim. Use `edit_file` to make the minimal
> correct fix.
>
> - Make the smallest change that correctly resolves the described defect.
> - Update any documentation, spec, README, docstrings, or comments that must
>   stay consistent with the behavior you changed. If the fix alters documented
>   behavior, the docs/spec must reflect it.
> - Do not make unrelated changes, refactors, or style edits.
> - You may run tests or small checks to validate the fix.
> - You have no access to GitLab; work only in the worktree.

The fixer receives the issue title/description, the structured claim, and the
committed evidence bundle (via `_gather_claim_evidence` at HEAD). On exit, the
worktree's `git diff` is captured as the patch. Empty diff or turn-budget
exhaustion is recorded as a fix error; the issue is skipped downstream.

### 4.2 Why an isolated worktree, not the real repo

Containing the fix in a worktree (like `/review`'s `_review_patch`) means a
runaway fixer cannot corrupt the user's working tree. The patch is only applied
to the real repository in Phase 5, after verification.

---

## 5. Phase 4 — Verify

The verifier is the gate that decides whether a fix is good enough to commit.
It runs in a **fresh worktree at HEAD with the patch applied** (`git apply`),
so it sees exactly the fixed code and nothing else.

### 5.1 Verifier prompt (essence)

> You are verifying that one GitLab issue has actually been fixed in the code
> before you. The original claim and the applied patch are provided. Treat the
> fix as a hypothesis and confirm it independently.
>
> Confirm (`CONFIRMED`) only if BOTH hold:
> 1. The issue's described defect **no longer reproduces** in the fixed code.
> 2. **Documentation, spec, README, and comments** that describe the changed
>    behavior are consistent with the fix. If the fix changed documented
>    behavior and the docs were not updated, that is out-of-sync → `REFUTED`.
>
> Reject as `REFUTED` when the defect still reproduces, the fix is incomplete,
> or the fix introduced a doc/spec inconsistency. Cosmetic doc gaps unrelated
> to the behavior change are not grounds for `REFUTED`.
>
> End with exactly one token on its own line: `CONFIRMED` or `REFUTED`.

This implements the user's "check that the documentation, spec etc. are in
sync" requirement as a first-class verification criterion: a fix that resolves
the bug but leaves the docs describing the old behavior is `REFUTED` and is not
committed.

### 5.2 Retry & state

Mirrors `/review-issues`' `_verify_with_retry`: a transient error
(`_TransientVerifierError`, including a missing verdict token) retries once. A
`REFUTED` or `CONFIRMED` verdict is recorded in `verify_results[iid]`. Only
`confirmed is True` fixes proceed to Phase 5. Patch-apply failure in the verify
worktree is recorded as an error (not a refutation).

---

## 6. State & resumability

`FixRunState` mirrors `IssueReviewRunState`:

```python
@dataclass
class FixRunState:
    run_id: str
    project: str
    tag: str
    iid_from: int | None
    iid_to: int | None
    queued_issues: list[GitLabIssue]
    claims: dict[int, Claim]            # iid → structured claim
    fix_patches: dict[int, str]         # iid → captured diff
    fix_errors: dict[int, str]          # iid → why the fix failed
    verify_results: dict[int, dict]     # iid → {confirmed, summary, error?}
    commit_results: dict[int, dict]     # iid → {committed, sha, closed, labels, error}
    phase: str   # fetch → extract → fix → verify → commit_close → done
    metrics: dict[str, int]
    dry_run: bool
    patch_max_turns: int
    ...
```

`save()`/`load()`/`find_resumable()` are structurally identical to
`IssueReviewRunState`'s — atomic temp-file rename, JSON blob, project+tag+range
matching for resume. Resume re-enters at the first incomplete phase; completed
issues (present in `fix_patches`/`verify_results`/`commit_results`) are skipped.

---

## 7. Phase 5 — Commit & close

For each fix whose verification returned `CONFIRMED`:

1. **Apply the patch to the real repository** — `git apply --index` in
   `ctx.base_dir` (stages the change ready to commit). If the patch does not
   apply cleanly to the current working tree (e.g. it was generated against an
   earlier HEAD and the tree has since moved), the failure is recorded and the
   issue is skipped.
2. **Commit** — `git commit -m "Fix #<iid>: <summary>\n\nFixes #<iid>"`. The
   `Fixes #N` trailer mirrors the conventional closing reference and aids
   traceability. The new SHA is recorded.
3. **Label** — `adapter.ensure_labels_exist([fix-status::fixed])` then
   `adapter.apply_labels` with the issue's existing labels plus
   `fix-status::fixed` (preserving prior labels).
4. **Close** — `adapter.close_issue(project, iid)`, a new `GitLabAdapter` method
   that calls `update_issue` with `state_event=close`.

`--dry-run` skips steps 1–4 and records what *would* have been done.

Commits are issued **sequentially** (sorted by iid), because each commit
advances HEAD and later patches were generated against the original HEAD. This
tolerates apply failures gracefully: an issue whose patch no longer applies
after a prior commit is recorded as failed rather than aborting the run.

`GitLabAdapter.close_issue` is the one addition to `review_issues.py`:

```python
def close_issue(self, project: str, iid: int) -> None:
    """Close an issue via update_issue with state_event=close."""
    self._call("tool_update_issue",
               project_id=project, issue_iid=iid, state_event="close")
```

---

## 8. Config

`_load_fix_config` reads tool configuration with a layered fallback so a
project already configured for `/review-issues` works with `/fix` out of the
box:

1. `_DEFAULT_TOOL_CONFIG` (same keys as `/review-issues`).
2. `[review-issues]` table in `swival.toml`, if present.
3. `[fix]` table in `swival.toml`, if present (overrides per-command).

`[fix]` also accepts `patch_max_turns`. The GitLab server/tool keys are
identical to `[review-issues]` (`gitlab_server`, `tool_list_issues`,
`tool_get_issue_notes`, `tool_list_labels`, `tool_create_label`,
`tool_update_issue`).

---

## 9. Artifacts & README

`fix-findings/` holds:

- One `.patch` file per fixed issue (`<iid>-<slug>.patch`), the captured diff.
- `README.md` — the findings report, regenerated after each Phase-5 commit and
  once more at the end of the run.

The README reports, per issue: IID, title, fix status (fixed/failed), verified
(✓/✗/error), commit SHA, closed (✓/—), and a GitLab link, plus a summary tally
(fetched / fixed / verified / committed / closed). This satisfies the "update
the README in the findings accordingly" requirement.

---

## 10. The guarantee, summarized

Every issue that gets committed and closed has passed an **independent
verification gate**:

| Gate | Question | Mechanism | Default |
|---|---|---|---|
| Phase 4 — Verify | "Is the issue actually fixed, and are docs/spec in sync?" | Independent verifier in an isolated worktree with the patch applied; defect must no longer reproduce AND docs/spec must be consistent. | `REFUTED` unless `CONFIRMED` |

Plus structural safeguards:

- **Contained fix generation** — fixes are produced in isolated worktrees, never
  the real repo, until Phase 5.
- **Patch-based commit** — only the verified diff is applied and committed; the
  fixer cannot sneak in unrelated changes.
- **Sequential, failure-tolerant commits** — a patch that no longer applies
  after a prior commit is skipped, not force-applied.
- **Resumable, phase-gated state** — an issue is committed/closed only after
  `verify_results` records `CONFIRMED`; resume cannot skip the gate.
- **`--dry-run`** — the full pipeline runs and the report is produced, but no
  commits or GitLab writes occur.

This applies the same "confirm before acting" discipline as `/audit` and
`/review-issues` to a new question: *is this issue actually fixed — and is the
fix safe to commit and close?*
