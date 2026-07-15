"""Staged validity review of GitLab issues against committed code.

Mirrors ``/audit``'s two-gate confirmation model, but the *issues* are the
proposed findings. Each issue is classified, normalized into a structured
claim, verified against committed code (HEAD for open issues; the parent of
the closing commit AND the closing commit for closed issues), adjudicated by
an adversarial panel, and the verdict is written back to GitLab as labels.

See ``REVIEW_ISSUES_DESIGN.md`` for the full design.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
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
    _call_audit_llm,
    _git,
    _git_show,
    _make_isolated_loop_kwargs,
    _parse_records_with_repair,
    _run_batch,
    _TransientVerifierError,
    _write_audit_trace,
    PhaseSchema,
    RecordSchema,
)
from .audit_ui import AuditUI, PhaseHandle

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REVIEW_ISSUES_PROVENANCE_URL = "https://swival.dev"

_DEFAULT_WORKERS = 4
_DEFAULT_VERIFY_MAX_TURNS = 60

# Verdict constants.
V_REAL_OPEN = "real::open"
V_REAL_FIXED = "real::fixed"
V_REAL_UNFIXED = "real::unfixed"
V_FALSE_POSITIVE = "false-positive::never-real"
V_NOT_APPLICABLE = "not-applicable"
V_DEFERRED = "deferred"

_ALL_VERDICTS = (
    V_REAL_OPEN, V_REAL_FIXED, V_REAL_UNFIXED, V_FALSE_POSITIVE, V_NOT_APPLICABLE,
)

# Conservative-recalibration order: a verdict may move rightward (less
# accusatory), never leftward.  See design §9.3.
_CONSERVATIVE_ORDER = [
    V_REAL_UNFIXED,    # most accusatory
    V_REAL_FIXED,
    V_REAL_OPEN,
    V_FALSE_POSITIVE,
    V_NOT_APPLICABLE,  # least accusatory
]

# Label scheme (design §3.3).
_LABEL_COLORS = {
    "validity::real": "#d9534f",
    "validity::false-positive": "#6c757d",
    "validity::not-applicable": "#e9ecef",
    "fix-status::open": "#f0ad4e",
    "fix-status::fixed": "#5cb85c",
    "fix-status::unfixed": "#d9534f",
}

_DEFAULT_METRICS: dict[str, int] = {
    "parse_failures_classify": 0,
    "parse_failures_extract": 0,
    "parse_failures_adjudicate": 0,
    "repair_successes": 0,
    "repair_failures": 0,
    "verifier_no_verdict": 0,
    "verifier_transient_retries": 0,
    "adjudication_lens_retries": 0,
    "mcp_errors": 0,
    "closing_commit_not_found": 0,
    "truncated_calls": 0,
    "empty_response_retries": 0,
}

# ---------------------------------------------------------------------------
# Debug log (mirrors audit.py)
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
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GitLabIssue:
    iid: int
    title: str
    description: str
    state: str  # "opened" | "closed"
    labels: list[str]
    web_url: str
    project: str

    @property
    def is_closed(self) -> bool:
        return self.state == "closed"


@dataclass
class IssueClassification:
    iid: int
    issue_type: str  # bug | feature_request | question | docs | discussion | other
    confidence: str
    summary: str = ""


@dataclass
class Claim:
    """Structured, falsifiable claim extracted from an issue body."""
    iid: int
    summary: str
    expected: str
    observed: str
    suspected_locations: list[str]
    claim_statement: str  # may be "NOT-FALSIFIABLE"


@dataclass
class VerifiedClaim:
    iid: int
    claim: Claim
    verdict: str  # one of _ALL_VERDICTS or V_DEFERRED
    grounding_reason: str
    baselines_used: list[str]  # e.g. ["HEAD"] or ["abc123^", "abc123"]
    reproducer_summaries: dict[str, str] = field(default_factory=dict)


@dataclass
class AdjudicationResult:
    iid: int
    final_verdict: str
    verdict_fit: bool
    reason: str
    recalibrated: bool = False
    error: str | None = None


@dataclass
class IssueReviewRunState:
    run_id: str
    project: str
    tag: str
    iid_from: int | None
    iid_to: int | None
    state_filter: str  # opened | closed | all
    queued_issues: list[GitLabIssue] = field(default_factory=list)
    classifications: dict[int, IssueClassification] = field(default_factory=dict)
    candidate_iids: list[int] = field(default_factory=list)
    claims: dict[int, Claim] = field(default_factory=dict)
    verified_claims: dict[int, VerifiedClaim] = field(default_factory=dict)
    final_verdicts: dict[int, str] = field(default_factory=dict)
    written_labels: dict[int, list[str]] = field(default_factory=dict)
    closing_commits: dict[int, str | None] = field(default_factory=dict)
    adjudication_results: dict[int, dict] = field(default_factory=dict)
    verification_state: dict[int, dict] = field(default_factory=dict)
    phase: str = "fetch"
    metrics: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_METRICS))
    dry_run: bool = False
    artifact_dir: Path = field(default_factory=lambda: Path("review-issues-findings"))
    state_dir: Path = field(default_factory=lambda: Path(".swival/review-issues"))

    def save(self) -> None:
        d = self.state_dir / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        blob = {
            "run_id": self.run_id,
            "project": self.project,
            "tag": self.tag,
            "iid_from": self.iid_from,
            "iid_to": self.iid_to,
            "state_filter": self.state_filter,
            "queued_issues": [asdict(i) for i in self.queued_issues],
            "classifications": {str(k): asdict(v) for k, v in self.classifications.items()},
            "candidate_iids": self.candidate_iids,
            "claims": {str(k): asdict(v) for k, v in self.claims.items()},
            "verified_claims": {str(k): asdict(v) for k, v in self.verified_claims.items()},
            "final_verdicts": {str(k): v for k, v in self.final_verdicts.items()},
            "written_labels": {str(k): v for k, v in self.written_labels.items()},
            "closing_commits": {str(k): v for k, v in self.closing_commits.items()},
            "adjudication_results": {str(k): v for k, v in self.adjudication_results.items()},
            "verification_state": {str(k): v for k, v in self.verification_state.items()},
            "phase": self.phase,
            "metrics": dict(self.metrics),
            "dry_run": self.dry_run,
        }
        state_path = d / "state.json"
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=2))
        tmp.replace(state_path)

    @classmethod
    def load(cls, state_dir: Path, run_id: str) -> "IssueReviewRunState":
        d = state_dir / run_id / "state.json"
        blob = json.loads(d.read_text())
        queued = [GitLabIssue(**i) for i in blob.get("queued_issues", [])]
        classifications = {
            int(k): IssueClassification(**v)
            for k, v in blob.get("classifications", {}).items()
        }
        claims = {
            int(k): Claim(**v)
            for k, v in blob.get("claims", {}).items()
        }
        verified = {
            int(k): VerifiedClaim(**v)
            for k, v in blob.get("verified_claims", {}).items()
        }
        return cls(
            run_id=blob["run_id"],
            project=blob["project"],
            tag=blob["tag"],
            iid_from=blob.get("iid_from"),
            iid_to=blob.get("iid_to"),
            state_filter=blob.get("state_filter", "opened"),
            queued_issues=queued,
            classifications=classifications,
            candidate_iids=blob.get("candidate_iids", []),
            claims=claims,
            verified_claims=verified,
            final_verdicts={int(k): v for k, v in blob.get("final_verdicts", {}).items()},
            written_labels={int(k): v for k, v in blob.get("written_labels", {}).items()},
            closing_commits={int(k): v for k, v in blob.get("closing_commits", {}).items()},
            adjudication_results={int(k): v for k, v in blob.get("adjudication_results", {}).items()},
            verification_state={int(k): v for k, v in blob.get("verification_state", {}).items()},
            phase=blob.get("phase", "fetch"),
            metrics={**_DEFAULT_METRICS, **blob.get("metrics", {})},
            dry_run=blob.get("dry_run", False),
            state_dir=state_dir,
        )

    @classmethod
    def find_resumable(
        cls, state_dir: Path, project: str, tag: str,
        iid_from: int | None, iid_to: int | None, state_filter: str,
    ) -> "IssueReviewRunState | None":
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
            if blob.get("state_filter", "opened") != state_filter:
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
# Config
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_CONFIG = {
    "gitlab_server": "gitlab",
    "tool_list_issues": "list_issues",
    "tool_get_issue_notes": "list_issue_notes",
    "tool_list_labels": "list_labels",
    "tool_create_label": "create_label",
    "tool_update_issue": "update_issue",
}


def _load_review_issues_config(base_dir: str) -> dict:
    """Read ``[review-issues]`` from swival.toml, falling back to defaults."""
    config = dict(_DEFAULT_TOOL_CONFIG)
    try:
        from .config import load_toml_config
        toml = load_toml_config(base_dir)
        if toml and "review-issues" in toml:
            override = toml["review-issues"]
            if isinstance(override, dict):
                for k, v in override.items():
                    config[k] = v
    except Exception:
        pass
    return config


# ---------------------------------------------------------------------------
# GitLabAdapter — MCP wrapper
# ---------------------------------------------------------------------------


class GitLabAdapterError(Exception):
    pass


class GitLabAdapter:
    """Config-driven wrapper over the GitLab MCP server.

    All GitLab access goes through ``ctx.mcp_manager.call_tool``.  Because MCP
    tool names vary by server installation, the adapter maps logical operations
    to configured tool names.
    """

    def __init__(self, mcp_manager, config: dict):
        self._mcp = mcp_manager
        self._server = config["gitlab_server"]
        self._tools = config

    def _call(self, tool_key: str, **args) -> str:
        namespaced = f"mcp__{self._server}__{self._tools[tool_key]}"
        try:
            result, is_error = self._mcp.call_tool(namespaced, args)
        except Exception as e:
            raise GitLabAdapterError(f"MCP call {tool_key} failed: {e}") from e
        if is_error:
            raise GitLabAdapterError(f"MCP tool {tool_key} returned error: {result}")
        return result

    # -- issue fetching --------------------------------------------------

    def list_issues(
        self, project: str, tag: str, state: str,
        iid_from: int | None, iid_to: int | None,
    ) -> list[GitLabIssue]:
        """Fetch issues matching the filter, paginating through all results.

        The GitLab MCP server has no ``iid_from``/``iid_to`` support (only a
        single ``iid``), so range filtering is applied client-side after
        fetching.  The server sorts by ``created_at`` by default, not by iid,
        so we must paginate through all results before we can be sure the
        range is fully covered.
        """
        issues: list[GitLabIssue] = []
        page = 1
        per_page = 50
        while True:
            args = {
                "project_id": project,
                "labels": tag,
                "state": state,
                "page": page,
                "per_page": per_page,
            }
            raw = self._call("tool_list_issues", **args)
            batch = _parse_mcp_json(raw)
            if not batch:
                break
            for item in batch:
                issue = _issue_from_dict(item, project)
                if iid_from is not None and issue.iid < iid_from:
                    continue
                if iid_to is not None and issue.iid > iid_to:
                    continue
                issues.append(issue)
            if len(batch) < per_page:
                break
            page += 1
            if page > 200:  # safety valve
                break
        return issues

    def get_issue_notes(self, project: str, iid: int) -> list[str]:
        """Return issue notes/discussion as plain text (best-effort)."""
        try:
            raw = self._call("tool_get_issue_notes", project_id=project, issue_iid=iid)
            notes = _parse_mcp_json(raw)
            if isinstance(notes, list):
                return [n.get("body", "") if isinstance(n, dict) else str(n) for n in notes]
        except GitLabAdapterError:
            pass
        return []

    # -- closing commit resolution --------------------------------------

    def closing_commit_for(self, issue: GitLabIssue, base_dir: str) -> str | None:
        """Resolve the commit that closed the issue.

        Priority:
        1. Commit SHAs mentioned in issue notes/comments (GitLab system notes
           often record "mentioned in commit <sha>" when a closing commit lands;
           developers may also paste a SHA).
        2. Commit-message references (``git log --grep`` for ``Closes #N`` /
           ``Fixes #N`` / ``Resolves #N``).
        3. ``None`` if neither resolves.
        """
        # 1. Search issue notes for commit SHAs.
        notes = self.get_issue_notes(issue.project, issue.iid)
        sha = _find_closing_commit_in_notes(notes, base_dir)
        if sha:
            return sha

        # 2. Fallback: git log --grep for "Fixes #N" / "Closes #N" / "Resolves #N".
        return _find_closing_commit_by_message(issue.iid, base_dir)

    # -- label management ------------------------------------------------

    def ensure_labels_exist(self, project: str, label_names: list[str]) -> None:
        """Create labels that don't yet exist in the project."""
        try:
            raw = self._call("tool_list_labels", project_id=project)
            existing = _parse_mcp_json(raw)
            existing_set = set()
            if isinstance(existing, list):
                for lbl in existing:
                    if isinstance(lbl, dict):
                        existing_set.add(lbl.get("name", ""))
                    elif isinstance(lbl, str):
                        existing_set.add(lbl)
        except GitLabAdapterError:
            existing_set = set()

        for name in label_names:
            if name in existing_set:
                continue
            color = _LABEL_COLORS.get(name, "#5843BE")
            try:
                self._call(
                    "tool_create_label",
                    project_id=project, name=name, color=color,
                )
            except GitLabAdapterError as e:
                _ui_warning(None, f"could not create label {name!r}: {e}")

    def apply_labels(self, project: str, iid: int, labels: list[str]) -> None:
        """Set issue labels (idempotent at the API level).

        Uses ``update_issue`` — the server has no dedicated set-labels tool.
        ``labels`` replaces the issue's full label set, which is what we want
        for verdict writeback.
        """
        self._call(
            "tool_update_issue",
            project_id=project, issue_iid=iid, labels=labels,
        )

    def close_issue(self, project: str, iid: int) -> None:
        """Close an issue via ``update_issue`` with ``state_event=close``."""
        self._call(
            "tool_update_issue",
            project_id=project, issue_iid=iid, state_event="close",
        )


# ---------------------------------------------------------------------------
# MCP response parsing helpers
# ---------------------------------------------------------------------------


def _parse_mcp_json(raw: str) -> list | dict:
    """Best-effort parse of an MCP tool result into JSON.

    MCP results are text; some servers wrap JSON in markdown fences or prepend
    prose.  Try direct JSON first, then extract the first JSON array/object.
    """
    if not raw:
        return []
    raw = raw.strip()
    # Strip markdown code fences.
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove first and last fence lines.
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting the first JSON array or object.
    for start_char, end_char in (("[", "]"), ("{", "}")):
        start = raw.find(start_char)
        if start == -1:
            continue
        end = raw.rfind(end_char)
        if end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    return []


def _issue_from_dict(d: dict, project: str) -> GitLabIssue:
    return GitLabIssue(
        iid=d.get("iid", d.get("id", 0)),
        title=d.get("title", ""),
        description=d.get("description", d.get("body", "")),
        state=d.get("state", "opened"),
        labels=d.get("labels", []),
        web_url=d.get("web_url", d.get("url", "")),
        project=project,
    )


# ---------------------------------------------------------------------------
# Git helpers — commit-parameterized variants
# ---------------------------------------------------------------------------

_CLOSING_KEYWORDS = re.compile(
    r"(?:fixes|closes|resolves|fixed|closed|resolved)\s+#+(\d+)",
    re.IGNORECASE,
)


_COMMIT_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")


def _find_closing_commit_in_notes(notes: list[str], base_dir: str) -> str | None:
    """Search issue notes for a commit SHA that exists in the repo.

    GitLab system notes often record ``mentioned in commit <sha>`` when a
    closing commit lands, and developers may paste SHAs in comments.  We scan
    all notes for hex strings (7–40 chars), validate each against the repo
    with ``git rev-parse``, and return the first that resolves.
    """
    for note in notes:
        if not note:
            continue
        for match in _COMMIT_SHA_RE.finditer(note):
            sha = match.group(1)
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--verify", sha],
                    capture_output=True,
                    text=True,
                    cwd=base_dir,
                    timeout=10,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except (subprocess.SubprocessError, OSError):
                continue
    return None


def _find_closing_commit_by_message(iid: int, base_dir: str) -> str | None:
    """Find the earliest commit that references ``Closes #<iid>``."""
    pattern = f"(?:Fixes|Closes|Resolves)\\s+#{iid}"
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--grep", pattern, "--format=%H", "--reverse"],
            capture_output=True,
            text=True,
            cwd=base_dir,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().splitlines()
        return lines[0] if lines else None
    except (subprocess.SubprocessError, OSError):
        return None


def _git_show_at(path: str, commit: str, base_dir: str) -> str:
    """Read ``path`` at ``commit`` (variant of audit._git_show which uses HEAD)."""
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        capture_output=True,
        cwd=base_dir,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git show {commit}:{path} failed: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout.decode(errors="replace")


def _git_rev_parse(ref: str, base_dir: str) -> str:
    return _git(["rev-parse", ref], base_dir)


class _worktree_at:
    """Context manager for a temporary git worktree at a specific commit.

    Variant of ``audit._worktree`` (which always checks out HEAD).
    """

    def __init__(self, base_dir: str, work_dir: Path, commit: str):
        self.base_dir = base_dir
        self.work_dir = work_dir
        self.commit = commit

    def _cleanup(self) -> None:
        try:
            _git(["worktree", "prune"], self.base_dir)
        except RuntimeError:
            pass
        if self.work_dir.exists():
            try:
                _git(
                    ["worktree", "remove", "--force", str(self.work_dir)],
                    self.base_dir,
                )
            except RuntimeError:
                pass
            if self.work_dir.exists():
                shutil.rmtree(self.work_dir, ignore_errors=True)

    def __enter__(self) -> Path:
        self.work_dir.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup()
        _git(
            ["worktree", "add", "--detach", str(self.work_dir), self.commit],
            self.base_dir,
        )
        return self.work_dir

    def __exit__(self, *exc):
        self._cleanup()
        return False


def _git_diff_name_only(base: str, head: str, base_dir: str) -> list[str]:
    """Return the list of file paths changed between ``base`` and ``head``."""
    result = subprocess.run(
        ["git", "diff", "--name-only", base, head],
        capture_output=True,
        cwd=base_dir,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff --name-only {base} {head} failed: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    out = result.stdout.decode(errors="replace")
    return [line for line in out.splitlines() if line.strip()]


def _git_diff_patch(base: str, head: str, base_dir: str) -> str:
    """Return the unified diff patch between ``base`` and ``head``."""
    result = subprocess.run(
        ["git", "diff", base, head],
        capture_output=True,
        cwd=base_dir,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff {base} {head} failed: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout.decode(errors="replace")


def _claim_evidence_paths(claim: Claim) -> list[str]:
    """Extract deduplicated file paths from the claim's suspected_locations."""
    seen: set[str] = set()
    paths: list[str] = []
    for loc in claim.suspected_locations:
        fpath = loc.split(":")[0]
        if fpath and fpath not in seen:
            seen.add(fpath)
            paths.append(fpath)
    return paths


def _gather_claim_evidence(
    claim: Claim, commit: str, base_dir: str,
    diff_range: tuple[str, str] | None = None,
) -> tuple[str, int]:
    """Collect committed file contents at ``commit`` for the claim's locations.

    Returns ``(evidence_text, n_files)``.  If the claim names no locations,
    returns a minimal hint so the verifier knows it must search.

    When ``diff_range`` ``(base, head)`` is given (closed issues: the
    ``parent..closing_commit`` range), files changed by that diff are also
    gathered at ``commit`` and the diff patch is included in the evidence.
    This gives the verifier the actual fix changes even when the claim names
    no specific locations.
    """
    # Collect paths from the claim's cited locations.
    paths: list[str] = _claim_evidence_paths(claim)

    # Augment with files changed by the diff, if a range was provided.
    diff_patch = ""
    if diff_range is not None:
        d_base, d_head = diff_range
        try:
            diff_files = _git_diff_name_only(d_base, d_head, base_dir)
        except RuntimeError:
            diff_files = []
        seen = set(paths)
        for fpath in diff_files:
            if fpath not in seen:
                seen.add(fpath)
                paths.append(fpath)
        try:
            diff_patch = _git_diff_patch(d_base, d_head, base_dir)
        except RuntimeError:
            diff_patch = ""

    if not paths:
        return (
            "(The issue does not name specific files. Search the repository "
            "in the worktree to locate the relevant code.)",
            0,
        )
    parts: list[str] = []
    n = 0
    for fpath in paths:
        try:
            content = _git_show_at(fpath, commit, base_dir)
        except RuntimeError:
            continue
        parts.append(f"--- {fpath} ---\n{content}")
        n += 1

    if diff_patch.strip():
        parts.append(f"--- diff {diff_range[0][:8]}..{diff_range[1][:8]} ---\n{diff_patch}")

    if not parts:
        return (
            f"(None of the cited paths ({', '.join(paths)}) exist at {commit}.)",
            0,
        )
    return "\n\n".join(parts), n


# ---------------------------------------------------------------------------
# Schemas & prompts
# ---------------------------------------------------------------------------

_ISSUE_CLASS_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="classification",
        required=("issue_type", "confidence"),
        enums={
            "issue_type": (
                "bug", "feature_request", "question",
                "docs", "discussion", "other",
            ),
            "confidence": ("high", "medium", "low"),
        },
    ),
    cardinality="one",
    allow_none=False,
)

_ISSUE_CLASS_WORKED_EXAMPLE = """\
@@ classification @@
issue_type: bug
confidence: high"""

_ISSUE_CLASS_SYSTEM = """\
You are classifying one GitLab issue by type. Read the title and description.

`bug` = reports a defect, incorrect behavior, crash, or broken functionality \
that is verifiable against source code. Everything else (feature requests, \
questions, docs, discussions, planning) is not a veracity claim.

When unsure, choose `bug` only if the issue makes a concrete claim about code \
behavior that can be checked against source. Otherwise classify by its \
primary intent.

Output exactly one `@@ classification @@` block:
- issue_type: bug | feature_request | question | docs | discussion | other
- confidence: high | medium | low

Use exactly the keys shown. Do not quote, escape, or wrap values."""

_CLAIM_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="claim",
        required=(
            "summary", "expected", "observed",
            "suspected_location", "claim_statement",
        ),
        repeated={"suspected_location": "suspected_locations"},
        multiline=("observed", "claim_statement"),
    ),
    cardinality="one",
    allow_none=False,
)

_CLAIM_WORKED_EXAMPLE = """\
@@ claim @@
summary: login crashes when email field is empty
expected: empty email should show a validation error
observed: the server raises an unhandled NullReferenceException and returns 500
suspected_location: src/auth/login.py:142
claim_statement: the login handler at login.py:142 dereferences the email field \
without a null check, so an empty submission causes a 500 error instead of a \
validation response."""

_CLAIM_SYSTEM = """\
You are extracting a verifiable claim from one GitLab issue. Use only the \
issue title, description, and notes provided. Produce a structured claim that \
a later phase will verify against source code.

Output exactly one `@@ claim @@` block:
- summary: one-line restatement of what the issue claims is wrong
- expected: what the issue says should happen
- observed: what the issue says actually happens (crash, wrong output, etc.); \
may span multiple indented lines
- suspected_location: one path:line or path:symbol per line; repeat the line \
for each (omit entirely if the issue names none)
- claim_statement: the falsifiable proposition — "X happens because Y, under \
conditions Z"; may span multiple indented lines. If the issue does not make a \
concrete, falsifiable claim about code behavior, set claim_statement to \
NOT-FALSIFIABLE

Use exactly the keys shown. Do not quote, escape, or wrap values."""

_VERIFY_SYSTEM = """\
You are verifying one GitLab issue's claim against committed source code in an \
isolated worktree. Determine whether the claim describes a real defect that \
manifests in this code. Treat the claim as a hypothesis, not ground truth.

Rules:
- You may inspect the code, or compile/run small proof-of-concept code if that \
helps.
- Use the committed source in the worktree only.
- A proof counts if you can identify the trigger path, the failing operation \
or violated invariant, and the practical incorrect/crashing/corrupting outcome \
from the code, or demonstrate equivalent runtime evidence.
- Reject as REFUTED when the code does not support the claim, or when an \
existing guard already prevents the described behavior.
- Reject defense-in-depth arguments — if today's code already blocks the \
described behavior, the claim is REFUTED.

End your final response with exactly one token on its own line:
CONFIRMED
REFUTED"""

_VALIDITY_LENSES = (
    "Lens: grounding & accuracy. Re-read the cited code at each baseline \
yourself. Is the claim actually confirmed/refuted as the verdict states? Did \
the verifier misread the code or miss a guard? If the verdict contradicts the \
code, reject the verdict (vote false_positive).",

    "Lens: fix-validity (closed issues only). For a real::fixed verdict, \
confirm the closing commit genuinely addresses the described claim, not just \
an adjacent symptom. For real::unfixed, confirm the claim truly still \
reproduces at the closing commit and the close wasn't legitimate for a \
different reason. For open issues this lens is a no-op pass.",

    "Lens: verdict-fit & significance. Is the verdict label correct for the \
evidence, and does this issue deserve a validity verdict at all? A vague issue \
forced into false-positive::never-real might better be not-applicable. \
Recalibrate the verdict to the realistic evidence; tie-break toward the more \
conservative reading.",
)

_VALIDITY_VERDICT_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="verdict",
        required=("verdict", "verdict_fit", "reason"),
        enums={
            "verdict": _ALL_VERDICTS,
            "verdict_fit": ("yes", "no"),
        },
    ),
    cardinality="one",
    allow_none=False,
)

_VALIDITY_VERDICT_WORKED_EXAMPLE = """\
@@ verdict @@
verdict: real::fixed
verdict_fit: yes
reason: claim was real at parent (unhandled null at login.py:142) and the \
closing commit added the null check, so it no longer reproduces"""

_VALIDITY_VERDICT_SYSTEM = """\
You are adjudicating one already-verified GitLab issue validity verdict. A \
prior phase checked the claim against the code and produced a verdict. Your \
job is the opposite: try to REFUTE this verdict. Default to verdict_fit=no \
unless the evidence forces otherwise. We would rather drop an uncertain \
verdict than write a wrong label to GitLab.

{lens}

A verdict is fit only if, under today's committed code (or the closing commit \
for closed issues), the evidence directly supports it:
- For real::open: the claim reproduces at HEAD.
- For real::fixed: the claim was real at the parent and the closing commit \
fixes it.
- For real::unfixed: the claim was real at the parent and still reproduces at \
the closing commit.
- For false-positive::never-real: the claim does not hold at the baseline.
- For not-applicable: the issue is not a bug-type claim.

You may recalibrate the verdict to a more conservative one (toward \
not-applicable), never to a more accusatory one (toward real::unfixed).

Output exactly one `@@ verdict @@` block:
- verdict: the verdict you agree with (may differ from the proposed one)
- verdict_fit: yes | no (yes = you agree the verdict is justified)
- reason: one line under 30 words

Use exactly the keys shown. Do not quote, escape, or wrap values."""

_REPORT_TEMPLATE = """\
You are writing the final markdown report for one GitLab issue validity \
review.

Use exactly this structure:
- # Issue !{iid}: {title}
- ## Verdict
- ## Claim
- ## Evidence Summary
- ## Grounding Result
- ## Adjudication Notes
- ## Labels Applied

Be terse, factual, and evidence-driven. Verdict must be one of: \
real::open, real::fixed, real::unfixed, false-positive::never-real, \
not-applicable."""


# ---------------------------------------------------------------------------
# Verdict-line parsing
# ---------------------------------------------------------------------------

_CONFIRMED_KEYWORD = "CONFIRMED"
_REFUTED_KEYWORD = "REFUTED"


def _parse_validity_verdict_line(answer: str) -> bool | None:
    """Parse the final CONFIRMED/REFUTED token (mirrors _parse_verdict_line)."""
    for line in reversed(answer.splitlines()):
        token = line.strip()
        if not token:
            continue
        if token == _CONFIRMED_KEYWORD:
            return True
        if token == _REFUTED_KEYWORD:
            return False
    return None


# ---------------------------------------------------------------------------
# Conservative recalibration
# ---------------------------------------------------------------------------


def _more_conservative(proposed: str, original: str) -> str:
    """Return ``proposed`` only if it is at least as conservative as ``original``.

    Verdicts may move rightward in ``_CONSERVATIVE_ORDER`` (toward
    not-applicable), never leftward (toward real::unfixed).
    """
    oi = _CONSERVATIVE_ORDER.index(original) if original in _CONSERVATIVE_ORDER else 0
    pi = _CONSERVATIVE_ORDER.index(proposed) if proposed in _CONSERVATIVE_ORDER else len(_CONSERVATIVE_ORDER)
    return proposed if pi >= oi else original


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------


def _phase_classify_one(
    issue: GitLabIssue,
    state: IssueReviewRunState,
    ctx: "InputContext",
) -> IssueClassification:
    """Phase 2: classify one issue as bug or not-applicable."""
    user = (
        f"Issue !{issue.iid}: {issue.title}\n\n"
        f"Description:\n{issue.description or '(empty)'}"
    )
    messages = [
        {"role": "system", "content": _ISSUE_CLASS_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review-issues: classify !{issue.iid}",
        metrics=state.metrics,
    )
    try:
        records = _parse_records_with_repair(
            ctx, raw,
            schema=_ISSUE_CLASS_SCHEMA,
            worked_example=_ISSUE_CLASS_WORKED_EXAMPLE,
            metrics=state.metrics,
        )
    except ValueError:
        records = []
    if not records:
        return IssueClassification(
            iid=issue.iid, issue_type="other", confidence="low",
            summary="classification parse failed",
        )
    r = records[0]
    return IssueClassification(
        iid=issue.iid,
        issue_type=r.get("issue_type", "other"),
        confidence=r.get("confidence", "low"),
        summary=issue.title[:80],
    )


def _phase_extract_claim(
    issue: GitLabIssue,
    state: IssueReviewRunState,
    ctx: "InputContext",
    adapter: GitLabAdapter,
) -> Claim:
    """Phase 3: extract a structured claim from the issue body + notes."""
    notes = adapter.get_issue_notes(issue.project, issue.iid)
    notes_text = "\n\n".join(n for n in notes if n) if notes else ""
    user = (
        f"Issue !{issue.iid}: {issue.title}\n\n"
        f"Description:\n{issue.description or '(empty)'}"
    )
    if notes_text:
        user += f"\n\nDiscussion notes:\n{notes_text}"

    messages = [
        {"role": "system", "content": _CLAIM_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review-issues: extract claim !{issue.iid}",
        metrics=state.metrics,
    )
    try:
        records = _parse_records_with_repair(
            ctx, raw,
            schema=_CLAIM_SCHEMA,
            worked_example=_CLAIM_WORKED_EXAMPLE,
            metrics=state.metrics,
        )
    except ValueError:
        records = []
    if not records:
        return Claim(
            iid=issue.iid,
            summary=issue.title[:80],
            expected="",
            observed="",
            suspected_locations=[],
            claim_statement="NOT-FALSIFIABLE",
        )
    r = records[0]
    return Claim(
        iid=issue.iid,
        summary=r.get("summary", issue.title[:80]),
        expected=r.get("expected", ""),
        observed=r.get("observed", ""),
        suspected_locations=r.get("suspected_locations", []),
        claim_statement=r.get("claim_statement", "NOT-FALSIFIABLE"),
    )


# ---------------------------------------------------------------------------
# Phase 4: Grounding
# ---------------------------------------------------------------------------


def _verify_claim_at_commit(
    claim: Claim,
    commit: str,
    base_dir: str,
    ctx: "InputContext",
    state: IssueReviewRunState,
    work_dir: Path,
    ui: "AuditUI | None" = None,
    diff_range: tuple[str, str] | None = None,
) -> tuple[bool, str]:
    """Run the verifier agent in a worktree at ``commit``.

    Returns ``(confirmed, summary)``.  Raises ``_TransientVerifierError`` on
    infrastructure failure (no verdict token).
    """
    from .agent import run_agent_loop

    evidence, n_files = _gather_claim_evidence(claim, commit, base_dir, diff_range=diff_range)
    _ui_info(ui, f"    verifier [!{claim.iid} @ {commit[:8]}]: {n_files} evidence file(s)")

    claim_json = json.dumps(asdict(claim), indent=2)

    with _worktree_at(base_dir, work_dir, commit):
        messages = [
            {"role": "system", "content": _VERIFY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Proposed claim (issue !{claim.iid}):\n{claim_json}\n\n"
                    f"Committed evidence bundle (at {commit[:8]}):\n{evidence}"
                ),
            },
        ]
        kw = _make_isolated_loop_kwargs(ctx, work_dir, max_turns=_DEFAULT_VERIFY_MAX_TURNS)

        try:
            answer, _exhausted = run_agent_loop(messages, ctx.tools, **kw)
        except (ConnectionError, TimeoutError, OSError) as e:
            raise _TransientVerifierError(str(e)) from e
        finally:
            _write_audit_trace(
                ctx, messages,
                task=f"review-issues: verify !{claim.iid} @ {commit[:8]}",
            )

        answer = answer or ""
        verdict = _parse_validity_verdict_line(answer)
        if verdict is None:
            state.metrics["verifier_no_verdict"] += 1
            raise _TransientVerifierError(
                "verifier produced no verdict token"
                + (" (turn budget exhausted)" if _exhausted else "")
            )
        summary = answer[-1000:]
        if verdict:
            _ui_info(ui, f"    verifier [!{claim.iid} @ {commit[:8]}]: CONFIRMED")
        else:
            _ui_info(ui, f"    verifier [!{claim.iid} @ {commit[:8]}]: REFUTED")
        return verdict, summary


def _verify_with_retry(
    claim: Claim,
    commit: str,
    base_dir: str,
    ctx: "InputContext",
    state: IssueReviewRunState,
    work_dir: Path,
    ui: "AuditUI | None" = None,
    diff_range: tuple[str, str] | None = None,
) -> tuple[bool, str]:
    """Verify with one transient-error retry (mirrors _verify_one_finding)."""
    try:
        return _verify_claim_at_commit(
            claim, commit, base_dir, ctx, state, work_dir, ui=ui, diff_range=diff_range,
        )
    except _TransientVerifierError as e:
        state.metrics["verifier_transient_retries"] += 1
        _ui_info(ui, f"  [!{claim.iid}] retrying after transient error: {e}")
        try:
            return _verify_claim_at_commit(
                claim, commit, base_dir, ctx, state, work_dir, ui=ui, diff_range=diff_range,
            )
        except Exception:
            raise


def _grounding_one(
    issue: GitLabIssue,
    claim: Claim,
    state: IssueReviewRunState,
    ctx: "InputContext",
    adapter: GitLabAdapter,
    ui: "AuditUI | None" = None,
) -> VerifiedClaim:
    """Phase 4: verify a claim and produce a verdict.

    Open issues → verify at HEAD.
    Closed issues → verify at parent C^ AND closing commit C.
    """
    base_dir = ctx.base_dir
    run_dir = Path(base_dir) / state.state_dir / state.run_id / "verify" / f"issue-{claim.iid}"

    # --- Open issue: verify at HEAD ---
    if not issue.is_closed:
        head = _git_rev_parse("HEAD", base_dir)
        confirmed, summary = _verify_with_retry(
            claim, head, base_dir, ctx, state, run_dir / "head", ui=ui,
        )
        verdict = V_REAL_OPEN if confirmed else V_FALSE_POSITIVE
        state.verification_state[claim.iid] = {
            "baseline": "HEAD",
            "commit": head,
            "confirmed": confirmed,
        }
        return VerifiedClaim(
            iid=claim.iid, claim=claim, verdict=verdict,
            grounding_reason="verified at HEAD",
            baselines_used=["HEAD"],
            reproducer_summaries={"HEAD": summary},
        )

    # --- Closed issue: find closing commit, verify at parent + closing ---
    closing_commit = state.closing_commits.get(claim.iid, "unset")
    if closing_commit == "unset" or closing_commit is None:
        closing_commit = adapter.closing_commit_for(issue, base_dir)
        state.closing_commits[claim.iid] = closing_commit

    if closing_commit is None:
        # No closing commit found — fall back to HEAD with a warning.
        state.metrics["closing_commit_not_found"] += 1
        _ui_warning(
            ui,
            f"  [!{claim.iid}] no closing commit found; "
            f"verifying at HEAD only (verdict will be flagged deferred)",
        )
        head = _git_rev_parse("HEAD", base_dir)
        confirmed, summary = _verify_with_retry(
            claim, head, base_dir, ctx, state, run_dir / "head", ui=ui,
        )
        verdict = V_DEFERRED if confirmed else V_FALSE_POSITIVE
        state.verification_state[claim.iid] = {
            "baseline": "HEAD",
            "commit": head,
            "confirmed": confirmed,
            "closing_commit": None,
            "fallback": True,
        }
        return VerifiedClaim(
            iid=claim.iid, claim=claim, verdict=verdict,
            grounding_reason="no closing commit found; verified at HEAD only",
            baselines_used=["HEAD"],
            reproducer_summaries={"HEAD": summary},
        )

    # Verify at parent C^.
    parent = _git_rev_parse(f"{closing_commit}^", base_dir)
    _ui_info(ui, f"  [!{claim.iid}] verifying at parent {parent[:8]} (was it real?)")
    confirmed_at_parent, summary_parent = _verify_with_retry(
        claim, parent, base_dir, ctx, state, run_dir / "parent", ui=ui,
        diff_range=(parent, closing_commit),
    )

    if not confirmed_at_parent:
        # Never real — false positive.
        verdict = V_FALSE_POSITIVE
        state.verification_state[claim.iid] = {
            "parent": parent, "parent_confirmed": False,
            "closing_commit": closing_commit,
        }
        return VerifiedClaim(
            iid=claim.iid, claim=claim, verdict=verdict,
            grounding_reason=f"claim refuted at parent {parent[:8]} — never real",
            baselines_used=[parent],
            reproducer_summaries={parent: summary_parent},
        )

    # Confirmed at parent — now verify at closing commit C.
    _ui_info(
        ui,
        f"  [!{claim.iid}] verifying at closing commit {closing_commit[:8]} (is it fixed?)",
    )
    confirmed_at_closing, summary_closing = _verify_with_retry(
        claim, closing_commit, base_dir, ctx, state, run_dir / "closing", ui=ui,
        diff_range=(parent, closing_commit),
    )

    if confirmed_at_closing:
        # Still reproduces after the fix — unfixed.
        verdict = V_REAL_UNFIXED
        reason = (
            f"claim confirmed at parent {parent[:8]} AND at closing commit "
            f"{closing_commit[:8]} — closed but not actually fixed"
        )
    else:
        # Was real, now fixed.
        verdict = V_REAL_FIXED
        reason = (
            f"claim confirmed at parent {parent[:8]}, refuted at closing commit "
            f"{closing_commit[:8]} — was real, now fixed"
        )

    state.verification_state[claim.iid] = {
        "parent": parent, "parent_confirmed": True,
        "closing_commit": closing_commit,
        "closing_confirmed": confirmed_at_closing,
    }
    return VerifiedClaim(
        iid=claim.iid, claim=claim, verdict=verdict,
        grounding_reason=reason,
        baselines_used=[parent, closing_commit],
        reproducer_summaries={
            parent: summary_parent,
            closing_commit: summary_closing,
        },
    )


# ---------------------------------------------------------------------------
# Phase 4.5: Adjudication
# ---------------------------------------------------------------------------


def _adjudicate_one(
    item: tuple[int, VerifiedClaim],
    state: IssueReviewRunState,
    ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> AdjudicationResult:
    """Refute or confirm one verdict through a three-lens panel."""
    _idx, vc = item
    iid = vc.iid
    proposed_verdict = vc.verdict

    # Build the evidence summary for the panel.
    evidence_parts: list[str] = []
    for baseline, summary in vc.reproducer_summaries.items():
        evidence_parts.append(f"Baseline {baseline[:12]}:\n{summary}")
    evidence_text = "\n\n".join(evidence_parts) if evidence_parts else "(none)"

    claim_json = json.dumps(asdict(vc.claim), indent=2)

    verdicts: list[dict] = []
    for lens in _VALIDITY_LENSES:
        v = None
        for attempt in range(2):
            if attempt:
                state.metrics["adjudication_lens_retries"] += 1
            user = (
                f"Proposed verdict: {proposed_verdict}\n\n"
                f"Claim (issue !{iid}):\n{claim_json}\n\n"
                f"Grounding reason: {vc.grounding_reason}\n\n"
                f"Baselines used: {', '.join(b[:12] for b in vc.baselines_used)}\n\n"
                f"Verifier summaries:\n{evidence_text}"
            )
            messages = [
                {"role": "system", "content": _VALIDITY_VERDICT_SYSTEM.format(lens=lens)},
                {"role": "user", "content": user},
            ]
            raw = _call_audit_llm(
                ctx, messages,
                trace_task=f"review-issues: adjudicate !{iid}",
                metrics=state.metrics,
            )
            try:
                records = _parse_records_with_repair(
                    ctx, raw,
                    schema=_VALIDITY_VERDICT_SCHEMA,
                    worked_example=_VALIDITY_VERDICT_WORKED_EXAMPLE,
                    metrics=state.metrics,
                )
            except ValueError:
                records = []
            if records:
                v = records[0]
                break
        if v is not None:
            verdicts.append(v)

    if len(verdicts) < 2:
        return AdjudicationResult(
            iid=iid, final_verdict=proposed_verdict, verdict_fit=True,
            reason=f"adjudication inconclusive: only {len(verdicts)} usable verdict(s)",
        )

    confirming = [v for v in verdicts if v.get("verdict_fit") == "yes"]
    if len(confirming) * 2 <= len(verdicts):
        # Majority rejected — drop to the most conservative proposed.
        return AdjudicationResult(
            iid=iid, final_verdict=V_NOT_APPLICABLE, verdict_fit=False,
            reason="; ".join(v.get("reason", "") for v in verdicts if v.get("reason"))[:300],
        )

    # Majority confirmed.  Check if any lens recalibrated the verdict.
    recalibrated_verdicts = [
        v.get("verdict", proposed_verdict) for v in confirming
    ]
    # Use the most common recalibrated verdict, conservative-tie-broken.
    from collections import Counter
    counts = Counter(recalibrated_verdicts)
    panel_verdict = max(
        recalibrated_verdicts,
        key=lambda v: (counts[v], -_CONSERVATIVE_ORDER.index(v) if v in _CONSERVATIVE_ORDER else 0),
    )

    final = _more_conservative(panel_verdict, proposed_verdict)
    recalibrated = final != proposed_verdict
    reason = "; ".join(v.get("reason", "") for v in confirming if v.get("reason"))[:300]

    return AdjudicationResult(
        iid=iid, final_verdict=final, verdict_fit=True,
        reason=reason, recalibrated=recalibrated,
    )


# ---------------------------------------------------------------------------
# Phase 5: Write-back & report
# ---------------------------------------------------------------------------


def _labels_for_verdict(verdict: str) -> list[str]:
    """Map a verdict to the GitLab label set (design §3.3)."""
    if verdict == V_REAL_OPEN:
        return ["validity::real", "fix-status::open"]
    if verdict == V_REAL_FIXED:
        return ["validity::real", "fix-status::fixed"]
    if verdict == V_REAL_UNFIXED:
        return ["validity::real", "fix-status::unfixed"]
    if verdict == V_FALSE_POSITIVE:
        return ["validity::false-positive"]
    if verdict == V_NOT_APPLICABLE:
        return ["validity::not-applicable"]
    # V_DEFERRED — no labels (couldn't determine).
    return []


def _phase_writeback(
    state: IssueReviewRunState,
    ctx: "InputContext",
    adapter: GitLabAdapter,
    ui: AuditUI,
) -> None:
    """Apply labels to GitLab and produce the report."""
    all_labels = set()
    for verdict in state.final_verdicts.values():
        all_labels.update(_labels_for_verdict(verdict))

    if all_labels and not state.dry_run:
        _ui_info(ui, "ensuring GitLab labels exist...")
        adapter.ensure_labels_exist(state.project, sorted(all_labels))

    ph = ui.phase("Phase 5 · Write-back", total=len(state.final_verdicts), color="cyan")
    if not ui.is_live:
        fmt.info(f"phase 5: writing back {len(state.final_verdicts)} verdict(s)...")

    for iid, verdict in state.final_verdicts.items():
        labels = _labels_for_verdict(verdict)
        if labels and not state.dry_run:
            try:
                adapter.apply_labels(state.project, iid, labels)
                state.written_labels[iid] = labels
            except GitLabAdapterError as e:
                state.metrics["mcp_errors"] += 1
                _ui_warning(ui, f"  [!{iid}] label write-back failed: {e}")
        elif labels and state.dry_run:
            _ui_info(ui, f"  [!{iid}] (dry-run) would apply: {', '.join(labels)}")
            state.written_labels[iid] = labels
        ph.advance()
        state.save()

    ph.complete(f"{len(state.written_labels)} labeled")
    if not ui.is_live:
        fmt.info(f"phase 5 complete. {len(state.written_labels)} issue(s) labeled.")

    # Write the report.
    _write_report(state, ctx)
    state.phase = "done"
    state.save()


def _write_report(state: IssueReviewRunState, ctx: "InputContext") -> str:
    """Produce the markdown report and write it to the artifact dir."""
    artifact_dir = Path(ctx.base_dir) / state.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# GitLab Issue Validity Review — Report\n")
    lines.append(f"- **Project:** {state.project}")
    lines.append(f"- **Tag:** {state.tag}")
    range_str = str(state.iid_from) if state.iid_from else "all"
    if state.iid_to:
        range_str = f"{state.iid_from}–{state.iid_to}"
    lines.append(f"- **IID range:** {range_str}")
    lines.append(f"- **State filter:** {state.state_filter}")
    lines.append(f"- **Run ID:** {state.run_id}")
    lines.append(f"- **Dry run:** {state.dry_run}\n")

    # Summary tally.
    tally: dict[str, int] = {}
    for v in state.final_verdicts.values():
        tally[v] = tally.get(v, 0) + 1
    lines.append("## Summary\n")
    lines.append("| Verdict | Count |")
    lines.append("|---|---|")
    for v in _ALL_VERDICTS + (V_DEFERRED,):
        if v in tally:
            lines.append(f"| `{v}` | {tally[v]} |")
    lines.append("")

    # Per-issue table.
    lines.append("## Issues\n")
    lines.append("| IID | Title | State | Verdict | Baselines | Link |")
    lines.append("|---|---|---|---|---|---|")
    issue_map = {i.iid: i for i in state.queued_issues}
    for iid in sorted(state.final_verdicts):
        verdict = state.final_verdicts[iid]
        issue = issue_map.get(iid)
        title = issue.title[:50] if issue else "?"
        issue_state = issue.state if issue else "?"
        vc = state.verified_claims.get(iid)
        baselines = ", ".join(b[:8] for b in vc.baselines_used) if vc else "—"
        link = f"[→]({issue.web_url})" if issue and issue.web_url else "—"
        lines.append(
            f"| !{iid} | {title} | {issue_state} | `{verdict}` | {baselines} | {link} |"
        )
    lines.append("")

    # Per-issue detail.
    lines.append("## Details\n")
    for iid in sorted(state.final_verdicts):
        issue = issue_map.get(iid)
        verdict = state.final_verdicts[iid]
        vc = state.verified_claims.get(iid)
        claim = state.claims.get(iid)
        adj = state.adjudication_results.get(iid, {})
        labels = state.written_labels.get(iid, [])

        lines.append(f"### Issue !{iid}: {issue.title if issue else '?'}\n")
        if issue and issue.web_url:
            lines.append(f"[View on GitLab]({issue.web_url})\n")
        lines.append(f"**Verdict:** `{verdict}`\n")
        if labels:
            lines.append(f"**Labels applied:** {', '.join(labels)}\n")
        if claim:
            lines.append(f"**Claim:** {claim.summary}")
            lines.append(f"- Expected: {claim.expected}")
            lines.append(f"- Observed: {claim.observed[:200]}")
            lines.append(f"- Claim statement: {claim.claim_statement[:200]}\n")
        if vc:
            lines.append(f"**Grounding:** {vc.grounding_reason}")
            lines.append(f"**Baselines:** {', '.join(vc.baselines_used)}\n")
        if adj:
            lines.append(f"**Adjudication:** {adj.get('reason', '')}")
            if adj.get("recalibrated"):
                lines.append(" (verdict was recalibrated by the panel)")
            lines.append("")
        lines.append("---\n")

    report = "\n".join(lines)
    report_path = artifact_dir / "README.md"
    report_path.write_text(report)
    return report


# ---------------------------------------------------------------------------
# Phase titles & pipeline driver
# ---------------------------------------------------------------------------

_PHASE_TITLES: dict[str, tuple[str, str]] = {
    "fetch": ("Phase 1 · Fetch", "fetch"),
    "classify": ("Phase 2 · Classify", "classify"),
    "extract": ("Phase 3 · Extract", "extract"),
    "grounding": ("Phase 4 · Grounding", "grounding"),
    "adjudication": ("Phase 4.5 · Adjudication", "grounding"),
    "writeback": ("Phase 5 · Write-back", "writeback"),
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
    state: IssueReviewRunState,
    ui: AuditUI,
    ctx: "InputContext",
    adapter: GitLabAdapter,
    base_dir: str,
    workers: int,
    *,
    resume: bool,
) -> str:
    """Drive the full pipeline, phase-gated for resume."""

    # ---- Phase 1: Fetch ----
    if state.phase == "fetch":
        ph1 = _phase_open(ui, "fetch", label=state.project)
        if not ui.is_live:
            fmt.info(f"phase 1: fetching issues from {state.project} (tag={state.tag})...")
        try:
            state.queued_issues = adapter.list_issues(
                state.project, state.tag, state.state_filter,
                state.iid_from, state.iid_to,
            )
        except GitLabAdapterError as e:
            return f"error: failed to fetch issues: {e}"
        ph1.complete(f"{len(state.queued_issues)} issue(s)")
        if not ui.is_live:
            fmt.info(f"phase 1 complete. {len(state.queued_issues)} issue(s) fetched.")
        if not state.queued_issues:
            state.phase = "done"
            state.save()
            return "no issues matched the filter."
        state.phase = "classify"
        state.save()

    # ---- Phase 2: Classify ----
    if state.phase == "classify":
        items = [i for i in state.queued_issues if i.iid not in state.classifications]
        ph2 = _phase_open(ui, "classify", total=len(state.queued_issues))
        if not ui.is_live:
            fmt.info(f"phase 2: classifying {len(items)} issue(s)...")

        def _fn(issue):
            return _phase_classify_one(issue, state, ctx)

        def _on_result(idx, item, result):
            if result is not None:
                state.classifications[result.iid] = result
                if result.issue_type == "bug":
                    if result.iid not in state.candidate_iids:
                        state.candidate_iids.append(result.iid)
                ph2.advance()
                state.save()

        _run_batch(
            _fn, items, max_workers=max(1, workers),
            ui=ui, label_for=lambda i: f"!{i.iid}", on_result=_on_result,
        )
        # Advance for already-classified (resume).
        for i in state.queued_issues:
            if i.iid in state.classifications:
                ph2.advance()
        n_bug = len(state.candidate_iids)
        n_skip = len(state.queued_issues) - n_bug
        ph2.complete(f"{n_bug} bug-type, {n_skip} not-applicable")
        if not ui.is_live:
            fmt.info(f"phase 2 complete. {n_bug} bug-type, {n_skip} not-applicable.")

        # Pre-populate not-applicable verdicts.
        for iid, cls in state.classifications.items():
            if cls.issue_type != "bug":
                state.final_verdicts[iid] = V_NOT_APPLICABLE
        state.phase = "extract"
        state.save()

    # ---- Phase 3: Extract claims ----
    if state.phase == "extract":
        to_extract = [i for i in state.queued_issues if i.iid in state.candidate_iids and i.iid not in state.claims]
        ph3 = _phase_open(ui, "extract", total=len(state.candidate_iids))
        if not ui.is_live:
            fmt.info(f"phase 3: extracting {len(to_extract)} claim(s)...")

        def _fn_ext(issue):
            return _phase_extract_claim(issue, state, ctx, adapter)

        def _on_ext(idx, item, result):
            if result is not None:
                state.claims[result.iid] = result
                ph3.advance()
                state.save()

        _run_batch(
            _fn_ext, to_extract, max_workers=max(1, workers),
            ui=ui, label_for=lambda i: f"!{i.iid}", on_result=_on_ext,
        )
        for iid in state.candidate_iids:
            if iid in state.claims:
                ph3.advance()
        ph3.complete(f"{len(state.claims)} claim(s)")
        if not ui.is_live:
            fmt.info(f"phase 3 complete. {len(state.claims)} claim(s) extracted.")
        state.phase = "grounding"
        state.save()

    # ---- Phase 4: Grounding ----
    if state.phase == "grounding":
        to_verify = [
            i for i in state.queued_issues
            if i.iid in state.candidate_iids and i.iid not in state.verified_claims
        ]
        ph4 = _phase_open(ui, "grounding", total=len(state.candidate_iids))
        if not ui.is_live:
            fmt.info(f"phase 4: grounding {len(to_verify)} claim(s)...")

        def _fn_ground(issue):
            claim = state.claims.get(issue.iid)
            if claim is None:
                return None
            try:
                return _grounding_one(issue, claim, state, ctx, adapter, ui=ui)
            except Exception as e:
                _ui_warning(ui, f"  [!{issue.iid}] grounding failed: {e}")
                return None

        def _on_ground(idx, item, result):
            if result is not None:
                state.verified_claims[result.iid] = result
                ph4.advance()
                state.save()

        _run_batch(
            _fn_ground, to_verify, max_workers=max(1, workers),
            ui=ui, label_for=lambda i: f"!{i.iid}", on_result=_on_ground,
        )
        for iid in state.candidate_iids:
            if iid in state.verified_claims:
                ph4.advance()
        ph4.complete(f"{len(state.verified_claims)} verified")
        if not ui.is_live:
            fmt.info(f"phase 4 complete. {len(state.verified_claims)} claim(s) verified.")
        state.phase = "adjudication"
        state.save()

    # ---- Phase 4.5: Adjudication ----
    if state.phase == "adjudication":
        items = [
            (i, vc) for i, vc in state.verified_claims.items()
            if i not in state.adjudication_results
        ]
        ph45 = _phase_open(ui, "adjudication", total=len(state.verified_claims))
        if not ui.is_live:
            fmt.info(f"phase 4.5: adjudicating {len(items)} verdict(s)...")

        def _fn_adj(item):
            return _adjudicate_one(item, state, ctx, ui=ui)

        def _on_adj(idx, item, result):
            if result is not None:
                state.adjudication_results[result.iid] = asdict(result)
                state.final_verdicts[result.iid] = result.final_verdict
                ph45.advance()
                state.save()

        _run_batch(
            _fn_adj, items, max_workers=max(1, workers),
            ui=ui, label_for=lambda t: f"!{t[0]}", on_result=_on_adj,
        )
        for iid in state.verified_claims:
            if iid in state.adjudication_results:
                ph45.advance()
        n_recalibrated = sum(
            1 for r in state.adjudication_results.values() if r.get("recalibrated")
        )
        ph45.complete(f"{len(state.adjudication_results)} adjudicated · {n_recalibrated} recalibrated")
        if not ui.is_live:
            fmt.info(
                f"phase 4.5 complete. {len(state.adjudication_results)} adjudicated, "
                f"{n_recalibrated} recalibrated."
            )
        state.phase = "writeback"
        state.save()

    # ---- Phase 5: Write-back & report ----
    if state.phase == "writeback":
        _phase_writeback(state, ctx, adapter, ui)

    # ---- Summary ----
    tally: dict[str, int] = {}
    for v in state.final_verdicts.values():
        tally[v] = tally.get(v, 0) + 1

    summary_lines = [
        "GitLab Issue Validity Review complete.",
        f"  Project: {state.project} · Tag: {state.tag}",
        f"  Issues reviewed: {len(state.queued_issues)}",
    ]
    for v in _ALL_VERDICTS + (V_DEFERRED,):
        if v in tally:
            summary_lines.append(f"  {v}: {tally[v]}")
    if state.dry_run:
        summary_lines.append("  (dry-run — no labels written to GitLab)")
    else:
        summary_lines.append(f"  Labels written: {len(state.written_labels)}")
    summary_lines.append(f"  Report: {state.artifact_dir / 'README.md'}")
    return "\n".join(summary_lines)


# ---------------------------------------------------------------------------
# Command entrypoint
# ---------------------------------------------------------------------------


def run_review_issues_command(cmd_arg: str, ctx: "InputContext") -> str:
    """Entry point for the /review-issues command. Returns summary text."""
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
    state_filter = "opened"

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
        elif parts[i] == "--state":
            if i + 1 >= len(parts):
                return "error: --state requires a value"
            i += 1
            state_filter = parts[i]
            if state_filter not in ("opened", "closed", "all"):
                return f"error: --state must be opened, closed, or all, got {state_filter!r}"
        elif parts[i] == "--workers":
            if i + 1 >= len(parts):
                return "error: --workers requires an integer"
            i += 1
            try:
                workers = int(parts[i])
            except ValueError:
                return f"error: --workers requires an integer, got {parts[i]!r}"
        elif parts[i].startswith("-"):
            return (
                f"error: unknown option {parts[i]!r}. "
                f"Known flags: --tag, --from, --to, --state, --workers, "
                f"--resume, --dry-run, --debug."
            )
        else:
            filtered.append(parts[i])
        i += 1

    if not filtered:
        return "error: a project path or ID is required (e.g. /review-issues group/sub --tag bug)"
    project = filtered[0]

    if not tag:
        return "error: --tag is required"

    if ctx.mcp_manager is None:
        return (
            "error: no MCP manager available. Configure a GitLab MCP server in "
            "swival.toml [mcp_servers] or .swival/mcp.json."
        )

    config = _load_review_issues_config(base_dir)

    if debug:
        log_dir = Path(base_dir) / ".swival" / "review-issues"
        log_dir.mkdir(parents=True, exist_ok=True)
        _debug_log_path = log_dir / "debug.jsonl"
        _debug_log("review_issues_start", args=arg)
        fmt.info(f"debug log: {_debug_log_path}")
    else:
        _debug_log_path = None

    state_dir = Path(base_dir) / ".swival" / "review-issues"

    adapter = GitLabAdapter(ctx.mcp_manager, config)

    # Resume or create new state.
    if resume:
        state = IssueReviewRunState.find_resumable(
            state_dir, project, tag, iid_from, iid_to, state_filter,
        )
        if state is None:
            return "error: no resumable run found for this project/tag/range."
        state.dry_run = dry_run
    else:
        state = IssueReviewRunState(
            run_id=uuid.uuid4().hex[:12],
            project=project,
            tag=tag,
            iid_from=iid_from,
            iid_to=iid_to,
            state_filter=state_filter,
            dry_run=dry_run,
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
                tally = {}
                for v in state.final_verdicts.values():
                    tally[v] = tally.get(v, 0) + 1
                ui.summary(
                    artifact_dir=str(state.artifact_dir),
                    written=len(state.written_labels),
                    readme_written=True,
                )
            else:
                ui.incomplete(result)
            return result
    finally:
        _debug_log_path = None
