"""Staged global code review over committed Git-tracked code.

Mirrors ``/audit``'s two-gate confirmation model, widened from security to six
general review categories: design, consistency, flaw, smell, bug, performance.
Every finding is independently confirmed (grounding + adversarial refutation)
before it is kept.

See ``REVIEW_DESIGN.md`` for the full design.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import as_completed
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .input_dispatch import InputContext

from . import fmt
from .audit import (
    AuditScope,
    _build_context_indices,
    _cached_git_show,
    _CALLEE_NONE,
    _CALLEE_PROMPT_NOTE,
    _CALLEE_SECTION_HEADER,
    _call_audit_llm,
    _coerce_focus,
    _consensus_severity,
    _demote_only,
    _dirty_worktree_warning,
    _evidence_file_paths,
    _extract_exports,
    _extract_imports,
    _finding_key,
    _gather_callee_context,
    _gather_callee_context_for_paths,
    _gather_evidence,
    _git,
    _git_show,
    _is_auditable,
    _less_severe_of,
    _load_file_contents,
    _make_isolated_loop_kwargs,
    _match_path_glob,
    _normalize_focus,
    _parse_records_with_repair,
    _phase1_source_inventory,
    PhaseSchema,
    RecordSchema,
    _resolve_scope,
    _run_batch,
    _SEVERITIES,
    _SEVERITY_RANK,
    _TransientVerifierError,
    _worktree,
    _write_audit_trace,
)
from .audit_ui import AuditUI, PhaseHandle

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REVIEW_PROVENANCE_URL = "https://swival.dev"

_LARGE_SCOPE_THRESHOLD = 500
_DEFAULT_PATCH_MAX_TURNS = 50
_DEFAULT_VERIFY_MAX_TURNS = 60
_PROMOTION_SCORE_THRESHOLD = 8
_FAN_IN_THRESHOLD = 3
_SIZE_LINE_THRESHOLD = 500

_CATEGORIES = ("design", "consistency", "flaw", "smell", "bug", "performance")
_OUT_OF_SCOPE_CATEGORY = "out-of-scope"

_DEFAULT_METRICS: dict[str, int] = {
    "parse_failures_profile": 0,
    "parse_failures_triage": 0,
    "parse_failures_finding": 0,
    "parse_failures_expansion": 0,
    "parse_failures_verdict": 0,
    "parse_failures_consolidate": 0,
    "repair_successes": 0,
    "repair_failures": 0,
    "verifier_no_verdict": 0,
    "verifier_transient_retries": 0,
    "adjudication_lens_retries": 0,
    "truncated_calls": 0,
    "empty_response_retries": 0,
    "high_none_retries": 0,
}

# ---------------------------------------------------------------------------
# Debug log
# ---------------------------------------------------------------------------

_debug_log_path: Path | None = None
_debug_log_lock = threading.Lock()
_current_ui = threading.local()


def _set_current_ui(ui: "AuditUI | None") -> None:
    _current_ui.ui = ui


def _get_current_ui() -> "AuditUI | None":
    return getattr(_current_ui, "ui", None)


def _set_current_worker_slot(slot: int | None) -> None:
    _current_ui.worker_slot = slot


def _get_current_worker_slot() -> int | None:
    return getattr(_current_ui, "worker_slot", None)


def _ui_info(ui: "AuditUI | None", msg: str) -> None:
    target = ui if ui is not None else _get_current_ui()
    if target is not None:
        target.scrollback(msg)
    else:
        fmt.info(msg)


def _ui_warning(ui: "AuditUI | None", msg: str) -> None:
    target = ui if ui is not None else _get_current_ui()
    if target is not None:
        target.warning(msg)
    else:
        fmt.warning(msg)


def _debug_log(event: str, **fields) -> None:
    if _debug_log_path is None:
        return
    entry = {"ts": time.time(), "event": event, **fields}
    line = json.dumps(entry, default=str) + "\n"
    with _debug_log_lock:
        with open(_debug_log_path, "a") as f:
            f.write(line)


# ---------------------------------------------------------------------------
# Review-surface scoring
# ---------------------------------------------------------------------------

_REVIEW_SURFACE_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\b(class|struct|interface|trait|impl|enum)\b"), 2),
    (re.compile(r"\b(def|function|func|fun|fn|sub)\b"), 1),
    (re.compile(r"\b(extends|implements|inherits|override|virtual)\b"), 2),
    (re.compile(r"\b(async|await|thread|lock|mutex|sync|channel|goroutine)\b"), 3),
    (re.compile(r"\b(try|catch|except|finally|throw|raise|panic)\b"), 2),
    (re.compile(r"\b(if|elif|else|switch|match|case|for|while)\b"), 1),
    (re.compile(r"\b(return|yield|break|continue)\b"), 1),
    (re.compile(r"\b(import|from|require|use|include)\b"), 1),
    (re.compile(r"\b(abstract|generic|template|protocol|where)\b"), 2),
    (re.compile(r"\b(TODO|FIXME|HACK|XXX|DEPRECATED)\b"), 3),
    (re.compile(r"\b(for|while|foreach|each|iter|loop|repeat)\b"), 2),
    (re.compile(r"\b(sort|sorted|map|filter|reduce|group|order)\b"), 2),
    (re.compile(r"\b(alloc|malloc|new |append|extend|insert|copy|clone)\b"), 2),
    (re.compile(r"\b(select|query|execute|cursor|fetch|join)\b"), 3),
    (re.compile(r"\b(recurs|recur|call_self)\b"), 2),
    (re.compile(r"\b(cache|memo|sleep|retry|backoff|timeout)\b"), 2),
]


def _score_review_surface(content: str) -> int:
    score = 0
    for pattern, weight in _REVIEW_SURFACE_PATTERNS:
        if pattern.search(content):
            score += weight
    return score


def _compute_fan_in(files: list[str], dependency_index: dict[str, list[str]]) -> dict[str, int]:
    """Count how many files depend on each file (inverted dependency index)."""
    fan_in: dict[str, int] = {f: 0 for f in files}
    for _f, deps in dependency_index.items():
        for dep in deps:
            fan_in[dep] = fan_in.get(dep, 0) + 1
    return fan_in


def _order_by_review_surface(
    files: list[str],
    content_cache: dict[str, str],
    dependency_index: dict[str, list[str]],
) -> tuple[list[str], dict[str, int]]:
    """Return (files ordered by descending score, score map).

    Applies two structural multipliers on top of the regex score:
    fan-in (core modules) and file size.
    """
    fan_in = _compute_fan_in(files, dependency_index)
    score_map: dict[str, int] = {}
    scored: list[tuple[int, str]] = []
    for f in files:
        content = content_cache.get(f, "")
        score = _score_review_surface(content)
        # Fan-in multiplier: +2 per importer above threshold.
        fi = fan_in.get(f, 0)
        if fi > _FAN_IN_THRESHOLD:
            score += (fi - _FAN_IN_THRESHOLD) * 2
        # Size multiplier: long files get +3.
        if content.count("\n") + 1 > _SIZE_LINE_THRESHOLD:
            score += 3
        score_map[f] = score
        scored.append((-score, f))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [f for _, f in scored], score_map


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ReviewTriageRecord:
    path: str
    priority: str  # REVIEW_HIGH | REVIEW_LOW | SKIP
    confidence: str
    categories: list[str]
    summary: str
    relevant_symbols: list[str]
    suspicious_flows: list[str]
    needs_followup: bool
    promotion_reasons: list[str] = field(default_factory=list)
    triage_failure_mode: str | None = None
    confirmation_outcome: str | None = None


@dataclass
class ReviewFinding:
    title: str
    category: str
    severity: str
    locations: list[str]
    preconditions: list[str]
    evidence: list[str]  # proof lines
    fix_outline: str
    source_file: str
    symptom: str = ""
    consequence: str = ""
    subject: str = ""


@dataclass
class ConfirmedFinding:
    finding: ReviewFinding
    grounding_reason: str
    reproducer: dict | None = None


@dataclass
class ReviewDeepReviewResult:
    path: str
    findings: list[ReviewFinding] | None = None
    error: str | None = None


@dataclass
class ReviewVerificationResult:
    finding_key: str
    confirmed_finding: ConfirmedFinding | None = None
    discarded: bool = False
    error: str | None = None
    attempts: int = 1


@dataclass
class ReviewAdjudicationResult:
    index: int
    kept: bool
    decision: str  # "keep" | "keep_with_changes" | "drop" | "error"
    finding: ReviewFinding
    original_severity: str
    final_severity: str
    reason: str
    error: str | None = None


@dataclass
class ReviewPatchResult:
    patch_text: str | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass
class ReviewRunState:
    run_id: str
    scope: AuditScope
    queued_files: list[str] = field(default_factory=list)
    reviewed_files: set[str] = field(default_factory=set)
    triage_records: dict[str, ReviewTriageRecord] = field(default_factory=dict)
    candidate_files: list[str] = field(default_factory=list)
    deep_reviewed_files: set[str] = field(default_factory=set)
    proposed_findings: list[ReviewFinding] = field(default_factory=list)
    confirmed_findings: list[ConfirmedFinding] = field(default_factory=list)
    repo_profile: dict | None = None
    import_index: dict[str, list[str]] = field(default_factory=dict)
    caller_index: dict[str, list[str]] = field(default_factory=dict)
    symbol_spans_index: dict[str, dict[str, dict]] = field(default_factory=dict)
    review_scores: dict[str, int] = field(default_factory=dict)
    artifact_dir: Path = field(default_factory=lambda: Path("review-findings"))
    state_dir: Path = field(default_factory=lambda: Path(".swival/review"))
    verification_state: dict[str, dict] = field(default_factory=dict)
    artifact_state: dict[str, dict] = field(default_factory=dict)
    phase: str = "init"
    metrics: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_METRICS))
    select_all: bool = False
    adjudication_discarded: list[dict] = field(default_factory=list)
    truncated_files: dict[str, int] = field(default_factory=dict)
    _content_cache: dict[str, str] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def dependency_index(self) -> dict[str, list[str]]:
        return self.caller_index

    def save(self) -> None:
        d = self.state_dir / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        blob = {
            "run_id": self.run_id,
            "scope": self.scope.to_dict(),
            "queued_files": self.queued_files,
            "reviewed_files": sorted(self.reviewed_files),
            "triage_records": {k: asdict(v) for k, v in self.triage_records.items()},
            "candidate_files": self.candidate_files,
            "deep_reviewed_files": sorted(self.deep_reviewed_files),
            "proposed_findings": [asdict(f) for f in self.proposed_findings],
            "confirmed_findings": [
                {
                    "finding": asdict(cf.finding),
                    "grounding_reason": cf.grounding_reason,
                    "reproducer": cf.reproducer,
                }
                for cf in self.confirmed_findings
            ],
            "repo_profile": self.repo_profile,
            "import_index": self.import_index,
            "caller_index": self.caller_index,
            "symbol_spans_index": self.symbol_spans_index,
            "review_scores": dict(self.review_scores),
            "verification_state": self.verification_state,
            "artifact_state": self.artifact_state,
            "phase": self.phase,
            "metrics": dict(self.metrics),
            "select_all": self.select_all,
            "adjudication_discarded": self.adjudication_discarded,
            "truncated_files": dict(self.truncated_files),
        }
        state_path = d / "state.json"
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=2))
        tmp.replace(state_path)

    @classmethod
    def load(cls, state_dir: Path, run_id: str) -> "ReviewRunState":
        d = state_dir / run_id / "state.json"
        blob = json.loads(d.read_text())
        scope = AuditScope.from_dict(blob["scope"])
        triage_records = {
            k: ReviewTriageRecord(**v)
            for k, v in blob.get("triage_records", {}).items()
        }
        proposed = [ReviewFinding(**f) for f in blob.get("proposed_findings", [])]
        confirmed = []
        for cf in blob.get("confirmed_findings", []):
            confirmed.append(
                ConfirmedFinding(
                    finding=ReviewFinding(**cf["finding"]),
                    grounding_reason=cf["grounding_reason"],
                    reproducer=cf.get("reproducer"),
                )
            )
        return cls(
            run_id=blob["run_id"],
            scope=scope,
            queued_files=blob["queued_files"],
            reviewed_files=set(blob.get("reviewed_files", [])),
            triage_records=triage_records,
            candidate_files=blob.get("candidate_files", []),
            deep_reviewed_files=set(blob.get("deep_reviewed_files", [])),
            proposed_findings=proposed,
            confirmed_findings=confirmed,
            repo_profile=blob.get("repo_profile"),
            import_index=blob.get("import_index", {}),
            caller_index=blob.get("caller_index", {}),
            symbol_spans_index=blob.get("symbol_spans_index", {}),
            review_scores=blob.get("review_scores", {}),
            verification_state=blob.get("verification_state", {}),
            artifact_state=blob.get("artifact_state", {}),
            state_dir=state_dir,
            phase=blob.get("phase", "init"),
            metrics={**_DEFAULT_METRICS, **blob.get("metrics", {})},
            select_all=bool(blob.get("select_all", False)),
            adjudication_discarded=blob.get("adjudication_discarded", []),
            truncated_files=blob.get("truncated_files", {}),
        )

    @classmethod
    def find_resumable(
        cls,
        state_dir: Path,
        commit: str,
        focus: list[str] | None,
        include_done: bool = False,
    ) -> "ReviewRunState | None":
        if not state_dir.exists():
            return None
        want = None if focus is None else set(_normalize_focus(focus))
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
            if blob.get("scope", {}).get("commit") != commit:
                continue
            if want is not None:
                persisted = _normalize_focus(
                    _coerce_focus(blob.get("scope", {}).get("focus"))
                )
                if set(persisted) != want:
                    continue
            if blob.get("phase") == "done" and not include_done:
                continue
            mtime = sf.stat().st_mtime
            if mtime > best_mtime:
                try:
                    best = cls.load(state_dir, blob["run_id"])
                except Exception:
                    continue
                best_mtime = mtime
        return best


def _review_finding_key(finding: ReviewFinding) -> str:
    blob = json.dumps(asdict(finding), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Schemas & prompts
# ---------------------------------------------------------------------------

_REVIEW_PROFILE_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="profile",
        required=("language", "summary"),
        repeated={
            "language": "languages",
            "framework": "frameworks",
            "entry_point": "entry_points",
            "core_module": "core_modules",
            "public_api": "public_apis",
            "module_boundary": "module_boundaries",
            "state_layer": "state_layers",
            "concurrency_surface": "concurrency_surfaces",
            "abstraction_boundary": "abstraction_boundaries",
            "hot_path": "hot_paths",
        },
    ),
    cardinality="one",
    allow_none=False,
)

_REVIEW_PROFILE_WORKED_EXAMPLE = """\
@@ profile @@
language: python
language: rust
framework: pytest
framework: uv
entry_point: swival/agent.py
core_module: swival/audit.py
core_module: swival/agent.py
public_api: swival/audit.py::run_audit_command
module_boundary: swival/audit.py
abstraction_boundary: swival/mcp_client.py
state_layer: .swival/HISTORY.md
concurrency_surface: swival/audit_ui.py
hot_path: swival/agent.py
summary: a python cli coding agent with mcp client and audit pipeline."""

_REVIEW_PROFILE_SYSTEM = f"""\
You are preparing a compact repository profile for a staged code review.

This phase does not find bugs.
Its only job is to extract reusable repository facts that improve later review.

You have no tools, no shell access, and no ability to run commands.
All the source code you need is included below. Do not request additional information.

Output format: a single `@@ profile @@` block. The keys, one per line:
- language: one language per line; repeat the line for each
- framework: one framework or build/test tool per line; repeat the line for each
- entry_point: one repo-relative path per line; repeat the line for each
- core_module: one repo-relative path per line (high fan-in files); repeat for each
- public_api: one path::symbol per line; repeat for each
- module_boundary: one repo-relative path per line; repeat for each
- state_layer: one short string per line; repeat for each
- concurrency_surface: one short string per line; repeat for each
- abstraction_boundary: one short string per line; repeat for each
- hot_path: one short string per line; repeat for each
- summary: one line, under 120 words

Use exactly the keys shown. Do not quote, escape, or wrap values. Each value
runs to the end of its line. Omit a repeated key entirely when there is
nothing to add for it; do not emit empty values. At least one `language` line
and a non-empty `summary` are required.

Worked example:

{_REVIEW_PROFILE_WORKED_EXAMPLE}

End of example. Now produce the real profile for the repository below.

Rules:
- Use only the provided committed repository evidence.
- Do not speculate.
- Keep every field short and reusable in later prompts."""

# ---- Phase 2: Triage ----

_REVIEW_TRIAGE_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="triage",
        required=("priority", "confidence", "summary"),
        enums={
            "priority": ("REVIEW_HIGH", "REVIEW_LOW", "SKIP"),
            "confidence": ("high", "medium", "low"),
        },
        booleans=("needs_followup",),
        repeated={
            "category": "categories",
            "relevant_symbol": "relevant_symbols",
            "suspicious_flow": "suspicious_flows",
        },
    ),
    cardinality="one",
    allow_none=False,
)

_REVIEW_TRIAGE_WORKED_EXAMPLE = """\
@@ triage @@
priority: REVIEW_LOW
confidence: medium
summary: long method with deep nesting and duplicated validation logic
category: smell
category: consistency
relevant_symbol: process_request
suspicious_flow: validation duplicated between parse and process
needs_followup: false"""

_REVIEW_TRIAGE_SYSTEM = f"""\
You are performing phase 2 code-review triage for one committed file with its \
direct local context.

Goal:
- decide whether this file deserves deep review
- optimize for precision over recall
- avoid false positives and confirmation bias

Allowed priority labels:
- REVIEW_HIGH
- REVIEW_LOW
- SKIP

Scope is **general code review**, across six categories: `design`, \
`consistency`, `flaw`, `smell`, `bug`, `performance` (definitions provided). A \
finding is in scope only when you can name, from the evidence:
1. **Subject** — the concrete code element (function, module, boundary, block).
2. **Symptom** — the concrete thing wrong with it, observable in the code.
3. **Consequence** — the concrete maintenance, correctness, or runtime cost it \
imposes.

If you cannot name all three, SKIP.

Out of scope and must SKIP:
- style preferences and bikeshedding with no concrete consequence
- "I would have structured this differently" without a concrete cost
- missing tests, missing docs, missing comments (unless the absence breaks a \
contract)
- generic hardening / "this should also validate X" / defense-in-depth
- hypothetical bugs with no concrete trigger path in committed code
- micro-optimizations and premature optimization with no realistic scaling cost

Review lenses to consider (hints, not sufficient reasons to escalate):
- responsibility_violation · leaky_abstraction · wrong_indirection
- circular_or_excessive_coupling · god_object · missing_module
- divergent_patterns · naming_drift · convention_break
- wrong_algorithm · broken_invariant · unhandled_edge_case · latent_race
- dead_code · duplication · long_method · deep_nesting · magic_number
- complex_conditional · premature_abstraction · unclear_control_flow
- incorrect_output · unhandled_exception · resource_leak · data_corruption
- n_plus_one_query · redundant_allocation · wrong_complexity · unbounded_growth
- hot_path_redundant_work · repeated_decode · quadratic_blowup

You have no tools, no shell access, and no ability to run commands.
All the source code you need is included below. Do not request additional information.
{_CALLEE_PROMPT_NOTE}

Output format: a single `@@ triage @@` block with these keys, one per line:
- priority: REVIEW_HIGH | REVIEW_LOW | SKIP
- confidence: high | medium | low
- summary: one-line summary of why this file does or does not deserve review
- category: one category per line; repeat for each (omit if SKIP)
- relevant_symbol: one symbol per line; repeat for each (omit if SKIP)
- suspicious_flow: one flow per line; repeat for each (omit if SKIP)
- needs_followup: true | false

Use exactly the keys shown. Do not quote, escape, or wrap values.

Worked example:

{_REVIEW_TRIAGE_WORKED_EXAMPLE}

End of example. Now produce the real triage for the file below.

Rules:
- Use only the provided committed repository evidence.
- Prefer SKIP when uncertain; the deep-review phase is expensive.
- Set needs_followup to true only when you see something suspicious but cannot \
confirm it warrants escalation from this file alone."""

_REVIEW_TRIAGE_CONFIRM_SYSTEM = """\
You are re-checking one file that was SKIPped in triage but touches a core \
module or entry point. Is there genuinely nothing in this file worth a deep \
review pass? Answer with a single `@@ triage @@` block (same format as the \
original triage). If you find something, set priority to REVIEW_LOW. If not, \
confirm SKIP."""

# ---- Phase 3a: Inventory ----

_REVIEW3A_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="finding",
        required=("title", "category", "severity", "location", "symptom", "claim"),
        enums={"category": _CATEGORIES, "severity": _SEVERITIES},
    ),
    cardinality="zero_or_more",
    allow_none=True,
)

_REVIEW3A_WORKED_EXAMPLE = """\
@@ finding @@
title: duplicated validation logic between parse and process
category: smell
severity: medium
location: src/handler.py:142
symptom: the same field-validation logic is copy-pasted in parse_input and process_request
claim: validation must be kept in sync across two functions, risking drift"""

_REVIEW3A_SYSTEM = f"""\
You are performing phase 3 deep code review for one candidate file.

Review only the committed repository evidence provided.
Reject any claim that is not fully proven.

You have no tools, no shell access, and no ability to run commands.
All the source code you need is included below. Do not request additional information.
{_CALLEE_PROMPT_NOTE}

Output format: either one or more `@@ finding @@` blocks (described below), OR \
the single line `@@ none @@` if no in-scope finding exists. No other output \
shape is valid.

Each `@@ finding @@` block has these keys, one per line:
- title: short title
- category: design | consistency | flaw | smell | bug | performance
- severity: low | medium | high | critical
- location: path:line
- symptom: the concrete thing wrong, observable in the code, under 25 words
- claim: one-line statement of the consequence, under 20 words

Use exactly the keys shown. Do not quote, escape, or wrap values. Each value
runs to the end of its line.

Severity (anchor to the project's realistic maintenance reality, not worst case):
- critical: a bug that breaks core behavior or causes data loss; a design flaw \
that makes the system unsound; a performance issue causing service exhaustion.
- high: a bug on a real code path; a design/consistency issue that will cause \
repeated bugs or measurably blocks maintainability; a performance issue on a \
real hot path with a concrete scaling cliff.
- medium: a smell/flaw with real maintenance cost; a consistency break that will \
confuse the next contributor; a performance issue with a real but bounded cost.
- low: minor smell or nitpick; a performance micro-issue with marginal cost. \
Default-drop unless the consequence is concrete.

Worked example:

{_REVIEW3A_WORKED_EXAMPLE}

End of example. Now produce the real findings for the file below.

Rules:
- Report zero findings rather than a speculative finding.
- Every finding must be provable from the provided repository evidence.
- At most 3 findings per file.
- Each claim under 20 words.
- Prefer the narrowest issue that the evidence directly proves.
- Use exact path:line citations.
- Do not include best practices, missing tests, or generic hardening advice.

Scope gate — apply to every candidate finding before emitting it:

A finding is in scope only if you can answer all three of these from the \
evidence, in one breath:
  1. Subject: what is the concrete code element?
  2. Symptom: what is concretely wrong with it, observable in the code?
  3. Consequence: what concrete maintenance, correctness, or runtime cost does \
it impose?

Category-specific tests:
- design: is the abstraction/module/boundary wrong, with a concrete cost to \
callers or maintainers?
- consistency: are there divergent structures for the same problem, with a \
concrete confusion cost?
- flaw: is there a concrete logic error or invariant violation (not hypothetical)?
- smell: is there a real maintenance cost (not just "ugly")?
- bug: is there a concrete runtime defect reproducible from committed code?
- performance: is there a concrete runtime cost under realistic input (not a \
micro-optimization)?

If the answer to any of the three is missing, vague, or only achievable in a \
hypothetical scenario, omit the finding. If no finding survives the scope gate, \
emit exactly the single line below and stop:

@@ none @@"""

# ---- Phase 3b: Expansion ----

_REVIEW3B_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="expansion",
        required=(
            "category", "subject", "symptom", "consequence",
            "evidence", "fix_outline",
        ),
        enums={"category": _CATEGORIES},
        multiline=("evidence",),
    ),
    cardinality="one",
    allow_none=False,
)

_REVIEW3B_WORKED_EXAMPLE = """\
@@ expansion @@
category: smell
subject: parse_input and process_request in src/handler.py
symptom: field-validation logic is copy-pasted across both functions (lines 142-160 and 210-228)
consequence: any validation change must be applied in two places, risking drift and bugs
evidence:
  parse_input at src/handler.py:142 validates email, name, and age fields.
  process_request at src/handler.py:210 repeats the same checks inline.
  The two blocks are textually identical except for variable names.
  A change to one without the other would silently diverge.
fix_outline: extract a shared validate_fields helper and call it from both"""

_REVIEW3B_SYSTEM = f"""\
You are expanding one code-review finding with proof details.

You have no tools, no shell access, and no ability to run commands.
All the source code you need is included below. Do not request additional information.
{_CALLEE_PROMPT_NOTE}

Output format: exactly one `@@ expansion @@` block. The block has these keys, \
one per line:
- category: design | consistency | flaw | smell | bug | performance
- subject: the concrete code element, under 20 words
- symptom: the concrete thing wrong, observable in the code, under 25 words
- consequence: the concrete cost, under 25 words
- evidence: propagation path and exact line citations - under 100 words total. \
The evidence value may span multiple lines: any line that begins with two or \
more spaces continues the evidence value.
- fix_outline: smallest correct fix, under 20 words

Use exactly the keys shown. Do not quote, escape, or wrap values. Every value
runs to the end of its line, and only `evidence:` may continue on indented lines.

Worked example:

{_REVIEW3B_WORKED_EXAMPLE}

End of example. Now produce the real expansion for the finding below.

Rules:
- Use only the provided repository evidence.
- Prefer the narrowest issue that the evidence directly proves.
- Do not speculate beyond what the code proves.
- If the candidate is real but out of review scope (style preference, \
hypothetical, no consequence), emit:
  category: out-of-scope
  subject: out-of-scope
  symptom: out-of-scope
  consequence: out-of-scope
  evidence: out-of-scope because subject, symptom, or consequence is not proven
  fix_outline: no fix needed"""

# ---- Phase 4: Grounding ----

_REVIEW_VERIFY_BUG_SYSTEM = """\
You are verifying one proposed bug finding using the committed source in an \
isolated worktree. Determine whether the finding describes a real defect that \
manifests in practice. Treat the finding as a hypothesis, not ground truth.

Rules:
- You may inspect the code, or compile/run small proof-of-concept code if that \
helps.
- Use the committed source in the worktree only.
- A proof counts if you can identify the trigger path, the failing operation or \
violated invariant, and the practical incorrect/crashing/corrupting outcome \
from the code, or demonstrate equivalent runtime evidence.
- Reject as REFUTED when the code does not support a practical trigger path, or \
when an existing guard already prevents the defect (reject defense-in-depth).

End your final response with exactly one token on its own line:
CONFIRMED
REFUTED"""

_REVIEW_VERIFY_PERF_SYSTEM = """\
You are verifying one proposed performance finding using the committed source \
in an isolated worktree. Determine whether the finding describes a real \
inefficiency with a concrete runtime cost under realistic input. Treat the \
finding as a hypothesis, not ground truth.

Rules:
- You may inspect the code, reason about complexity, and run small timing or \
allocation-count proof-of-concepts against realistic-sized input.
- Use the committed source in the worktree only.
- A proof counts if you can identify the inefficient construct, the input shape \
or size that triggers it, and the concrete cost (latency, memory, scaling cliff) \
— either by source-based complexity reasoning grounded in the actual code path, \
or by a small measurement against realistic-sized input.

Reject as REFUTED when:
- the cost only appears at input sizes no realistic caller produces, or
- an existing bound, cache, or early-exit already caps the cost, or
- the finding is a micro-optimization or premature optimization with no \
realistic scaling cost (reject bike-shedding over constant factors).

End your final response with exactly one token on its own line:
CONFIRMED
REFUTED"""

_REVIEW_VERIFY_SUBJECTIVE_SYSTEM = """\
You are verifying one proposed code-review finding (category: {category}) using \
the committed source. Treat the finding as a hypothesis, not ground truth.

Confirm the finding only if, from the committed evidence, all three hold:
1. **Subject is real**: the cited location exists and the described element is \
as claimed.
2. **Symptom is real**: the code at the cited location actually exhibits the \
described problem (read it; do not trust the proposal's characterization).
3. **Consequence is real**: the claimed maintenance/correctness cost genuinely \
follows from the symptom, under today's code. "Could be argued", "might confuse \
someone", and "I prefer X" do not qualify.

For `consistency` findings, independently locate the divergent structure the \
finding references; if you cannot find it, REFUTE.
For `flaw` findings, identify the concrete edge case or invariant violation; \
hypothetical "could break if X" without a code-grounded path is REFUTED.
For `smell` findings, confirm the smell is non-trivial: the maintenance cost \
must be concrete (e.g. duplication that must be kept in sync, a method too long \
to hold in working memory), not a cosmetic preference.
For `design` findings, confirm the abstraction or boundary is genuinely wrong \
and the cost to callers/maintainers is concrete, not a style preference.

Reject defense-in-depth and "would be more robust if" arguments as REFUTED.

End with exactly one token on its own line: CONFIRMED or REFUTED."""

# ---- Phase 4.5: Adjudication ----

_REVIEW_LENSES = (
    "Lens: grounding & accuracy. Re-read the cited code yourself. Is the symptom "
    "actually present as described? Is the consequence real under today's code, "
    "or is it speculative? If the finding mischaracterizes the code, it is a "
    "false_positive.",

    "Lens: significance. Does this actually matter? A grounded but trivial issue "
    "(cosmetic style, a nitpick, a smell with no real maintenance cost, a "
    "micro-optimization with no realistic scaling cost) is a false_positive. We "
    "would rather drop a real-but-trivial issue than ship noise. Tie-break toward "
    "false_positive for low-severity findings.",

    "Lens: category-fit & severity. Is the finding labeled with the right category, "
    "and is the severity justified for the realistic consequence? Recalibrate to "
    "the realistic maintenance cost and treat overstated severity as false_positive. "
    "A `smell` dressed up as `design`, or a `low` dressed up as `high`, is a "
    "false_positive even if the underlying observation is true.",
)

_REVIEW_VERDICT_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="verdict",
        required=("verdict", "category_fit", "severity", "reason"),
        enums={
            "verdict": ("real", "false_positive"),
            "category_fit": ("yes", "no"),
            "severity": _SEVERITIES,
        },
    ),
    cardinality="one",
    allow_none=False,
)

_REVIEW_VERDICT_WORKED_EXAMPLE = """\
@@ verdict @@
verdict: real
category_fit: yes
severity: medium
reason: duplicated validation is genuinely present and must be kept in sync"""

_REVIEW_VERDICT_SYSTEM = """\
You are adjudicating one already-confirmed code-review finding. A prior phase \
checked it against the evidence and accepted it. Your job is the opposite: try \
to REFUTE this finding. Default to false_positive unless the evidence forces \
otherwise. We would rather drop a real issue than ship noise.

{lens}

A finding is real only if, under today's committed code, all hold:
- the symptom is actually present at the cited location
- the consequence is concrete and real (not "could", "might", "I prefer")
- the category and severity are justified for the realistic consequence

Set category_fit to `no` when the finding is mislabeled (e.g. a style nitpick \
filed as `design`, a non-reproducible concern filed as `bug`, or a constant-\
factor micro-optimization filed as `performance` with no scaling cost).

Recalibrate severity to the realistic maintenance/runtime cost; tie-break \
downward.

Output exactly one `@@ verdict @@` block with these keys, one per line:
- verdict: real | false_positive
- category_fit: yes | no
- severity: low | medium | high | critical
- reason: one line under 25 words

Use exactly the keys shown. Do not quote, escape, or wrap values."""

_REVIEW_CONSOLIDATE_SYSTEM = """\
You are finalizing one code-review finding that a refutation panel voted to \
keep. Produce the corrected, evidence-grounded version that the report and \
patch will be generated from.

You have no tools and cannot run commands. Use only the evidence and panel \
verdicts below.

Rules:
- Severity may only be lowered or left unchanged, never raised above the \
proposed severity. Anchor it to the realistic consequence and tie-break \
downward.
- Keep the title and category as narrow as the evidence proves.
- Do not invent new issues. If the panel narrowed the realistic impact, \
reflect that.

Output exactly one `@@ ruling @@` block with these keys, one per line:
- title: short, specific finding title
- category: design | consistency | flaw | smell | bug | performance
- severity: low | medium | high | critical
- precondition: one minimal precondition per line, repeat for each (omit if none)
- fix_outline: smallest correct fix, under 20 words

Use exactly the keys shown. Do not quote, escape, or wrap values."""

_REVIEW_CONSOLIDATE_WORKED_EXAMPLE = """\
@@ ruling @@
title: duplicated validation logic between parse and process
category: smell
severity: medium
precondition: both functions remain in the same module
fix_outline: extract a shared validate_fields helper"""

_REVIEW_RULING_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="ruling",
        required=("title", "category", "severity"),
        optional=("fix_outline",),
        enums={"category": _CATEGORIES, "severity": _SEVERITIES},
        repeated={"precondition": "preconditions"},
    ),
    cardinality="one",
    allow_none=False,
)

_REVIEW_REPORT_TEMPLATE = """\
You are writing the final markdown report for one confirmed code-review finding.

Use exactly this structure:
- # <short finding title>
- ## Classification
- ## Affected Locations
- ## Summary
- ## Preconditions
- ## Evidence
- ## Why This Is A Real Issue
- ## Fix Requirement
- ## Patch Rationale
- ## Residual Risk
- ## Patch

You have no tools, no shell access, and no ability to run commands.
All the information you need is included below. Do not request additional information.
Your entire response must be a single markdown document and nothing else.

Rules:
- Confidence must be certain.
- Be terse, factual, and evidence-driven.
- Residual Risk must be `None` unless a narrow evidence-based concern remains."""


# ---------------------------------------------------------------------------
# Verdict-line parsing
# ---------------------------------------------------------------------------

_CONFIRMED_KEYWORD = "CONFIRMED"
_REFUTED_KEYWORD = "REFUTED"


def _parse_review_verdict_line(answer: str) -> bool | None:
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
# Phase 1: repo profile
# ---------------------------------------------------------------------------


def _phase1_review_profile(state: ReviewRunState, ctx: "InputContext") -> dict:
    evidence_parts = []
    inventory = _phase1_source_inventory(state.scope.tracked_files)
    if inventory:
        evidence_parts.append(inventory)

    manifest_names = {
        "package.json", "Cargo.toml", "go.mod", "pyproject.toml",
        "setup.py", "setup.cfg", "CMakeLists.txt", "Makefile",
        "build.gradle", "pom.xml", "Gemfile", "composer.json",
    }
    for f in state.scope.tracked_files:
        if Path(f).name in manifest_names:
            try:
                content = _cached_git_show(f, state, ctx.base_dir)
                evidence_parts.append(f"--- {f} ---\n{content[:2000]}")
            except RuntimeError:
                pass

    evidence = "\n\n".join(evidence_parts)
    messages = [
        {"role": "system", "content": _REVIEW_PROFILE_SYSTEM},
        {"role": "user", "content": evidence},
    ]
    raw = _call_audit_llm(
        ctx, messages,
        trace_task="review: phase 1 profile",
        metrics=state.metrics,
    )
    records = _parse_records_with_repair(
        ctx, raw,
        schema=_REVIEW_PROFILE_SCHEMA,
        worked_example=_REVIEW_PROFILE_WORKED_EXAMPLE,
        metrics=state.metrics,
    )
    return records[0] if records else {}


def _repo_profile_json(state: ReviewRunState) -> str:
    if state.repo_profile is None:
        return "{}"
    return json.dumps(state.repo_profile, indent=2)


# ---------------------------------------------------------------------------
# Phase 2: Triage
# ---------------------------------------------------------------------------


def _review_triage_record_from_parsed(path: str, parsed: dict) -> ReviewTriageRecord:
    return ReviewTriageRecord(
        path=path,
        priority=parsed.get("priority", "SKIP"),
        confidence=parsed.get("confidence", "low"),
        categories=parsed.get("categories", []),
        summary=parsed.get("summary", ""),
        relevant_symbols=parsed.get("relevant_symbols", []),
        suspicious_flows=parsed.get("suspicious_flows", []),
        needs_followup=bool(parsed.get("needs_followup", False)),
    )


def _review_triage_one(
    path: str, state: ReviewRunState, ctx: "InputContext"
) -> ReviewTriageRecord:
    try:
        content = _cached_git_show(path, state, ctx.base_dir)
    except RuntimeError:
        return ReviewTriageRecord(
            path=path, priority="SKIP", confidence="low",
            categories=[], summary="file unreadable",
            relevant_symbols=[], suspicious_flows=[],
            needs_followup=False, triage_failure_mode="unreadable",
        )

    callee_context = _gather_callee_context(path, content, state, ctx)
    profile_json = _repo_profile_json(state)
    suffix = (
        f"Primary file: {path}\n\n"
        f"Repository profile:\n{profile_json}\n\n"
        f"Committed evidence bundle:\n{content}\n\n"
        f"{_CALLEE_SECTION_HEADER}\n{callee_context}"
    )
    messages = [
        {"role": "system", "content": _REVIEW_TRIAGE_SYSTEM},
        {"role": "user", "content": suffix},
    ]
    truncation_out: list[dict] = []
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review: phase 2 triage {path}",
        metrics=state.metrics,
        truncation_out=truncation_out,
    )
    if truncation_out:
        state.truncated_files[path] = state.truncated_files.get(path, 0) + 1
    try:
        records = _parse_records_with_repair(
            ctx, raw,
            schema=_REVIEW_TRIAGE_SCHEMA,
            worked_example=_REVIEW_TRIAGE_WORKED_EXAMPLE,
            metrics=state.metrics,
        )
    except ValueError as e:
        return ReviewTriageRecord(
            path=path, priority="SKIP", confidence="low",
            categories=[], summary=f"triage parse failed: {e}",
            relevant_symbols=[], suspicious_flows=[],
            needs_followup=False, triage_failure_mode=str(e),
        )
    if not records:
        return ReviewTriageRecord(
            path=path, priority="SKIP", confidence="low",
            categories=[], summary="triage returned no records",
            relevant_symbols=[], suspicious_flows=[],
            needs_followup=False, triage_failure_mode="empty",
        )
    return _review_triage_record_from_parsed(path, records[0])


def _review_confirm_one(
    path: str, state: ReviewRunState, ctx: "InputContext"
) -> ReviewTriageRecord:
    """Confirmation pass on a low-confidence SKIP touching a core module."""
    try:
        content = _cached_git_show(path, state, ctx.base_dir)
    except RuntimeError:
        return _review_triage_one(path, state, ctx)

    callee_context = _gather_callee_context(path, content, state, ctx)
    suffix = (
        f"Primary file: {path}\n\n"
        f"Repository profile:\n{_repo_profile_json(state)}\n\n"
        f"Committed evidence bundle:\n{content}\n\n"
        f"{_CALLEE_SECTION_HEADER}\n{callee_context}"
    )
    messages = [
        {"role": "system", "content": _REVIEW_TRIAGE_CONFIRM_SYSTEM + "\n\n" + _REVIEW_TRIAGE_SYSTEM},
        {"role": "user", "content": suffix},
    ]
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review: phase 2 confirm {path}",
        metrics=state.metrics,
    )
    try:
        records = _parse_records_with_repair(
            ctx, raw,
            schema=_REVIEW_TRIAGE_SCHEMA,
            worked_example=_REVIEW_TRIAGE_WORKED_EXAMPLE,
            metrics=state.metrics,
        )
    except ValueError:
        return _review_triage_one(path, state, ctx)
    if not records:
        return _review_triage_one(path, state, ctx)
    rec = _review_triage_record_from_parsed(path, records[0])
    if rec.priority != "SKIP":
        rec.confirmation_outcome = "promoted"
    return rec


# ---------------------------------------------------------------------------
# Promotion rules
# ---------------------------------------------------------------------------


def _core_module_paths(state: ReviewRunState) -> set[str]:
    profile = state.repo_profile or {}
    modules = profile.get("core_modules", []) or []
    mandatory = set(state.scope.mandatory_files)
    return {m for m in modules if isinstance(m, str) and m in mandatory}


def _entry_point_paths(state: ReviewRunState) -> list[str]:
    profile = state.repo_profile or {}
    entries = profile.get("entry_points", []) or []
    mandatory = set(state.scope.mandatory_files)
    return [e for e in entries if isinstance(e, str) and e in mandatory]


def _compute_review_promotions(
    state: ReviewRunState, force_review_matches: dict[str, str]
) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}

    def _add(p: str, msg: str) -> None:
        reasons.setdefault(p, []).append(msg)

    for p in state.queued_files:
        score = state.review_scores.get(p, 0)
        if score >= _PROMOTION_SCORE_THRESHOLD:
            _add(p, f"review-surface score {score}")

    entries = _entry_point_paths(state)
    for p in entries:
        _add(p, "phase 1 entry point")

    for p in _core_module_paths(state):
        _add(p, "phase 1 core module")

    for entry in entries:
        for dep in state.dependency_index.get(entry, []):
            if state.review_scores.get(dep, 0) > 0:
                _add(dep, f"reached from entry point {entry}")

    for p, rec in state.triage_records.items():
        if rec.needs_followup:
            _add(p, "triage needs_followup")

    for p, rec in state.triage_records.items():
        if rec.triage_failure_mode is not None:
            _add(p, f"triage infrastructure failure: {rec.triage_failure_mode}")

    for p, source in force_review_matches.items():
        if source == "focus":
            _add(p, "named explicitly in /review focus")
        else:
            _add(p, f"forced via swival.toml ({source})")

    return reasons


def _apply_review_promotions(
    state: ReviewRunState, force_review_matches: dict[str, str]
) -> dict[str, list[str]]:
    for p in state.queued_files:
        if p not in state.triage_records:
            state.triage_records[p] = ReviewTriageRecord(
                path=p, priority="SKIP", confidence="low",
                categories=[], summary="triage record missing after retries",
                relevant_symbols=[], suspicious_flows=[],
                needs_followup=False, triage_failure_mode="missing",
            )
            state.reviewed_files.add(p)

    promotions = _compute_review_promotions(state, force_review_matches)
    for path, why in promotions.items():
        rec = state.triage_records.get(path)
        if rec is None:
            continue
        for r in why:
            if r not in rec.promotion_reasons:
                rec.promotion_reasons.append(r)
        if rec.priority == "SKIP":
            rec.priority = "REVIEW_LOW"
    return promotions


# ---------------------------------------------------------------------------
# Phase 3: Deep review
# ---------------------------------------------------------------------------


def _review3a_inventory(
    path: str, state: ReviewRunState, ctx: "InputContext",
    content: str, callee_context: str | None = None,
) -> list[dict]:
    triage = state.triage_records.get(path)
    categories = ", ".join(triage.categories) if triage else "all"
    profile_json = _repo_profile_json(state)
    triage_json = json.dumps(asdict(triage), indent=2) if triage else "{}"
    if callee_context is None:
        callee_context = _gather_callee_context(path, content, state, ctx)

    suffix = (
        f"Primary file: {path}\n\n"
        f"Focus categories (triage hints, not limits): {categories}\n\n"
        f"Repository profile:\n{profile_json}\n\n"
        f"Phase 2 triage result:\n{triage_json}\n\n"
        f"Committed evidence bundle:\n{content}\n\n"
        f"{_CALLEE_SECTION_HEADER}\n{callee_context}"
    )
    messages = [
        {"role": "system", "content": _REVIEW3A_SYSTEM},
        {"role": "user", "content": suffix},
    ]
    truncation_out: list[dict] = []
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review: phase 3a inventory {path}",
        metrics=state.metrics,
        truncation_out=truncation_out,
    )
    if truncation_out:
        state.truncated_files[path] = state.truncated_files.get(path, 0) + 1
    return _parse_records_with_repair(
        ctx, raw,
        schema=_REVIEW3A_SCHEMA,
        worked_example=_REVIEW3A_WORKED_EXAMPLE,
        metrics=state.metrics,
    )


def _review3b_expand_one(
    item: tuple[dict, str, str, ReviewRunState, "InputContext"],
    callee_context: str | None = None,
) -> dict | None:
    finding_stub, path, content, state, ctx = item
    if callee_context is None:
        callee_context = _gather_callee_context(path, content, state, ctx)
    suffix = (
        f"Finding to expand:\n"
        f"  Title: {finding_stub.get('title', '')}\n"
        f"  Category: {finding_stub.get('category', '')}\n"
        f"  Severity: {finding_stub.get('severity', '')}\n"
        f"  Location: {finding_stub.get('location', '')}\n"
        f"  Symptom: {finding_stub.get('symptom', '')}\n"
        f"  Claim: {finding_stub.get('claim', '')}\n\n"
        f"Committed evidence for {path}:\n{content}\n\n"
        f"{_CALLEE_SECTION_HEADER}\n{callee_context}"
    )
    messages = [
        {"role": "system", "content": _REVIEW3B_SYSTEM},
        {"role": "user", "content": suffix},
    ]
    truncation_out: list[dict] = []
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review: phase 3b expand {path}",
        metrics=state.metrics,
        truncation_out=truncation_out,
    )
    if truncation_out:
        state.truncated_files[path] = state.truncated_files.get(path, 0) + 1
    try:
        records = _parse_records_with_repair(
            ctx, raw,
            schema=_REVIEW3B_SCHEMA,
            worked_example=_REVIEW3B_WORKED_EXAMPLE,
            metrics=state.metrics,
        )
    except ValueError:
        return None
    return records[0] if records else None


def _is_out_of_scope_expansion(expansion: dict) -> bool:
    return expansion.get("category") == _OUT_OF_SCOPE_CATEGORY


def _below_floor(category: str, severity: str) -> bool:
    """True when a bug/performance finding is below the medium floor."""
    if category not in ("bug", "performance"):
        return False
    return _SEVERITY_RANK.get((severity or "").lower(), 99) > _SEVERITY_RANK["medium"]


def _canonicalize_review_finding(
    inventory_item: dict, expansion: dict, source_file: str
) -> ReviewFinding:
    severity = (inventory_item.get("severity") or "low").lower()
    if severity not in _SEVERITIES:
        severity = "low"
    location = inventory_item.get("location", source_file)
    return ReviewFinding(
        title=inventory_item.get("title", "untitled"),
        category=expansion.get("category", inventory_item.get("category", "smell")),
        severity=severity,
        locations=[location],
        preconditions=[],
        evidence=[
            f"subject: {expansion.get('subject', '')}",
            f"symptom: {expansion.get('symptom', '')}",
            f"consequence: {expansion.get('consequence', '')}",
            expansion.get("evidence", ""),
        ],
        fix_outline=expansion.get("fix_outline", ""),
        source_file=source_file,
        symptom=expansion.get("symptom", inventory_item.get("symptom", "")),
        consequence=expansion.get("consequence", inventory_item.get("claim", "")),
        subject=expansion.get("subject", ""),
    )


def _review_deep_review(
    path: str, state: ReviewRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> list[ReviewFinding]:
    try:
        content = _cached_git_show(path, state, ctx.base_dir)
    except RuntimeError:
        return []

    callee_context = _gather_callee_context(path, content, state, ctx)
    inventory = _review3a_inventory(path, state, ctx, content, callee_context=callee_context)
    if not inventory:
        triage = state.triage_records.get(path)
        if triage is not None and triage.priority == "REVIEW_HIGH":
            state.metrics["high_none_retries"] += 1
            inventory = _review3a_inventory(path, state, ctx, content, callee_context=callee_context)
    if not inventory:
        return []

    expansion_items = [(stub, path, content, state, ctx) for stub in inventory]
    expansions = []
    for item in expansion_items:
        try:
            expansions.append(_review3b_expand_one(item, callee_context=callee_context))
        except Exception as e:
            _ui_warning(ui, f"expansion failed for {path}: {e}")
            expansions.append(None)

    findings = []
    failed = 0
    for stub, expansion in zip(inventory, expansions):
        if expansion is None:
            failed += 1
            continue
        if _is_out_of_scope_expansion(expansion):
            continue
        if _below_floor(
            expansion.get("category", ""),
            stub.get("severity", "low"),
        ):
            continue
        findings.append(_canonicalize_review_finding(stub, expansion, path))

    if failed > 0 and not findings:
        raise ValueError(
            f"all {failed} expansion(s) failed for {path} "
            f"({len(inventory)} inventory finding(s))"
        )
    return findings


def _deep_review_one(
    path: str, state: ReviewRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> ReviewDeepReviewResult:
    try:
        findings = _review_deep_review(path, state, ctx, ui=ui)
        return ReviewDeepReviewResult(path=path, findings=findings)
    except (ValueError, RuntimeError) as e:
        if isinstance(e, ValueError):
            state.metrics["analytical_retries"] = state.metrics.get("analytical_retries", 0) + 1
        _ui_info(ui, f"  retrying deep review for {path} after error: {e}")
        try:
            findings = _review_deep_review(path, state, ctx, ui=ui)
            return ReviewDeepReviewResult(path=path, findings=findings)
        except (ValueError, RuntimeError) as e2:
            return ReviewDeepReviewResult(path=path, error=str(e2))


# ---------------------------------------------------------------------------
# Phase 4: Grounding
# ---------------------------------------------------------------------------


def _verify_bug_or_perf(
    finding: ReviewFinding, state: ReviewRunState, ctx: "InputContext",
    work_dir: Path, ui: "AuditUI | None" = None,
) -> tuple[bool, str]:
    """Verify a bug or performance finding in an isolated worktree."""
    from .agent import run_agent_loop

    is_perf = finding.category == "performance"
    system = _REVIEW_VERIFY_PERF_SYSTEM if is_perf else _REVIEW_VERIFY_BUG_SYSTEM
    label = "performance" if is_perf else "bug"

    finding_json = json.dumps(asdict(finding), indent=2)
    locs = ", ".join(finding.locations) if finding.locations else finding.source_file
    _ui_info(ui, f"    verifier [{locs}]: verifying {label} finding")

    with _worktree(ctx.base_dir, work_dir):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Proposed finding:\n{finding_json}"},
        ]
        kw = _make_isolated_loop_kwargs(ctx, work_dir, max_turns=_DEFAULT_VERIFY_MAX_TURNS)

        target_ui = ui if ui is not None else _get_current_ui()
        worker_slot = _get_current_worker_slot()
        if target_ui is not None and worker_slot is not None:
            from .a2a_types import EVENT_STATUS_UPDATE

            def _on_event(kind: str, data: dict) -> None:
                if kind != EVENT_STATUS_UPDATE:
                    return
                turn = data.get("turn")
                if not isinstance(turn, int):
                    return
                max_turns = data.get("max_turns")
                target_ui.worker_turn(
                    worker_slot, turn,
                    max_turns if isinstance(max_turns, int) else 0,
                )
            kw["event_callback"] = _on_event

        try:
            answer, exhausted = run_agent_loop(messages, ctx.tools, **kw)
        except (ConnectionError, TimeoutError, OSError) as e:
            raise _TransientVerifierError(str(e)) from e
        finally:
            _write_audit_trace(ctx, messages, task=f"review: verify {label} {finding.title}")

        answer = answer or ""
        verdict = _parse_review_verdict_line(answer)
        if verdict is None:
            state.metrics["verifier_no_verdict"] += 1
            raise _TransientVerifierError(
                "verifier produced no verdict token"
                + (" (turn budget exhausted)" if exhausted else "")
            )
        if verdict:
            _ui_info(ui, f"    verifier [{locs}]: CONFIRMED — {finding.title}")
        else:
            _ui_info(ui, f"    verifier [{locs}]: REFUTED — {finding.title}")
        return verdict, answer[-1000:]


def _verify_subjective(
    finding: ReviewFinding, state: ReviewRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> tuple[bool, str]:
    """Verify a subjective-category finding via evidence-grounded LLM check."""
    evidence, n_files = _gather_evidence(finding, state, ctx)
    locs = ", ".join(finding.locations) if finding.locations else finding.source_file
    _ui_info(ui, f"    verifier [{locs}]: checking {finding.category} ({n_files} evidence files)")

    finding_json = json.dumps(asdict(finding), indent=2)
    system = _REVIEW_VERIFY_SUBJECTIVE_SYSTEM.format(category=finding.category)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Proposed finding:\n{finding_json}\n\nCommitted evidence bundle:\n{evidence}"},
    ]
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review: verify {finding.category} {finding.title}",
        metrics=state.metrics,
    )
    verdict = _parse_review_verdict_line(raw)
    if verdict is None:
        state.metrics["verifier_no_verdict"] += 1
        raise _TransientVerifierError("verifier produced no verdict token")
    if verdict:
        _ui_info(ui, f"    verifier [{locs}]: CONFIRMED — {finding.title}")
    else:
        _ui_info(ui, f"    verifier [{locs}]: REFUTED — {finding.title}")
    return verdict, raw[-1000:]


def _verify_single_finding(
    finding: ReviewFinding, state: ReviewRunState, ctx: "InputContext",
    work_dir: Path, ui: "AuditUI | None" = None,
) -> ConfirmedFinding | None:
    category = finding.category
    if category in ("bug", "performance"):
        confirmed, summary = _verify_bug_or_perf(finding, state, ctx, work_dir, ui=ui)
    else:
        confirmed, summary = _verify_subjective(finding, state, ctx, ui=ui)

    if not confirmed:
        _ui_info(ui, f"  discarded (refuted): {finding.title}")
        return None
    return ConfirmedFinding(
        finding=finding,
        grounding_reason=f"confirmed by {category} verification",
        reproducer={"summary": summary},
    )


def _verify_one_finding(
    item: tuple[str, ReviewFinding],
    state: ReviewRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> ReviewVerificationResult:
    fkey, finding = item
    work_dir = (
        Path(ctx.base_dir) / state.state_dir / state.run_id
        / "verify" / fkey / "work"
    )
    attempts = 0
    try:
        attempts += 1
        confirmed = _verify_single_finding(finding, state, ctx, work_dir, ui=ui)
    except _TransientVerifierError as e:
        state.metrics["verifier_transient_retries"] += 1
        _ui_info(ui, f"  [{fkey}] retrying {finding.title} after transient error: {e}")
        try:
            attempts += 1
            confirmed = _verify_single_finding(finding, state, ctx, work_dir, ui=ui)
        except Exception as e2:
            return ReviewVerificationResult(finding_key=fkey, error=str(e2), attempts=attempts)
    except Exception as e:
        return ReviewVerificationResult(finding_key=fkey, error=str(e), attempts=attempts)

    if confirmed is None:
        return ReviewVerificationResult(finding_key=fkey, discarded=True, attempts=attempts)
    return ReviewVerificationResult(finding_key=fkey, confirmed_finding=confirmed, attempts=attempts)


# ---------------------------------------------------------------------------
# Phase 4.5: Adjudication
# ---------------------------------------------------------------------------


def _review_finding_brief(finding: ReviewFinding) -> str:
    locs = ", ".join(finding.locations) if finding.locations else finding.source_file
    preconds = "; ".join(finding.preconditions) if finding.preconditions else "(none)"
    evidence = " ".join(finding.evidence) if finding.evidence else "(none)"
    return (
        f"title: {finding.title}\n"
        f"category: {finding.category}\n"
        f"severity: {finding.severity}\n"
        f"locations: {locs}\n"
        f"preconditions: {preconds}\n"
        f"evidence: {evidence}\n"
        f"proposed fix: {finding.fix_outline}"
    )


def _review_adjudicate_review_one(
    finding: ReviewFinding, lens: str, evidence: str,
    reproducer_summary: str, state: ReviewRunState, ctx: "InputContext",
) -> dict | None:
    user = (
        f"Finding under review:\n{_review_finding_brief(finding)}\n\n"
        f"Phase 4 verification summary:\n{reproducer_summary or '(none)'}\n\n"
        f"Committed evidence bundle:\n{evidence}"
    )
    messages = [
        {"role": "system", "content": _REVIEW_VERDICT_SYSTEM.format(lens=lens)},
        {"role": "user", "content": user},
    ]
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review: phase 4.5 adjudicate {finding.title}",
        metrics=state.metrics,
    )
    try:
        records = _parse_records_with_repair(
            ctx, raw,
            schema=_REVIEW_VERDICT_SCHEMA,
            worked_example=_REVIEW_VERDICT_WORKED_EXAMPLE,
            metrics=state.metrics,
        )
    except ValueError:
        return None
    return records[0] if records else None


def _review_consolidate(
    finding: ReviewFinding, verdicts: list[dict], evidence: str,
    state: ReviewRunState, ctx: "InputContext",
) -> dict | None:
    panel_text = "\n".join(
        f"- verdict={v.get('verdict')} fit={v.get('category_fit')} "
        f"severity={v.get('severity')} reason={v.get('reason')}"
        for v in verdicts
    )
    user = (
        f"Proposed finding:\n{_review_finding_brief(finding)}\n\n"
        f"Refutation panel verdicts:\n{panel_text}\n\n"
        f"Committed evidence bundle:\n{evidence}"
    )
    messages = [
        {"role": "system", "content": _REVIEW_CONSOLIDATE_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw = _call_audit_llm(
        ctx, messages,
        trace_task=f"review: phase 4.5 consolidate {finding.title}",
        metrics=state.metrics,
    )
    try:
        records = _parse_records_with_repair(
            ctx, raw,
            schema=_REVIEW_RULING_SCHEMA,
            worked_example=_REVIEW_CONSOLIDATE_WORKED_EXAMPLE,
            metrics=state.metrics,
        )
    except ValueError:
        return None
    return records[0] if records else None


def _panel_drop_reason(verdicts: list[dict]) -> str:
    rejecting = [
        v for v in verdicts
        if v.get("verdict") != "real" or v.get("category_fit") != "yes"
    ]
    reasons = [v.get("reason", "") for v in rejecting if v.get("reason")]
    if reasons:
        return "; ".join(reasons[:2])[:300]
    return "panel majority judged this not worth reporting"


def _review_adjudicate_one(
    item: tuple[int, ConfirmedFinding],
    state: ReviewRunState, ctx: "InputContext",
    ui: "AuditUI | None" = None,
) -> ReviewAdjudicationResult:
    index, cf = item
    finding = cf.finding
    original_severity = finding.severity
    reproducer_summary = str(cf.reproducer.get("summary", "")) if cf.reproducer else ""

    try:
        evidence, _n = _gather_evidence(finding, state, ctx)
    except Exception as e:
        return ReviewAdjudicationResult(
            index=index, kept=True, decision="error", finding=finding,
            original_severity=original_severity, final_severity=original_severity,
            reason="adjudication skipped: evidence gather failed", error=str(e),
        )

    verdicts: list[dict] = []
    for lens in _REVIEW_LENSES:
        v = None
        for attempt in range(2):
            if attempt:
                state.metrics["adjudication_lens_retries"] += 1
            try:
                v = _review_adjudicate_review_one(
                    finding, lens, evidence, reproducer_summary, state, ctx,
                )
            except Exception as e:
                _ui_info(ui, f"    adjudicate [{finding.title}]: reviewer error: {e}")
                v = None
            if v is not None:
                break
        if v is not None:
            verdicts.append(v)

    if len(verdicts) < 2:
        return ReviewAdjudicationResult(
            index=index, kept=True, decision="keep", finding=finding,
            original_severity=original_severity, final_severity=original_severity,
            reason=f"adjudication inconclusive: only {len(verdicts)} usable verdict(s)",
        )

    confirming = [
        v for v in verdicts
        if v.get("verdict") == "real" and v.get("category_fit") == "yes"
    ]
    if len(confirming) * 2 <= len(verdicts):
        return ReviewAdjudicationResult(
            index=index, kept=False, decision="drop", finding=finding,
            original_severity=original_severity, final_severity=original_severity,
            reason=_panel_drop_reason(verdicts),
        )

    panel_sev = _consensus_severity([v.get("severity", "") for v in confirming])
    ruling = None
    try:
        ruling = _review_consolidate(finding, verdicts, evidence, state, ctx)
    except Exception as e:
        _ui_info(ui, f"    adjudicate [{finding.title}]: consolidate error: {e}")

    updated = replace(finding)
    if ruling is not None:
        ruling_sev = ruling.get("severity") or original_severity
        proposed_sev = _less_severe_of(ruling_sev, panel_sev) if panel_sev else ruling_sev
        updated.title = ruling.get("title") or finding.title
        updated.category = ruling.get("category") or finding.category
        updated.fix_outline = ruling.get("fix_outline") or finding.fix_outline
        preconds = ruling.get("preconditions")
        if preconds:
            updated.preconditions = preconds
    else:
        proposed_sev = panel_sev or original_severity

    final_sev = _demote_only(original_severity, proposed_sev)
    updated.severity = final_sev

    changed = (
        updated.title != finding.title
        or updated.category != finding.category
        or updated.severity != finding.severity
        or updated.fix_outline != finding.fix_outline
        or updated.preconditions != finding.preconditions
    )
    return ReviewAdjudicationResult(
        index=index, kept=True,
        decision="keep_with_changes" if changed else "keep",
        finding=updated,
        original_severity=original_severity,
        final_severity=final_sev,
        reason="; ".join(v.get("reason", "") for v in confirming if v.get("reason"))[:300],
    )


# ---------------------------------------------------------------------------
# Phase 5: Artifacts
# ---------------------------------------------------------------------------


def _review_patch(
    cf: ConfirmedFinding, ctx: "InputContext", state: ReviewRunState,
    patch_max_turns: int = _DEFAULT_PATCH_MAX_TURNS,
    ui: "AuditUI | None" = None,
) -> ReviewPatchResult:
    from .agent import run_agent_loop

    finding_json = json.dumps(asdict(cf.finding), indent=2)
    evidence, _n = _gather_evidence(cf.finding, state, ctx)
    work_dir = Path(ctx.base_dir) / state.state_dir / state.run_id / "patch-gen"

    try:
        with _worktree(ctx.base_dir, work_dir):
            prompt = (
                f"Fix the following code-review finding with the smallest correct "
                f"change. Use edit_file to make the fix. Do not make unrelated changes.\n\n"
                f"{finding_json}\n\n"
                f"Committed source for affected files:\n{evidence}"
            )
            messages = [
                {"role": "system", "content": "You are fixing a code issue. Make the minimal correct fix using edit_file."},
                {"role": "user", "content": prompt},
            ]
            kw = _make_isolated_loop_kwargs(ctx, work_dir, max_turns=patch_max_turns)

            tracker = kw.get("file_tracker")
            if tracker is not None:
                evidence_paths = _evidence_file_paths(cf.finding)
                for rel in evidence_paths:
                    tracker.record_read(str(work_dir / rel))

            try:
                _answer, exhausted = run_agent_loop(messages, ctx.tools, **kw)
            except Exception as e:
                return ReviewPatchResult(error_code="patch_agent_error", error=f"agent loop failed: {e}")
            finally:
                _write_audit_trace(ctx, messages, task=f"review: patch {cf.finding.title}")

            if exhausted:
                return ReviewPatchResult(error_code="patch_turn_budget_exhausted", error="turn budget exhausted")

            diff = subprocess.run(
                ["git", "diff"], capture_output=True, cwd=str(work_dir), timeout=10,
            )
            patch_text = diff.stdout.decode(errors="replace").strip()
            if not patch_text:
                return ReviewPatchResult(error_code="patch_no_diff", error="no changes produced")
            return ReviewPatchResult(patch_text=patch_text + "\n")
    except RuntimeError as e:
        return ReviewPatchResult(error_code="patch_worktree_error", error=f"worktree failed: {e}")


def _review_report(
    cf: ConfirmedFinding, patch_filename: str, patch_text: str,
    state: ReviewRunState, ctx: "InputContext",
) -> str:
    finding_json = json.dumps(asdict(cf.finding), indent=2)
    evidence, _n = _gather_evidence(cf.finding, state, ctx)
    system = _REVIEW_REPORT_TEMPLATE
    suffix = (
        f"Confirmed finding:\n{finding_json}\n\n"
        f"Grounding reason:\n{cf.grounding_reason}\n\n"
        f"Affected source:\n{evidence}\n\n"
        f"Patch ({patch_filename}):\n```diff\n{patch_text}```"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": suffix},
    ]
    return _call_audit_llm(
        ctx, messages,
        trace_task=f"review: report {cf.finding.title}",
        metrics=state.metrics,
    )


# ---------------------------------------------------------------------------
# README rendering
# ---------------------------------------------------------------------------

_CATEGORY_ORDER = ("bug", "performance", "design", "consistency", "flaw", "smell")
_SEVERITY_SORT = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CATEGORY_LABEL = {
    "bug": "Bug", "performance": "Performance", "design": "Design",
    "consistency": "Consistency", "flaw": "Flaw", "smell": "Smell",
}


def _make_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] if slug else "finding"


def _artifact_filenames(index: int, finding: ReviewFinding) -> tuple[str, str]:
    slug = _make_slug(finding.title)
    return f"{index:03d}-{slug}.patch", f"{index:03d}-{slug}.md"


def _ensure_artifact_state(state: ReviewRunState) -> None:
    current = {
        _review_finding_key(cf): cf for cf in state.confirmed_findings
    }
    for key in list(state.artifact_state):
        if key not in current:
            del state.artifact_state[key]
    for key, cf in current.items():
        if key in state.artifact_state:
            continue
        next_idx = max(
            (int(e["index"]) for e in state.artifact_state.values()),
            default=0,
        ) + 1
        patch_fn, report_fn = _artifact_filenames(next_idx, cf.finding)
        state.artifact_state[key] = {
            "index": next_idx,
            "patch_filename": patch_fn,
            "report_filename": report_fn,
            "patch_status": "pending",
            "report_status": "pending",
        }


def _render_review_readme(state: ReviewRunState, *, repo_name: str = "") -> str:
    lines: list[str] = []
    lines.append("# Code Review Findings\n")
    if repo_name:
        lines.append(f"**Repository:** {repo_name}")
    lines.append(f"**Run:** {state.run_id} · {state.scope.branch} @ {state.scope.commit[:8]}")
    lines.append(f"**Files reviewed:** {len(state.candidate_files)}\n")

    n_proposed = len(state.proposed_findings)
    n_confirmed = len(state.confirmed_findings)
    n_dropped = len(state.adjudication_discarded)
    lines.append("## Summary\n")
    lines.append(f"- Findings proposed: {n_proposed}")
    lines.append(f"- Findings confirmed: {n_confirmed}")
    lines.append(f"- Findings dropped (adjudication): {n_dropped}\n")

    if state.adjudication_discarded:
        lines.append("### Dropped by adjudication\n")
        for d in state.adjudication_discarded:
            lines.append(f"- [{d.get('severity', '?')}] {d.get('title', '?')} — {d.get('reason', '')}")
        lines.append("")

    lines.append("## Findings\n")
    for cat in _CATEGORY_ORDER:
        cat_findings = [
            (i, cf) for i, cf in enumerate(state.confirmed_findings)
            if cf.finding.category == cat
        ]
        if not cat_findings:
            continue
        cat_findings.sort(key=lambda t: _SEVERITY_SORT.get(t[1].finding.severity, 99))
        lines.append(f"### {_CATEGORY_LABEL.get(cat, cat)}\n")
        lines.append("| # | Severity | Title | Location | Patch | Report |")
        lines.append("|---|---|---|---|---|---|")
        for idx, cf in cat_findings:
            key = _review_finding_key(cf)
            art = state.artifact_state.get(key, {})
            patch_fn = art.get("patch_filename", "")
            report_fn = art.get("report_filename", "")
            loc = cf.finding.locations[0] if cf.finding.locations else cf.finding.source_file
            lines.append(
                f"| {idx + 1} | {cf.finding.severity} | {cf.finding.title} | "
                f"`{loc}` | {f'[{patch_fn}]({patch_fn})' if patch_fn else '—'} | "
                f"{f'[{report_fn}]({report_fn})' if report_fn else '—'} |"
            )
        lines.append("")
    return "\n".join(lines)


def _write_review_readme(state: ReviewRunState, base_dir: str) -> bool:
    artifact_dir = Path(base_dir) / state.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    repo_name = Path(base_dir).resolve().name
    readme = _render_review_readme(state, repo_name=repo_name)
    (artifact_dir / "README.md").write_text(readme)
    return True


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_review_config(base_dir: str) -> tuple[list[str], dict[str, str], int | None]:
    """Read review config from swival.toml: (force_review, force_review_sources, patch_max_turns)."""
    force_review: list[str] = []
    force_review_sources: dict[str, str] = {}
    patch_max_turns: int | None = None
    try:
        from .config import load_toml_config
        toml = load_toml_config(base_dir)
        if toml and "review" in toml:
            cfg = toml["review"]
            if isinstance(cfg, dict):
                fr = cfg.get("force_review", [])
                if isinstance(fr, list):
                    force_review = fr
                pmt = cfg.get("patch_max_turns")
                if isinstance(pmt, int):
                    patch_max_turns = pmt
    except Exception:
        pass
    return force_review, force_review_sources, patch_max_turns


def _resolve_force_review(
    globs: list[str], sources: dict[str, str], mandatory_files: list[str],
) -> tuple[dict[str, str], list[str]]:
    matches: dict[str, str] = {}
    warnings: list[str] = []
    for glob in globs:
        source = sources.get(glob, "swival.toml")
        matched = [f for f in mandatory_files if _match_path_glob(f, glob)]
        if not matched:
            warnings.append(f"force-review glob {glob!r} matched no files")
        for f in matched:
            matches[f] = source
    return matches, warnings


def _exact_focus_paths(scope: AuditScope) -> list[str]:
    mandatory = set(scope.mandatory_files)
    return [p for p in scope.focus if p in mandatory]


# ---------------------------------------------------------------------------
# Phase titles & pipeline
# ---------------------------------------------------------------------------

_PHASE_TITLES: dict[str, tuple[str, str]] = {
    "inventory": ("Phase 1 · Inventory", "inventory"),
    "triage": ("Phase 2 · Triage", "triage"),
    "deep_review": ("Phase 3 · Deep Review", "deep_review"),
    "grounding": ("Phase 4 · Grounding", "grounding"),
    "adjudication": ("Phase 4.5 · Adjudication", "grounding"),
    "artifacts": ("Phase 5 · Artifacts", "artifacts"),
}


def _phase_open(
    ui: AuditUI, phase_key: str, *, total: int | None = None, label: str | None = None,
) -> PhaseHandle:
    title, color_key = _PHASE_TITLES[phase_key]
    if label is not None:
        title = f"{title} · {label}"
    elif total is not None:
        title = f"{title} · {total} file{'s' if total != 1 else ''}"
    return ui.phase(title, total=total, color=fmt.phase_color(color_key))


def _run_pipeline_body(
    state: ReviewRunState, ui: AuditUI, ctx: "InputContext", base_dir: str,
    workers: int, *, resume: bool, force_review: list[str],
    force_review_sources: dict[str, str], patch_max_turns: int,
    selected_indexes: set[int] | None,
) -> str:

    # ---- Phase 1: scope + profile ----
    if state.phase == "init":
        n_files = len(state.scope.mandatory_files)
        ph1 = _phase_open(ui, "inventory", total=4, label=f"{n_files} file{'s' if n_files != 1 else ''}")
        ph1.set_current(f"loading {n_files} files")
        if not ui.is_live:
            fmt.info(f"phase 1: loading {n_files} file contents...")
        content_cache = _load_file_contents(state.scope.mandatory_files, base_dir)
        ph1.advance(current="building import/caller indices")
        if not ui.is_live:
            fmt.info("phase 1: building import/caller indices...")
        state.import_index, state.caller_index, state.symbol_spans_index = _build_context_indices(
            state.scope.mandatory_files, content_cache
        )
        state._content_cache.update(content_cache)
        ph1.advance(current="ordering by review surface")
        if not ui.is_live:
            fmt.info("phase 1: ordering by review surface...")
        state.queued_files, state.review_scores = _order_by_review_surface(
            state.scope.mandatory_files, content_cache, state.dependency_index
        )
        ph1.advance(current="calling LLM for repo profile")
        if not ui.is_live:
            fmt.info("phase 1: calling LLM for repo profile...")
        try:
            state.repo_profile = _phase1_review_profile(state, ctx)
        except ValueError as e:
            _ui_warning(ui, f"phase 1: profile parse failed ({e}); continuing with empty profile")
            state.repo_profile = {}
        ph1.advance()
        state.phase = "triage"
        state.save()
        summary_str = state.repo_profile.get("summary", "")[:80]
        if not ui.is_live:
            fmt.info(f"phase 1 complete. profile: {summary_str}")
        ph1.complete(f"profile: {summary_str}" if summary_str else None)

    # Resume rule: re-apply force_review before Phase 2.
    if resume and state.phase in ("triage", "deep_review") and force_review:
        force_matches, force_warnings = _resolve_force_review(
            force_review, force_review_sources, state.scope.mandatory_files
        )
        for w in force_warnings:
            ui.warning(w)
        re_promoted = 0
        for path, source in force_matches.items():
            rec = state.triage_records.get(path)
            if rec is None:
                continue
            reason = f"forced via swival.toml ({source})"
            if reason in rec.promotion_reasons:
                continue
            rec.promotion_reasons.append(reason)
            if rec.priority == "SKIP":
                rec.priority = "REVIEW_LOW"
                if path not in state.candidate_files:
                    state.candidate_files.append(path)
                re_promoted += 1
        if re_promoted:
            ui.scrollback(f"resume: {re_promoted} file(s) promoted via updated swival.toml force_review")
            state.save()

    if state.phase == "triage" and state.select_all:
        state.candidate_files = list(state.queued_files)
        state.reviewed_files.update(state.queued_files)
        state.phase = "deep_review"
        state.save()
        ui.scrollback(f"phase 2: skipped (--all); {len(state.candidate_files)} files queued for deep review")

    # ---- Phase 2: Triage ----
    if state.phase == "triage":
        def _triage(path):
            return _review_triage_one(path, state, ctx)

        ph2 = _phase_open(ui, "triage", total=len(state.queued_files))
        if not ui.is_live:
            fmt.info(f"phase 2: triaging {len(state.queued_files)} files...")

        def _on_triage(idx, item, rec):
            if rec is not None:
                state.triage_records[rec.path] = rec
                state.reviewed_files.add(rec.path)
                ph2.advance()
                state.save()
                if not ui.is_live:
                    done = len(state.reviewed_files)
                    total = len(state.queued_files)
                    if done % 10 == 0 or done == total:
                        fmt.info(f"  triaged {done}/{total} files")

        to_triage = [f for f in state.queued_files if f not in state.reviewed_files]
        _run_batch(
            _triage, to_triage, max_workers=max(1, workers),
            ui=ui, label_for=lambda p: p, on_result=_on_triage,
        )

        # Confirmation pass on low-confidence SKIPs touching core modules/entry points.
        core_and_entries = _core_module_paths(state) | set(_entry_point_paths(state))
        to_confirm = [
            p for p, r in state.triage_records.items()
            if r.priority == "SKIP" and r.confidence == "low" and p in core_and_entries
        ]
        for p in to_confirm:
            rec = _review_confirm_one(p, state, ctx)
            state.triage_records[p] = rec
            if rec.confirmation_outcome == "promoted" and p not in state.candidate_files:
                pass  # promotions handle candidate_files below
            state.save()

        # Apply promotions.
        force_matches, force_warnings = _resolve_force_review(
            force_review, force_review_sources, state.scope.mandatory_files
        )
        for w in force_warnings:
            ui.warning(w)
        _apply_review_promotions(state, force_matches)

        # Build candidate list.
        state.candidate_files = [
            p for p in state.queued_files
            if state.triage_records.get(p, ReviewTriageRecord(
                p, "SKIP", "low", [], "", [], [], False
            )).priority in ("REVIEW_HIGH", "REVIEW_LOW")
        ]
        state.phase = "deep_review"
        state.save()

        n_high = sum(1 for r in state.triage_records.values() if r.priority == "REVIEW_HIGH")
        n_low = sum(1 for r in state.triage_records.values() if r.priority == "REVIEW_LOW")
        ph2.complete(f"{len(state.candidate_files)} escalated ({n_high} high, {n_low} low)")
        if not ui.is_live:
            fmt.info(f"phase 2 complete. {len(state.candidate_files)} files escalated.")

    # ---- Phase 3: Deep review ----
    if state.phase == "deep_review":
        to_review = [f for f in state.candidate_files if f not in state.deep_reviewed_files]
        ph3 = _phase_open(ui, "deep_review", total=len(state.candidate_files))
        if not ui.is_live:
            fmt.info(f"phase 3: deep-reviewing {len(to_review)} files...")

        def _fn_dr(path):
            return _deep_review_one(path, state, ctx, ui=ui)

        def _on_dr(idx, item, result):
            if result is not None:
                state.deep_reviewed_files.add(result.path)
                if result.findings:
                    for f in result.findings:
                        state.proposed_findings.append(f)
                        if ui is not None:
                            ui.finding(f.severity, f.title, f.source_file)
                if result.error:
                    _ui_warning(ui, f"  {result.path}: {result.error}")
                ph3.advance()
                state.save()

        _run_batch(
            _fn_dr, to_review, max_workers=max(1, workers),
            ui=ui, label_for=lambda p: p, on_result=_on_dr,
        )
        for f in state.candidate_files:
            if f in state.deep_reviewed_files:
                ph3.advance()
        ph3.complete(f"{len(state.proposed_findings)} finding(s)")
        if not ui.is_live:
            fmt.info(f"phase 3 complete. {len(state.proposed_findings)} finding(s) proposed.")
        state.phase = "grounding"
        state.save()

    # ---- Phase 4: Grounding ----
    if state.phase == "grounding":
        items = [
            (_review_finding_key(f), f) for f in state.proposed_findings
            if _review_finding_key(f) not in state.verification_state
        ]
        ph4 = _phase_open(ui, "grounding", total=len(state.proposed_findings))
        if not ui.is_live:
            fmt.info(f"phase 4: grounding {len(items)} finding(s)...")

        def _fn_v(item):
            return _verify_one_finding(item, state, ctx, ui=ui)

        def _on_v(idx, item, result):
            if result is not None:
                fkey = result.finding_key
                if result.confirmed_finding is not None:
                    state.verified_state = state.verification_state
                    state.verification_state[fkey] = {"status": "confirmed"}
                    if ui is not None:
                        ui.tally(verified=1, severity=item[1].severity)
                elif result.discarded:
                    state.verification_state[fkey] = {"status": "refuted"}
                    if ui is not None:
                        ui.tally(discarded=1)
                elif result.error:
                    state.verification_state[fkey] = {"status": "error", "error": result.error}
                    if ui is not None:
                        ui.tally(failed=1)
                ph4.advance()
                state.save()

        results = _run_batch(
            _fn_v, items, max_workers=max(1, workers),
            ui=ui, label_for=lambda t: t[1].title, on_result=_on_v,
        )

        # Collect confirmed findings.
        for (fkey, finding), result in zip(items, results):
            if result is not None and result.confirmed_finding is not None:
                state.confirmed_findings.append(result.confirmed_finding)

        ph4.complete(f"{len(state.confirmed_findings)} confirmed")
        if not ui.is_live:
            n_conf = len(state.confirmed_findings)
            n_disc = sum(1 for v in state.verification_state.values() if v.get("status") == "refuted")
            fmt.info(f"phase 4 complete. {n_conf} confirmed, {n_disc} refuted.")
        state.phase = "adjudication"
        state.save()

    # ---- Phase 4.5: Adjudication ----
    if state.phase == "adjudication":
        items = list(enumerate(state.confirmed_findings))
        ph45 = _phase_open(ui, "adjudication", total=len(items))
        if not ui.is_live:
            fmt.info(f"phase 4.5: adjudicating {len(items)} finding(s)...")

        verify_discarded = sum(
            1 for v in state.verification_state.values() if v.get("status") == "refuted"
        )
        ui.set_outcome_baseline(
            verified_severities=[cf.finding.severity for cf in state.confirmed_findings],
            discarded=verify_discarded,
        )

        def _fn_a(item):
            return _review_adjudicate_one(item, state, ctx, ui=ui)

        def _on_a(idx, item, result):
            if result is not None:
                if not result.kept:
                    state.adjudication_discarded.append({
                        "title": result.finding.title,
                        "severity": result.original_severity,
                        "source_file": result.finding.source_file,
                        "reason": result.reason,
                    })
                    if ui is not None:
                        ui.tally(verified=-1, severity=result.original_severity)
                        ui.tally(discarded=1)
                    _ui_info(ui, f"  dropped (adjudication): {result.finding.title} — {result.reason}")
                ph45.advance()
                state.save()

        results = _run_batch(
            _fn_a, items, max_workers=max(1, workers),
            ui=ui, label_for=lambda t: t[1].finding.title, on_result=_on_a,
        )

        # Rebuild confirmed_findings from kept results.
        kept: list[ConfirmedFinding] = []
        recalibrated = 0
        for i, result in enumerate(results):
            if result is None:
                kept.append(state.confirmed_findings[i])
                continue
            if not result.kept:
                continue
            cf = state.confirmed_findings[i]
            kept.append(replace(cf, finding=result.finding))
            if result.final_severity != result.original_severity:
                recalibrated += 1
                if ui is not None:
                    ui.tally(verified=-1, severity=result.original_severity)
                    ui.tally(verified=1, severity=result.final_severity)
                _ui_info(ui, f"  severity {result.original_severity} -> {result.final_severity}: {result.finding.title}")

        state.confirmed_findings = kept
        ph45.complete(f"{len(kept)} kept · {len(state.adjudication_discarded)} dropped · {recalibrated} recalibrated")
        if not ui.is_live:
            fmt.info(f"phase 4.5 complete. {len(kept)} kept, {len(state.adjudication_discarded)} dropped, {recalibrated} recalibrated.")
        state.phase = "artifacts"
        state.save()

    # ---- Phase 5: Artifacts ----
    if state.phase == "artifacts":
        _ensure_artifact_state(state)
        ph5 = _phase_open(ui, "artifacts", total=len(state.confirmed_findings))
        if not ui.is_live:
            fmt.info(f"phase 5: generating {len(state.confirmed_findings)} artifact(s)...")

        artifact_dir = Path(base_dir) / state.artifact_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)

        for i, cf in enumerate(state.confirmed_findings):
            key = _review_finding_key(cf)
            art = state.artifact_state.get(key, {})
            if not art:
                continue
            if selected_indexes is not None and i not in selected_indexes:
                ph5.advance()
                continue

            patch_fn = art["patch_filename"]
            report_fn = art["report_filename"]

            # Generate patch.
            if art.get("patch_status") != "done":
                pr = _review_patch(cf, ctx, state, patch_max_turns=patch_max_turns, ui=ui)
                if pr.patch_text is not None:
                    (artifact_dir / patch_fn).write_text(pr.patch_text)
                    art["patch_status"] = "done"
                else:
                    art["patch_status"] = "failed"
                    art["patch_error"] = pr.error_code or pr.error or "unknown"
                    _ui_warning(ui, f"  patch failed for {cf.finding.title}: {pr.error}")

            # Generate report.
            if art.get("report_status") != "done" and art.get("patch_status") == "done":
                patch_text = (artifact_dir / patch_fn).read_text() if (artifact_dir / patch_fn).exists() else ""
                try:
                    report = _review_report(cf, patch_fn, patch_text, state, ctx)
                    (artifact_dir / report_fn).write_text(report)
                    art["report_status"] = "done"
                except Exception as e:
                    art["report_status"] = "failed"
                    art["report_error"] = str(e)
                    _ui_warning(ui, f"  report failed for {cf.finding.title}: {e}")

            state.save()
            ph5.advance()

        _write_review_readme(state, base_dir)
        ph5.complete(f"{len(state.confirmed_findings)} finding(s)")
        if not ui.is_live:
            fmt.info(f"phase 5 complete. artifacts in {state.artifact_dir}/")
        state.phase = "done"
        state.save()

    # ---- Summary ----
    n_final = len(state.confirmed_findings)
    n_dropped = len(state.adjudication_discarded)
    n_refuted = sum(1 for v in state.verification_state.values() if v.get("status") == "refuted")
    lines = [
        "Code review complete.",
        f"  Files reviewed: {len(state.candidate_files)}",
        f"  Findings proposed: {len(state.proposed_findings)}",
        f"  Findings confirmed: {n_final}",
        f"  Findings refuted: {n_refuted}",
        f"  Findings dropped (adjudication): {n_dropped}",
        f"  Artifacts: {state.artifact_dir}/",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command entrypoint
# ---------------------------------------------------------------------------


def run_review_command(cmd_arg: str, ctx: "InputContext") -> str:
    """Entry point for the /review command. Returns summary text."""
    global _debug_log_path

    base_dir = ctx.base_dir
    workers = 4

    arg = cmd_arg.strip()
    resume = False
    regen = False
    debug = False
    select_all = False
    patch_max_turns_cli: int | None = None
    finding_selector: str | None = None
    focus: list[str] | None = None

    parts = arg.split()
    filtered = []
    i = 0
    while i < len(parts):
        if parts[i] == "--resume":
            resume = True
        elif parts[i] == "--regen":
            regen = True
        elif parts[i] == "--debug":
            debug = True
        elif parts[i] == "--all":
            select_all = True
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
        elif parts[i] == "--finding":
            if finding_selector is not None:
                return "error: --finding may only be provided once; use --finding 2,5"
            if i + 1 >= len(parts) or parts[i + 1].startswith("-"):
                return "error: --finding requires a selector"
            i += 1
            finding_selector = parts[i]
        elif parts[i].startswith("-"):
            return (
                f"error: unknown option {parts[i]!r}. "
                f"Known flags: --resume, --regen, --debug, --all, "
                f"--workers N, --patch-max-turns N, --finding N."
            )
        else:
            filtered.append(parts[i])
        i += 1
    if filtered:
        focus = _normalize_focus(filtered)

    if finding_selector is not None and not regen:
        return "error: --finding requires --regen"

    force_review, force_review_sources, config_patch_max_turns = _load_review_config(base_dir)
    patch_max_turns = (
        patch_max_turns_cli
        if patch_max_turns_cli is not None
        else config_patch_max_turns or _DEFAULT_PATCH_MAX_TURNS
    )

    if debug:
        log_dir = Path(base_dir) / ".swival" / "review"
        log_dir.mkdir(parents=True, exist_ok=True)
        _debug_log_path = log_dir / "debug.jsonl"
        _debug_log("review_start", args=arg)
        fmt.info(f"debug log: {_debug_log_path}")
    else:
        _debug_log_path = None

    state_dir = Path(base_dir) / ".swival" / "review"

    selected_indexes: set[int] | None = None
    if regen and finding_selector is not None:
        # Parse finding selector and find a completed run.
        try:
            from .audit import _parse_finding_selector
            selected_indexes = _parse_finding_selector(finding_selector, 9999)
        except Exception:
            return f"error: invalid --finding selector: {finding_selector!r}"
        # Find the most recent completed run.
        state = ReviewRunState.find_resumable(state_dir, _git(["rev-parse", "HEAD"], base_dir), focus, include_done=True)
        if state is None:
            return "error: no completed review run found to regenerate."
        # Reset artifact targets for selected findings.
        _ensure_artifact_state(state)
        for key, art in state.artifact_state.items():
            idx = art.get("index", 0) - 1
            if selected_indexes is not None and idx not in selected_indexes:
                continue
            art["patch_status"] = "pending"
            art["report_status"] = "pending"
        state.phase = "artifacts"
        state.save()
    elif resume:
        state = ReviewRunState.find_resumable(state_dir, _git(["rev-parse", "HEAD"], base_dir), focus)
        if state is None:
            return "error: no resumable review run found. Run /review without --resume to start a new one."
        state.select_all = select_all or state.select_all
    else:
        scope = _resolve_scope(base_dir, focus or [])
        dirty = _dirty_worktree_warning(base_dir, scope)
        if dirty:
            fmt.warning(dirty)
        state = ReviewRunState(
            run_id=uuid.uuid4().hex[:12],
            scope=scope,
            select_all=select_all,
            state_dir=state_dir,
        )

    ui = AuditUI(
        run_id=state.run_id,
        branch=state.scope.branch,
        commit=state.scope.commit,
        workers=workers,
        total_files=len(state.scope.mandatory_files),
    )

    try:
        with ui:
            result = _run_pipeline_body(
                state, ui, ctx, base_dir, workers,
                resume=resume, force_review=force_review,
                force_review_sources=force_review_sources,
                patch_max_turns=patch_max_turns,
                selected_indexes=selected_indexes,
            )
            if state.phase == "done":
                ui.summary(
                    artifact_dir=str(state.artifact_dir),
                    written=len(state.confirmed_findings),
                    readme_written=True,
                )
            else:
                ui.incomplete(result)
            return result
    finally:
        _debug_log_path = None
