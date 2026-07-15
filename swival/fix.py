"""Fix open GitLab issues from a project, verifying each fix before committing.

``/fix`` fetches open GitLab issues matching a project + tag, normalizes each
into a structured claim, generates a fix in an isolated worktree, verifies the
fix (the issue no longer reproduces *and* docs/spec stay in sync), then commits
the fix to the real repository, closes the issue in GitLab, applies a
``fix-status::fixed`` label, and updates the findings README.

It reuses ``/review-issues``' GitLab adapter and claim model, and ``/audit``'s
shared machinery (isolated worktrees, agent loops, batch runner, live UI).

See ``FIX_DESIGN.md`` for the full design.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .input_dispatch import InputContext

from . import fmt
from .audit import (
    _git,
    _make_isolated_loop_kwargs,
    _run_batch,
    _TransientVerifierError,
    _write_audit_trace,
)
from .audit_ui import AuditUI, PhaseHandle
from .review_issues import (
    Claim,
    GitLabAdapter,
    GitLabAdapterError,
    GitLabIssue,
    _DEFAULT_TOOL_CONFIG,
    _gather_claim_evidence,
    _git_rev_parse,
    _parse_validity_verdict_line,
    _phase_extract_claim,
    _worktree_at,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIX_PROVENANCE_URL = "https://swival.dev"

_DEFAULT_WORKERS = 4
_DEFAULT_PATCH_MAX_TURNS = 50
_DEFAULT_VERIFY_MAX_TURNS = 60

_FIX_LABEL = "fix-status::fixed"

_DEFAULT_METRICS: dict[str, int] = {
    "parse_failures_extract": 0,
    "repair_successes": 0,
    "repair_failures": 0,
    "fix_no_diff": 0,
    "fix_turn_budget_exhausted": 0,
    "fix_agent_error": 0,
    "verify_no_verdict": 0,
    "verify_transient_retries": 0,
    "verify_patch_apply_failed": 0,
    "commit_apply_failed": 0,
    "commit_failed": 0,
    "mcp_errors": 0,
    "truncated_calls": 0,
    "empty_response_retries": 0,
}

# ---------------------------------------------------------------------------
# Debug log (mirrors audit.py / review_issues.py)
# ---------------------------------------------------------------------------

_debug_log_path: Path | None = None
_debug_log_lock = threading.Lock()


def _debug_log(event: str, **fields) -> None:
    if _debug_log_path is None:
        return
    entry = {"ts": time.time(), "event": event, **fields}
    line = json.dumps(entry, default=str) + "\n"
    with _debug_log_lock:
        with open(_debug_log_path, "a") as f:
            f.write(line)


def _ui_info(ui: "AuditUI | None", msg: str) -> None:
    if ui is not None:
        ui.scrollback(msg)
    else:
        fmt.info(msg)


def _ui_warning(ui: "AuditUI | None", msg: str) -> None:
    if ui is not None:
        ui.warning(msg)
    else:
        fmt.warning(msg)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_fix_config(base_dir: str) -> tuple[dict, int | None]:
    """Read ``[fix]`` (falling back to ``[review-issues]``) from swival.toml.

    Returns ``(tool_config, patch_max_turns)``. Tool keys default to
    ``_DEFAULT_TOOL_CONFIG``; an existing ``[review-issues]`` table is reused so
    a project already configured for ``/review-issues`` works with ``/fix`` out
    of the box, and a ``[fix]`` table overrides per-command.
    """
    config = dict(_DEFAULT_TOOL_CONFIG)
    patch_max_turns: int | None = None
    try:
        from .config import load_toml_config

        toml = load_toml_config(base_dir)
        if toml:
            # Inherit [review-issues] tool mapping first.
            ri = toml.get("review-issues")
            if isinstance(ri, dict):
                for k, v in ri.items():
                    config[k] = v
            # Then [fix] overrides (and adds patch_max_turns).
            fx = toml.get("fix")
            if isinstance(fx, dict):
                for k, v in fx.items():
                    config[k] = v
                pmt = fx.get("patch_max_turns")
                if isinstance(pmt, int):
                    patch_max_turns = pmt
    except Exception:
        pass
    return config, patch_max_turns


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_FIX_SYSTEM = """\
You are fixing one GitLab issue in an isolated git worktree. The issue has been \
normalized into a structured claim. Use edit_file to make the minimal correct \
fix.

Rules:
- Make the smallest change that correctly resolves the described defect.
- Update any documentation, spec, README, docstrings, or comments that must \
stay consistent with the behavior you changed. If the fix alters documented \
behavior, the docs/spec must reflect it.
- Do not make unrelated changes, refactors, or style edits.
- You may run tests or small checks to validate the fix, but do not modify the \
test suite beyond what the fix requires.
- You have no access to GitLab; work only in the worktree.

When done, stop. The diff you produce will be captured automatically."""

_FIX_VERIFY_SYSTEM = """\
You are verifying that one GitLab issue has actually been fixed in the code \
before you. The issue's original claim is provided, and the fix patch has \
already been applied to this worktree. Treat the fix as a hypothesis and \
confirm it independently.

Rules:
- You may inspect the code, compile, or run small proof-of-concept checks in \
the worktree.
- Confirm (CONFIRMED) only if BOTH hold:
  1. The issue's described defect no longer reproduces in the fixed code — the \
claim is no longer true.
  2. Documentation, spec, README, and comments that describe the changed \
behavior are consistent with the fix. If the fix changed documented behavior \
and the docs were not updated, that is out-of-sync → REFUTED.
- Reject as REFUTED when the defect still reproduces, the fix is incomplete, \
or the fix introduced an inconsistency with docs/spec.
- Do not demand perfection: cosmetic doc gaps unrelated to the behavior change \
are not grounds for REFUTED.

End your final response with exactly one token on its own line:
CONFIRMED
REFUTED"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FixRunState:
    run_id: str
    project: str
    tag: str
    iid_from: int | None
    iid_to: int | None
    queued_issues: list[GitLabIssue] = field(default_factory=list)
    claims: dict[int, Claim] = field(default_factory=dict)
    fix_patches: dict[int, str] = field(default_factory=dict)
    fix_errors: dict[int, str] = field(default_factory=dict)
    verify_results: dict[int, dict] = field(default_factory=dict)
    commit_results: dict[int, dict] = field(default_factory=dict)
    phase: str = "fetch"
    metrics: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_METRICS))
    dry_run: bool = False
    patch_max_turns: int = _DEFAULT_PATCH_MAX_TURNS
    artifact_dir: Path = field(default_factory=lambda: Path("fix-findings"))
    state_dir: Path = field(default_factory=lambda: Path(".swival/fix"))

    def save(self) -> None:
        d = self.state_dir / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        blob = {
            "run_id": self.run_id,
            "project": self.project,
            "tag": self.tag,
            "iid_from": self.iid_from,
            "iid_to": self.iid_to,
            "queued_issues": [asdict(i) for i in self.queued_issues],
            "claims": {str(k): asdict(v) for k, v in self.claims.items()},
            "fix_patches": {str(k): v for k, v in self.fix_patches.items()},
            "fix_errors": {str(k): v for k, v in self.fix_errors.items()},
            "verify_results": {str(k): v for k, v in self.verify_results.items()},
            "commit_results": {str(k): v for k, v in self.commit_results.items()},
            "phase": self.phase,
            "metrics": dict(self.metrics),
            "dry_run": self.dry_run,
            "patch_max_turns": self.patch_max_turns,
        }
        state_path = d / "state.json"
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=2))
        tmp.replace(state_path)

    @classmethod
    def load(cls, state_dir: Path, run_id: str) -> "FixRunState":
        d = state_dir / run_id / "state.json"
        blob = json.loads(d.read_text())
        queued = [GitLabIssue(**i) for i in blob.get("queued_issues", [])]
        claims = {
            int(k): Claim(**v) for k, v in blob.get("claims", {}).items()
        }
        return cls(
            run_id=blob["run_id"],
            project=blob["project"],
            tag=blob["tag"],
            iid_from=blob.get("iid_from"),
            iid_to=blob.get("iid_to"),
            queued_issues=queued,
            claims=claims,
            fix_patches={int(k): v for k, v in blob.get("fix_patches", {}).items()},
            fix_errors={int(k): v for k, v in blob.get("fix_errors", {}).items()},
            verify_results={int(k): v for k, v in blob.get("verify_results", {}).items()},
            commit_results={int(k): v for k, v in blob.get("commit_results", {}).items()},
            phase=blob.get("phase", "fetch"),
            metrics={**_DEFAULT_METRICS, **blob.get("metrics", {})},
            dry_run=blob.get("dry_run", False),
            patch_max_turns=blob.get("patch_max_turns", _DEFAULT_PATCH_MAX_TURNS),
            state_dir=state_dir,
        )

    @classmethod
    def find_resumable(
        cls, state_dir: Path, project: str, tag: str,
        iid_from: int | None, iid_to: int | None,
    ) -> "FixRunState | None":
        if not state_dir.exists():
            return None
        best = None
        best_mtime = 0.0
        for entry in state_dir.iterdir():
            sf = entry / "state.json"
            if not sf.exists():
                continue
            try:
                blob = json.loads(sf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if blob.get("project") != project or blob.get("tag") != tag:
                continue
            if blob.get("iid_from") != iid_from or blob.get("iid_to") != iid_to:
                continue
            if blob.get("phase") == "done":
                continue
            mtime = sf.stat().st_mtime
            if mtime > best_mtime:
                try:
                    best = cls.load(state_dir, blob["run_id"])
                except Exception:
                    continue
                best_mtime = mtime
        return best


# ---------------------------------------------------------------------------
# Phase 1: Fetch
# ---------------------------------------------------------------------------


def _fetch_issues(
    adapter: GitLabAdapter, state: FixRunState, ui: AuditUI,
) -> str | None:
    """Fetch open issues matching the project/tag/range. Returns error or None."""
    ph = _phase_open(ui, "fetch", label=state.project)
    if not ui.is_live:
        fmt.info(
            f"phase 1: fetching open issues from {state.project} "
            f"(tag={state.tag})..."
        )
    try:
        state.queued_issues = adapter.list_issues(
            state.project, state.tag, "opened",
            state.iid_from, state.iid_to,
        )
    except GitLabAdapterError as e:
        return f"error: failed to fetch issues: {e}"
    ph.complete(f"{len(state.queued_issues)} issue(s)")
    if not ui.is_live:
        fmt.info(f"phase 1 complete. {len(state.queued_issues)} issue(s) fetched.")
    if not state.queued_issues:
        state.phase = "done"
        state.save()
        return "no open issues matched the filter."
    state.phase = "extract"
    state.save()
    return None


# ---------------------------------------------------------------------------
# Phase 2: Extract claims
# ---------------------------------------------------------------------------


def _extract_one(
    issue: GitLabIssue, state: FixRunState, ctx: "InputContext",
    adapter: GitLabAdapter,
) -> Claim:
    return _phase_extract_claim(issue, state, ctx, adapter)


# ---------------------------------------------------------------------------
# Phase 3: Fix
# ---------------------------------------------------------------------------


def _fix_one(
    issue: GitLabIssue, claim: Claim, state: FixRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> tuple[str | None, str | None]:
    """Generate a fix patch in an isolated worktree at HEAD.

    Returns ``(patch_text, error)`` — exactly one is non-None.
    """
    from .agent import run_agent_loop

    base_dir = ctx.base_dir
    head = _git_rev_parse("HEAD", base_dir)
    work_dir = (
        Path(base_dir) / state.state_dir / state.run_id
        / "fix" / f"issue-{issue.iid}" / "work"
    )

    evidence, n_files = _gather_claim_evidence(claim, head, base_dir)
    _ui_info(ui, f"  [!{issue.iid}] fixing ({n_files} evidence file(s))")

    claim_json = json.dumps(asdict(claim), indent=2)
    user = (
        f"Issue !{issue.iid}: {issue.title}\n\n"
        f"Issue description:\n{issue.description or '(empty)'}\n\n"
        f"Structured claim:\n{claim_json}\n\n"
        f"Committed evidence bundle (at {head[:8]}):\n{evidence}"
    )
    messages = [
        {"role": "system", "content": _FIX_SYSTEM},
        {"role": "user", "content": user},
    ]

    try:
        with _worktree_at(base_dir, work_dir, head):
            kw = _make_isolated_loop_kwargs(
                ctx, work_dir, max_turns=state.patch_max_turns,
            )
            try:
                _answer, exhausted = run_agent_loop(messages, ctx.tools, **kw)
            except Exception as e:
                state.metrics["fix_agent_error"] += 1
                return None, f"agent loop failed: {e}"
            finally:
                _write_audit_trace(
                    ctx, messages, task=f"fix: patch !{issue.iid}",
                )

            if exhausted:
                state.metrics["fix_turn_budget_exhausted"] += 1
                return None, "turn budget exhausted before a fix was produced"

            diff = subprocess.run(
                ["git", "diff"], capture_output=True, cwd=str(work_dir), timeout=10,
            )
            patch_text = diff.stdout.decode(errors="replace").strip()
            if not patch_text:
                state.metrics["fix_no_diff"] += 1
                return None, "no changes produced"
            return patch_text + "\n", None
    except RuntimeError as e:
        return None, f"worktree failed: {e}"


# ---------------------------------------------------------------------------
# Phase 4: Verify
# ---------------------------------------------------------------------------


def _apply_patch_to_worktree(patch_text: str, work_dir: Path) -> str | None:
    """Apply a patch into a worktree. Returns error string or None on success."""
    patch_file = work_dir.parent / "fix.patch"
    patch_file.write_text(patch_text)
    check = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        capture_output=True, cwd=str(work_dir), timeout=15,
    )
    if check.returncode != 0:
        return (
            f"patch does not apply cleanly: "
            f"{check.stderr.decode(errors='replace').strip()}"
        )
    apply = subprocess.run(
        ["git", "apply", str(patch_file)],
        capture_output=True, cwd=str(work_dir), timeout=15,
    )
    if apply.returncode != 0:
        return (
            f"git apply failed: "
            f"{apply.stderr.decode(errors='replace').strip()}"
        )
    return None


def _verify_fix_one(
    issue: GitLabIssue, claim: Claim, patch_text: str,
    state: FixRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> tuple[bool | None, str]:
    """Verify the fix in an isolated worktree with the patch applied.

    Returns ``(confirmed, summary)``. ``confirmed`` is True/False, or None on
    infrastructure error (caller records the error in ``summary``).
    """
    from .agent import run_agent_loop

    base_dir = ctx.base_dir
    head = _git_rev_parse("HEAD", base_dir)
    work_dir = (
        Path(base_dir) / state.state_dir / state.run_id
        / "verify" / f"issue-{issue.iid}"
    )

    _ui_info(ui, f"  [!{issue.iid}] verifying fix")
    claim_json = json.dumps(asdict(claim), indent=2)

    try:
        with _worktree_at(base_dir, work_dir, head):
            apply_err = _apply_patch_to_worktree(patch_text, work_dir)
            if apply_err is not None:
                state.metrics["verify_patch_apply_failed"] += 1
                return None, f"patch apply failed: {apply_err}"

            user = (
                f"Issue !{issue.iid}: {issue.title}\n\n"
                f"Original claim (the defect that should now be fixed):\n"
                f"{claim_json}\n\n"
                f"Fix patch applied to this worktree:\n```diff\n{patch_text}```\n\n"
                f"Inspect the code in the worktree to confirm the defect no "
                f"longer reproduces and that docs/spec are in sync."
            )
            messages = [
                {"role": "system", "content": _FIX_VERIFY_SYSTEM},
                {"role": "user", "content": user},
            ]
            kw = _make_isolated_loop_kwargs(
                ctx, work_dir, max_turns=_DEFAULT_VERIFY_MAX_TURNS,
            )
            try:
                answer, exhausted = run_agent_loop(messages, ctx.tools, **kw)
            except (ConnectionError, TimeoutError, OSError) as e:
                raise _TransientVerifierError(str(e)) from e
            finally:
                _write_audit_trace(
                    ctx, messages, task=f"fix: verify !{issue.iid}",
                )

            answer = answer or ""
            verdict = _parse_validity_verdict_line(answer)
            if verdict is None:
                state.metrics["verify_no_verdict"] += 1
                raise _TransientVerifierError(
                    "verifier produced no verdict token"
                    + (" (turn budget exhausted)" if exhausted else "")
                )
            if verdict:
                _ui_info(ui, f"  [!{issue.iid}] CONFIRMED — fixed & in sync")
            else:
                _ui_info(ui, f"  [!{issue.iid}] REFUTED — not fixed or out of sync")
            return verdict, answer[-1000:]
    except _TransientVerifierError:
        raise
    except RuntimeError as e:
        return None, f"worktree failed: {e}"


def _verify_with_retry(
    issue: GitLabIssue, claim: Claim, patch_text: str,
    state: FixRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> tuple[bool | None, str]:
    """Verify with one transient-error retry (mirrors _verify_with_retry)."""
    try:
        return _verify_fix_one(issue, claim, patch_text, state, ctx, ui=ui)
    except _TransientVerifierError as e:
        state.metrics["verify_transient_retries"] += 1
        _ui_info(ui, f"  [!{issue.iid}] retrying after transient error: {e}")
        try:
            return _verify_fix_one(issue, claim, patch_text, state, ctx, ui=ui)
        except _TransientVerifierError as e2:
            return None, f"transient error after retry: {e2}"


# ---------------------------------------------------------------------------
# Phase 5: Commit & close
# ---------------------------------------------------------------------------


def _commit_message(issue: GitLabIssue, claim: Claim) -> str:
    summary = claim.summary or issue.title
    summary = summary.strip().splitlines()[0][:120]
    return f"Fix #{issue.iid}: {summary}\n\nFixes #{issue.iid}"


def _apply_patch_to_repo(patch_text: str, base_dir: str) -> str | None:
    """Apply a patch to the real working tree + index. Returns error or None."""
    patch_file = Path(base_dir) / ".swival" / "fix" / "_staged.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch_text)
    check = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        capture_output=True, cwd=base_dir, timeout=20,
    )
    if check.returncode != 0:
        return (
            f"patch does not apply to the working tree: "
            f"{check.stderr.decode(errors='replace').strip()}"
        )
    # --index stages the change so it is ready to commit.
    apply = subprocess.run(
        ["git", "apply", "--index", str(patch_file)],
        capture_output=True, cwd=base_dir, timeout=20,
    )
    if apply.returncode != 0:
        return (
            f"git apply --index failed: "
            f"{apply.stderr.decode(errors='replace').strip()}"
        )
    return None


def _git_commit(message: str, base_dir: str) -> tuple[str | None, str | None]:
    """Commit staged changes. Returns ``(sha, error)``."""
    result = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True, cwd=base_dir, timeout=30,
    )
    if result.returncode != 0:
        return None, result.stderr.decode(errors="replace").strip() or "commit failed"
    return _git_rev_parse("HEAD", base_dir), None


def _commit_and_close_one(
    issue: GitLabIssue, claim: Claim, patch_text: str,
    state: FixRunState, ctx: "InputContext", adapter: GitLabAdapter,
    ui: "AuditUI | None" = None,
) -> dict:
    """Commit the fix to the real repo, close the GitLab issue, label it."""
    base_dir = ctx.base_dir
    result: dict = {
        "committed": False, "sha": None,
        "closed": False, "labels": [],
        "error": None,
    }

    if state.dry_run:
        _ui_info(
            ui,
            f"  [!{issue.iid}] (dry-run) would commit fix, close issue, "
            f"and apply {_FIX_LABEL}",
        )
        result["error"] = "dry-run: no commit/close performed"
        return result

    # 1. Apply patch to the real working tree + index.
    apply_err = _apply_patch_to_repo(patch_text, base_dir)
    if apply_err is not None:
        state.metrics["commit_apply_failed"] += 1
        result["error"] = apply_err
        return result

    # 2. Commit.
    message = _commit_message(issue, claim)
    sha, commit_err = _git_commit(message, base_dir)
    if commit_err is not None:
        state.metrics["commit_failed"] += 1
        # Unstage so the working tree is not left half-applied.
        subprocess.run(
            ["git", "reset", "HEAD"], capture_output=True, cwd=base_dir, timeout=15,
        )
        result["error"] = f"commit failed: {commit_err}"
        return result
    result["committed"] = True
    result["sha"] = sha
    _ui_info(ui, f"  [!{issue.iid}] committed {sha[:8]}")

    # 3. Apply fix-status::fixed label (preserve existing labels).
    new_labels = list(dict.fromkeys(issue.labels + [_FIX_LABEL]))
    try:
        adapter.ensure_labels_exist(state.project, [_FIX_LABEL])
        adapter.apply_labels(state.project, issue.iid, new_labels)
        result["labels"] = new_labels
    except GitLabAdapterError as e:
        state.metrics["mcp_errors"] += 1
        _ui_warning(ui, f"  [!{issue.iid}] label write-back failed: {e}")

    # 4. Close the issue.
    try:
        adapter.close_issue(state.project, issue.iid)
        result["closed"] = True
        _ui_info(ui, f"  [!{issue.iid}] closed in GitLab")
    except GitLabAdapterError as e:
        state.metrics["mcp_errors"] += 1
        _ui_warning(ui, f"  [!{issue.iid}] close failed: {e}")
        if not result["error"]:
            result["error"] = f"close failed: {e}"

    return result


# ---------------------------------------------------------------------------
# README rendering
# ---------------------------------------------------------------------------


def _make_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] if slug else "issue"


def _patch_filename(iid: int, title: str) -> str:
    return f"{iid:03d}-{_make_slug(title)}.patch"


def _write_patch_artifacts(state: FixRunState, base_dir: str) -> None:
    """Persist each issue's patch as an artifact alongside the README."""
    artifact_dir = Path(base_dir) / state.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    issue_map = {i.iid: i for i in state.queued_issues}
    for iid, patch_text in state.fix_patches.items():
        issue = issue_map.get(iid)
        fn = _patch_filename(iid, issue.title if issue else f"issue-{iid}")
        (artifact_dir / fn).write_text(patch_text)


def _render_fix_readme(state: FixRunState) -> str:
    lines: list[str] = []
    lines.append("# Fix Run — Report\n")
    lines.append(f"- **Project:** {state.project}")
    lines.append(f"- **Tag:** {state.tag}")
    range_str = str(state.iid_from) if state.iid_from else "all"
    if state.iid_to:
        range_str = f"{state.iid_from}–{state.iid_to}"
    lines.append(f"- **IID range:** {range_str}")
    lines.append(f"- **Run ID:** {state.run_id}")
    lines.append(f"- **Dry run:** {state.dry_run}\n")

    n_queued = len(state.queued_issues)
    n_fixed = len(state.fix_patches)
    n_verified = sum(
        1 for r in state.verify_results.values() if r.get("confirmed")
    )
    n_committed = sum(1 for r in state.commit_results.values() if r.get("committed"))
    n_closed = sum(1 for r in state.commit_results.values() if r.get("closed"))
    lines.append("## Summary\n")
    lines.append(f"- Open issues fetched: {n_queued}")
    lines.append(f"- Fixes generated: {n_fixed}")
    lines.append(f"- Fixes verified: {n_verified}")
    lines.append(f"- Fixes committed: {n_committed}")
    lines.append(f"- Issues closed: {n_closed}\n")

    lines.append("## Issues\n")
    lines.append(
        "| IID | Title | Fix | Verified | Commit | Closed | Link |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    issue_map = {i.iid: i for i in state.queued_issues}
    for iid in sorted(
        set(state.claims) | set(state.fix_patches) | set(state.commit_results)
    ):
        issue = issue_map.get(iid)
        title = issue.title[:50] if issue else "?"
        link = f"[→]({issue.web_url})" if issue and issue.web_url else "—"

        fix_err = state.fix_errors.get(iid)
        if iid in state.fix_patches:
            fix_cell = "fixed"
        elif fix_err:
            fix_cell = f"failed: {fix_err[:40]}"
        else:
            fix_cell = "—"

        vr = state.verify_results.get(iid, {})
        if vr.get("error"):
            vcell = f"error: {vr['error'][:30]}"
        elif vr.get("confirmed") is True:
            vcell = "✓"
        elif vr.get("confirmed") is False:
            vcell = "✗"
        else:
            vcell = "—"

        cr = state.commit_results.get(iid, {})
        commit_cell = cr.get("sha", "")[:8] or "—"
        if cr.get("error") and not cr.get("committed"):
            commit_cell = f"failed: {cr['error'][:30]}"
        closed_cell = "✓" if cr.get("closed") else "—"

        lines.append(
            f"| !{iid} | {title} | {fix_cell} | {vcell} | "
            f"`{commit_cell}` | {closed_cell} | {link} |"
        )
    lines.append("")
    return "\n".join(lines)


def _write_fix_readme(state: FixRunState, base_dir: str) -> None:
    artifact_dir = Path(base_dir) / state.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _write_patch_artifacts(state, base_dir)
    (artifact_dir / "README.md").write_text(_render_fix_readme(state))


# ---------------------------------------------------------------------------
# Phase titles & pipeline driver
# ---------------------------------------------------------------------------

_PHASE_TITLES: dict[str, tuple[str, str]] = {
    "fetch": ("Phase 1 · Fetch", "fetch"),
    "extract": ("Phase 2 · Extract", "extract"),
    "fix": ("Phase 3 · Fix", "fix"),
    "verify": ("Phase 4 · Verify", "grounding"),
    "commit_close": ("Phase 5 · Commit & Close", "writeback"),
}


def _phase_open(
    ui: AuditUI, phase_key: str, *, total: int | None = None, label: str | None = None,
) -> PhaseHandle:
    title, color_key = _PHASE_TITLES[phase_key]
    if label is not None:
        title = f"{title} · {label}"
    elif total is not None:
        title = f"{title} · {total} item{'s' if total != 1 else ''}"
    return ui.phase(title, total=total, color=fmt.phase_color(color_key))


def _run_pipeline_body(
    state: FixRunState, ui: AuditUI, ctx: "InputContext",
    adapter: GitLabAdapter, base_dir: str, workers: int, *,
    resume: bool,
) -> str:
    """Drive the full pipeline, phase-gated for resume."""

    # ---- Phase 1: Fetch ----
    if state.phase == "fetch":
        err = _fetch_issues(adapter, state, ui)
        if err is not None:
            return err

    # ---- Phase 2: Extract claims ----
    if state.phase == "extract":
        to_extract = [
            i for i in state.queued_issues if i.iid not in state.claims
        ]
        ph2 = _phase_open(ui, "extract", total=len(state.queued_issues))
        if not ui.is_live:
            fmt.info(f"phase 2: extracting {len(to_extract)} claim(s)...")

        def _fn_ext(issue):
            return _extract_one(issue, state, ctx, adapter)

        def _on_ext(idx, item, result):
            if result is not None:
                state.claims[result.iid] = result
                ph2.advance()
                state.save()

        _run_batch(
            _fn_ext, to_extract, max_workers=max(1, workers),
            ui=ui, label_for=lambda i: f"!{i.iid}", on_result=_on_ext,
        )
        for i in state.queued_issues:
            if i.iid in state.claims:
                ph2.advance()
        ph2.complete(f"{len(state.claims)} claim(s)")
        if not ui.is_live:
            fmt.info(f"phase 2 complete. {len(state.claims)} claim(s) extracted.")
        state.phase = "fix"
        state.save()

    # ---- Phase 3: Fix ----
    if state.phase == "fix":
        to_fix = [
            i for i in state.queued_issues
            if i.iid in state.claims
            and i.iid not in state.fix_patches
            and i.iid not in state.fix_errors
        ]
        ph3 = _phase_open(ui, "fix", total=len(state.claims))
        if not ui.is_live:
            fmt.info(f"phase 3: fixing {len(to_fix)} issue(s)...")

        def _fn_fix(issue):
            claim = state.claims.get(issue.iid)
            if claim is None:
                return None
            try:
                patch, err = _fix_one(issue, claim, state, ctx, ui=ui)
            except Exception as e:
                return (issue.iid, None, f"fix failed: {e}")
            return (issue.iid, patch, err)

        def _on_fix(idx, item, result):
            if result is None:
                return
            iid, patch, err = result
            if patch is not None:
                state.fix_patches[iid] = patch
            elif err is not None:
                state.fix_errors[iid] = err
                _ui_warning(ui, f"  [!{iid}] fix failed: {err}")
            ph3.advance()
            state.save()

        _run_batch(
            _fn_fix, to_fix, max_workers=max(1, workers),
            ui=ui, label_for=lambda i: f"!{i.iid}", on_result=_on_fix,
        )
        for i in state.queued_issues:
            if i.iid in state.fix_patches or i.iid in state.fix_errors:
                ph3.advance()
        ph3.complete(
            f"{len(state.fix_patches)} fixed · {len(state.fix_errors)} failed"
        )
        if not ui.is_live:
            fmt.info(
                f"phase 3 complete. {len(state.fix_patches)} patch(es) generated, "
                f"{len(state.fix_errors)} failed."
            )
        state.phase = "verify"
        state.save()

    # ---- Phase 4: Verify ----
    if state.phase == "verify":
        to_verify = [
            (i, state.fix_patches[i.iid])
            for i in state.queued_issues
            if i.iid in state.fix_patches and i.iid not in state.verify_results
        ]
        ph4 = _phase_open(ui, "verify", total=len(state.fix_patches))
        if not ui.is_live:
            fmt.info(f"phase 4: verifying {len(to_verify)} fix(es)...")

        def _fn_verify(item):
            issue, patch_text = item
            claim = state.claims.get(issue.iid)
            if claim is None:
                return (issue.iid, None, "no claim")
            try:
                confirmed, summary = _verify_with_retry(
                    issue, claim, patch_text, state, ctx, ui=ui,
                )
            except Exception as e:
                return (issue.iid, None, f"verify failed: {e}")
            return (issue.iid, confirmed, summary)

        def _on_verify(idx, item, result):
            if result is None:
                return
            iid, confirmed, summary = result
            if confirmed is None:
                state.verify_results[iid] = {"confirmed": None, "error": summary}
            else:
                state.verify_results[iid] = {
                    "confirmed": confirmed, "summary": summary,
                }
            ph4.advance()
            state.save()

        _run_batch(
            _fn_verify, to_verify, max_workers=max(1, workers),
            ui=ui, label_for=lambda t: f"!{t[0].iid}", on_result=_on_verify,
        )
        for iid in state.fix_patches:
            if iid in state.verify_results:
                ph4.advance()
        n_ok = sum(1 for r in state.verify_results.values() if r.get("confirmed") is True)
        n_bad = sum(1 for r in state.verify_results.values() if r.get("confirmed") is False)
        ph4.complete(f"{n_ok} verified · {n_bad} refuted")
        if not ui.is_live:
            fmt.info(f"phase 4 complete. {n_ok} verified, {n_bad} refuted.")
        state.phase = "commit_close"
        state.save()

    # ---- Phase 5: Commit & close ----
    if state.phase == "commit_close":
        # Only commit issues whose fix was verified.
        to_commit = [
            i for i in state.queued_issues
            if i.iid in state.fix_patches
            and state.verify_results.get(i.iid, {}).get("confirmed") is True
            and i.iid not in state.commit_results
        ]
        ph5 = _phase_open(ui, "commit_close", total=len(to_commit))
        if not ui.is_live:
            fmt.info(f"phase 5: committing/closing {len(to_commit)} fix(es)...")

        # Commit sequentially: each commit advances HEAD, and later patches
        # were generated against the original HEAD, so order them by iid and
        # tolerate apply failures.
        for issue in sorted(to_commit, key=lambda i: i.iid):
            patch_text = state.fix_patches[issue.iid]
            claim = state.claims.get(issue.iid)
            if claim is None:
                continue
            try:
                cr = _commit_and_close_one(
                    issue, claim, patch_text, state, ctx, adapter, ui=ui,
                )
            except Exception as e:
                cr = {
                    "committed": False, "sha": None, "closed": False,
                    "labels": [], "error": f"commit/close failed: {e}",
                }
                state.metrics["commit_failed"] += 1
            state.commit_results[issue.iid] = cr
            ph5.advance()
            state.save()
            _write_fix_readme(state, base_dir)

        n_committed = sum(1 for r in state.commit_results.values() if r.get("committed"))
        n_closed = sum(1 for r in state.commit_results.values() if r.get("closed"))
        ph5.complete(f"{n_committed} committed · {n_closed} closed")
        if not ui.is_live:
            fmt.info(
                f"phase 5 complete. {n_committed} committed, {n_closed} closed."
            )
        state.phase = "done"
        state.save()

    # Final README write (covers skipped/dry-run/incomplete cases too).
    _write_fix_readme(state, base_dir)

    # ---- Summary ----
    n_queued = len(state.queued_issues)
    n_fixed = len(state.fix_patches)
    n_verified = sum(1 for r in state.verify_results.values() if r.get("confirmed") is True)
    n_refuted = sum(1 for r in state.verify_results.values() if r.get("confirmed") is False)
    n_committed = sum(1 for r in state.commit_results.values() if r.get("committed"))
    n_closed = sum(1 for r in state.commit_results.values() if r.get("closed"))
    summary_lines = [
        "Fix run complete.",
        f"  Project: {state.project} · Tag: {state.tag}",
        f"  Open issues fetched: {n_queued}",
        f"  Fixes generated: {n_fixed}",
        f"  Fixes verified: {n_verified}",
        f"  Fixes refuted: {n_refuted}",
        f"  Fixes committed: {n_committed}",
        f"  Issues closed: {n_closed}",
        f"  Report: {state.artifact_dir / 'README.md'}",
    ]
    if state.dry_run:
        summary_lines.append("  (dry-run — no commits or GitLab writes)")
    return "\n".join(summary_lines)


# ---------------------------------------------------------------------------
# Command entrypoint
# ---------------------------------------------------------------------------


def run_fix_command(cmd_arg: str, ctx: "InputContext") -> str:
    """Entry point for the /fix command. Returns summary text."""
    global _debug_log_path

    base_dir = ctx.base_dir
    workers = _DEFAULT_WORKERS
    arg = cmd_arg.strip()
    resume = False
    debug = False
    dry_run = False
    project: str | None = None
    tag: str | None = None
    iid_from: int | None = None
    iid_to: int | None = None
    patch_max_turns_cli: int | None = None

    parts = arg.split()
    filtered = []
    i = 0
    while i < len(parts):
        if parts[i] == "--resume":
            resume = True
        elif parts[i] == "--debug":
            debug = True
        elif parts[i] == "--dry-run":
            dry_run = True
        elif parts[i] == "--tag":
            if i + 1 >= len(parts):
                return "error: --tag requires a value"
            i += 1
            tag = parts[i]
        elif parts[i] == "--from":
            if i + 1 >= len(parts):
                return "error: --from requires an integer"
            i += 1
            try:
                iid_from = int(parts[i])
            except ValueError:
                return f"error: --from requires an integer, got {parts[i]!r}"
        elif parts[i] == "--to":
            if i + 1 >= len(parts):
                return "error: --to requires an integer"
            i += 1
            try:
                iid_to = int(parts[i])
            except ValueError:
                return f"error: --to requires an integer, got {parts[i]!r}"
        elif parts[i] == "--workers":
            if i + 1 >= len(parts):
                return "error: --workers requires an integer"
            i += 1
            try:
                workers = int(parts[i])
            except ValueError:
                return f"error: --workers requires an integer, got {parts[i]!r}"
        elif parts[i] == "--patch-max-turns":
            if i + 1 >= len(parts):
                return "error: --patch-max-turns requires an integer"
            i += 1
            try:
                patch_max_turns_cli = int(parts[i])
            except ValueError:
                return f"error: --patch-max-turns requires an integer, got {parts[i]!r}"
            if patch_max_turns_cli < 1:
                return "error: --patch-max-turns must be at least 1"
        elif parts[i].startswith("-"):
            return (
                f"error: unknown option {parts[i]!r}. "
                f"Known flags: --tag, --from, --to, --workers, "
                f"--patch-max-turns, --resume, --dry-run, --debug."
            )
        else:
            filtered.append(parts[i])
        i += 1

    if not filtered:
        return "error: a project path or ID is required (e.g. /fix group/sub --tag bug)"
    project = filtered[0]

    if not tag:
        return "error: --tag is required"

    if ctx.mcp_manager is None:
        return (
            "error: no MCP manager available. Configure a GitLab MCP server in "
            "swival.toml [mcp_servers] or .swival/mcp.json."
        )

    config, config_patch_max_turns = _load_fix_config(base_dir)
    patch_max_turns = (
        patch_max_turns_cli
        if patch_max_turns_cli is not None
        else config_patch_max_turns or _DEFAULT_PATCH_MAX_TURNS
    )

    if debug:
        log_dir = Path(base_dir) / ".swival" / "fix"
        log_dir.mkdir(parents=True, exist_ok=True)
        _debug_log_path = log_dir / "debug.jsonl"
        _debug_log("fix_start", args=arg)
        fmt.info(f"debug log: {_debug_log_path}")
    else:
        _debug_log_path = None

    state_dir = Path(base_dir) / ".swival" / "fix"

    adapter = GitLabAdapter(ctx.mcp_manager, config)

    # Resume or create new state.
    if resume:
        state = FixRunState.find_resumable(
            state_dir, project, tag, iid_from, iid_to,
        )
        if state is None:
            return "error: no resumable fix run found for this project/tag/range."
        state.dry_run = dry_run
        state.patch_max_turns = patch_max_turns
    else:
        state = FixRunState(
            run_id=uuid.uuid4().hex[:12],
            project=project,
            tag=tag,
            iid_from=iid_from,
            iid_to=iid_to,
            dry_run=dry_run,
            patch_max_turns=patch_max_turns,
            state_dir=state_dir,
        )

    # Build the UI.
    commit = ""
    try:
        commit = _git(["rev-parse", "HEAD"], base_dir)
    except RuntimeError:
        pass
    branch = ""
    try:
        branch = _git(["branch", "--show-current"], base_dir) or "HEAD"
    except RuntimeError:
        pass

    n_issues = len(state.queued_issues) if resume else 0
    ui = AuditUI(
        run_id=state.run_id,
        branch=branch,
        commit=commit,
        workers=workers,
        total_files=n_issues,
    )

    try:
        with ui:
            result = _run_pipeline_body(
                state, ui, ctx, adapter, base_dir, workers, resume=resume,
            )
            if state.phase == "done":
                ui.summary(
                    artifact_dir=str(state.artifact_dir),
                    written=len(state.commit_results),
                    readme_written=True,
                )
            else:
                ui.incomplete(result)
            return result
    finally:
        _debug_log_path = None
