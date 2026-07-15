# Global Code Review — Design

A `/review` pipeline that hunts **design, consistency, flaw, code-smell, bug,
and performance** issues across committed Git-tracked code, mirroring `/audit`'s
staged approach:
every finding is independently **confirmed** before it is kept.

The central design principle borrowed from `/audit` is the *two-gate confirmation
model*. A finding is never reported just because Phase 3 proposed it. It must
survive:

1. **Grounding (Phase 4)** — an independent verifier treats the finding as a
   hypothesis and checks it against committed evidence (and, for `bug`-category
   findings, against a running isolated worktree). Unconfirmed findings are
   discarded.
2. **Adversarial refutation (Phase 4.5)** — a panel of skeptical reviewers, each
   through a different lens, defaults to *reject* and must be convinced by a
   strict majority. Findings the panel cannot justify are dropped; severity may
   only be lowered, never raised.

This document specifies the full pipeline. It is written so an implementer can
build `review.py` by analogy with `audit.py`, reusing `audit.py`'s shared
machinery (git scope, content cache, import/caller/symbol indices, callee
context, `PhaseSchema` record parsing, `_run_batch`, `AuditUI`, resumable state)
verbatim and swapping only the *domain* layer: the category system, the
review-surface scoring, the prompt schemas, and the confirmation criteria.

---

## 1. What changes from `/audit`, and what does not

### Reused unchanged (import from `audit.py` or share a module)

| Concern | `audit.py` symbol | Reuse |
|---|---|---|
| Git scope resolution | `_resolve_scope`, `AuditScope` | as-is |
| File loading | `_load_file_contents`, `_git_show`, `_cached_git_show` | as-is |
| Import/caller/symbol indices | `_build_context_indices`, `_extract_imports/exports` | as-is |
| Cross-file callee context | `_gather_callee_context`, `_gather_callee_context_for_paths` | as-is |
| Record parsing + repair | `PhaseSchema`, `RecordSchema`, `_parse_records_with_repair` | as-is |
| LLM call wrapper | `_call_audit_llm`, `_write_audit_trace` | as-is |
| Parallel batch | `_run_batch` (worker slots, UI events) | as-is |
| Isolated worktree | `_worktree`, `_make_isolated_loop_kwargs` | as-is (Phase 4 bug/perf path) |
| Live UI | `AuditUI`, `PhaseHandle` | as-is, new phase titles |
| Resumable state pattern | mirror `AuditRunState.save()/load()/find_resumable()` | new dataclass, same shape |

### Domain-specific (new in `review.py`)

- **Category system** replacing security bug-classes (§3).
- **Review-surface scoring** replacing attack-surface scoring (§4).
- **Repo profile schema** oriented to design surfaces, not trust boundaries (§5).
- **Triage, inventory, expansion, verification, adjudication prompts** retargeted
  to general review (§§6–10).
- **Severity ladder** retargeted to maintenance/correctness impact (§3.2).
- **Confirmation criteria** retargeted: grounding checks accuracy + evidence;
  panel checks significance + category-fit + severity (§§9–10).

---

## 2. Pipeline overview

```
Phase 1  Inventory        resolve scope · load files · build indices ·
                          score by review surface · LLM repo profile
                          ───────────────────────────────────────► ReviewRunState
Phase 2  Triage           per-file LLM triage → REVIEW_HIGH / REVIEW_LOW / SKIP
                          promotion rules override SKIPs
                          confirmation pass on low-confidence SKIPs
                          ───────────────────────────────────────► candidate_files
Phase 3  Deep review      per candidate file:
   3a     Inventory          list candidate issues (title, category, severity,
                             location, symptom, claim)
   3b     Expansion          expand each into a structured finding
                             (category, symptom, evidence, impact, fix_outline)
                          discard out-of-category / below-floor
                          ───────────────────────────────────────► proposed_findings
Phase 4  Grounding         per finding: independent verifier
                             - bug / performance → isolated worktree
                               (reproduction / measurement)
                             - other categories → evidence-grounded check
                           CONFIRMED / REFUTED; discard REFUTED
                          ───────────────────────────────────────► confirmed_findings
Phase 4.5 Adjudication     per confirmed finding: adversarial refutation panel
                          three lenses (grounding, significance, category-fit)
                          default-to-reject; strict majority to keep
                          consolidate + demote-only severity
                          ───────────────────────────────────────► final findings
Phase 5  Artifacts         per finding: fix patch + markdown report + README
```

State transitions are phase-gated exactly as in `audit.py`: each phase sets
`state.phase` and calls `state.save()` so `/review --resume` re-enters at the
right point. `--all` skips triage; `--regen --finding N` re-runs Phase 5 for one
finding.

---

## 3. Category system

`/audit` is security-only: a finding is in scope only when an untrusted actor
has a concrete trigger yielding a security outcome. `/review` widens scope to
six categories. The scope gate (§7.4) is retargeted per category: each has its
own "must name three things" test, but the *shape* — name the concrete subject,
name the concrete symptom, name the concrete consequence — is preserved so the
two confirmation gates can still adjudicate objectively.

### 3.1 Categories

| Category | One-line scope | Out of scope (→ SKIP / drop) |
|---|---|---|
| `design` | The shape of the abstraction/module boundary is wrong: responsibility violations, leaky abstractions, wrong level of indirection, circular or excessive coupling, missing module that forces duplication. | Style preference, "I would have done X instead" without a concrete consequence. |
| `consistency` | The same problem is solved two divergent ways within the codebase: naming drift, parallel structures that diverged, convention breaks that will confuse contributors. | One-off local style; cosmetic. |
| `flaw` | A logic error or incorrect invariant that is *not* a runtime-reproducible defect on its own: wrong algorithm, edge case the code does not handle, race that does not yet manifest, invariant the code silently breaks. | "Could be more robust"; defense-in-depth suggestions. |
| `smell` | Maintainability hazard with real cost: dead code, duplication (DRY), god object, long method, deep nesting, magic number, complex conditional, premature abstraction. | Trivial nits; bikeshedding. |
| `bug` | A runtime-defect reproducible from committed code: incorrect output, crash, unhandled exception, hang, data corruption. | Hypothetical "could crash if X" without a concrete trigger path. |
| `performance` | The code is correct but pathologically inefficient under realistic input: wrong complexity class (O(n²) where O(n) fits), N+1 query, redundant allocation, repeated work in a hot path, unbounded growth. | Micro-optimization; premature optimization with no measured or realistic scaling cost. |

`bug` and `performance` are the two categories eligible for the Phase 4 worktree
reproduction path — `bug` claims are reproduced, `performance` claims are
measured (timing, allocation count) in the isolated worktree. The other four
are confirmed by evidence-grounded reasoning (§9.2).

### 3.2 Severity ladder

Replaces `/audit`'s security severity (low/medium/high/critical) with a
maintenance, correctness, and performance ladder. Same four levels, demote-only
in adjudication.

- **critical** — a `bug` that breaks core behavior or causes data loss; a
  `design` flaw that makes the system unsound (the abstraction cannot uphold its
  contract for a real caller); a `performance` issue that causes service
  exhaustion or timeout under realistic load.
- **high** — a `bug` on a real code path; a `design`/`consistency` issue that
  will cause repeated bugs or measurably blocks maintainability (e.g. a leaky
  abstraction every new caller must work around); a `performance` issue on a
  real hot path with a concrete scaling cliff (e.g. quadratic blowup on
  attacker- or user-sized input).
- **medium** — a `smell`/`flaw` with real maintenance cost; a `consistency`
  break that will confuse the next contributor; a `performance` issue with a
  real but bounded cost (e.g. redundant allocation noticeable only at scale).
- **low** — minor smell or nitpick; a `performance` micro-issue with marginal
  cost. Kept only when the panel agrees it matters. Default-drop: the panel
  should reject low-severity findings unless the consequence is concrete.

Severity anchors to the project's *realistic* maintenance reality, not a
worst-case hypothetical — the same tie-break-downward rule as `/audit`.

---

## 4. Review-surface scoring (Phase 1 ordering)

`/audit` orders files by an attack-surface keyword score so the most
security-sensitive files are reviewed first. `/review` orders by a
**review-surface score**: a cheap heuristic for "this file is likely to hold
real design/complexity issues." The score is the sum of weighted pattern hits,
exactly mirroring `_ATTACK_SURFACE_PATTERNS` / `_score_attack_surface`.

```python
_REVIEW_SURFACE_PATTERNS = [
    # High complexity / high fan-in signals — most likely to hold design issues.
    (r"\b(class|struct|interface|trait|impl|enum)\b", 2),   # type definitions
    (r"\b(def|function|func|fun|fn|sub)\b", 1),              # definitions (density)
    (r"\b(extends|implements|inherits|override|virtual)\b", 2),
    (r"\b(async|await|thread|lock|mutex|sync|channel|goroutine)\b", 3),  # concurrency
    (r"\b(try|catch|except|finally|throw|raise|panic)\b", 2),           # error surface
    (r"\b(if|elif|else|switch|match|case|for|while)\b", 1),             # branching density
    (r"\b(return|yield|break|continue)\b", 1),
    (r"\b(import|from|require|use|include)\b", 1),          # coupling
    (r"\b(abstract|generic|template|protocol|where)\b", 2), # abstraction surface
    (r"\b(TODO|FIXME|HACK|XXX|DEPRECATED)\b", 3),           # known debt markers
    (r"\b(for|while|foreach|each|iter|loop|repeat)\b", 2),  # loop density
    (r"\b(sort|sorted|map|filter|reduce|group|order)\b", 2),# collection passes
    (r"\b(alloc|malloc|new |append|extend|insert|copy|clone)\b", 2), # allocation
    (r"\b(select|query|execute|cursor|fetch|join)\b", 3),   # query/N+1 surface
    (r"\b(recurs|recur|call_self)\b", 2),                   # recursion
    (r"\b(cache|memo|sleep|retry|backoff|timeout)\b", 2),   # perf-sensitive control
]
```

Two structural multipliers apply on top of the regex score (computed from the
indices built in Phase 1):

- **Fan-in multiplier**: files imported by many others (core modules) get
  `+2` per importer above a threshold — design issues in a core module ripple.
  (`state.dependency_index` inverted: count how many files list `f`.)
- **Size multiplier**: files above a line-count threshold get `+3` — long files
  are the canonical god-object smell.

The promotion threshold (§6.3) is calibrated to this scale, just as
`_PROMOTION_SCORE_THRESHOLD = 8` is calibrated to the attack-surface scale.

---

## 5. Phase 1 — Inventory & repo profile

Identical mechanics to `/audit` Phase 1:

1. `_resolve_scope` → `AuditScope` (tracked files, focus filter, mandatory set).
2. `_load_file_contents` via `git cat-file --batch`.
3. `_build_context_indices` → import/caller/symbol spans.
4. `_order_by_review_surface` (new scoring, §4) → `queued_files`, `review_scores`.
5. LLM repo profile with a **design-oriented schema**.

### 5.1 Repo profile schema

```python
_REVIEW_PROFILE_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="profile",
        required=("language", "summary"),
        repeated={
            "language": "languages",
            "framework": "frameworks",
            "entry_point": "entry_points",
            "core_module": "core_modules",       # high fan-in files
            "public_api": "public_apis",         # exported surface
            "module_boundary": "module_boundaries",
            "state_layer": "state_layers",        # persistence / mutable global state
            "concurrency_surface": "concurrency_surfaces",
            "abstraction_boundary": "abstraction_boundaries",
            "hot_path": "hot_paths",              # perf-sensitive regions / request loops
        },
    ),
    cardinality="one",
    allow_none=False,
)
```

The system prompt mirrors `_PHASE1_SYSTEM` word-for-word in structure: "This
phase does not find bugs. Its only job is to extract reusable repository facts."
The fields shift from security (`trust_boundary`, `auth_surface`,
`dangerous_operation`) to design (`core_module`, `public_api`,
`module_boundary`, `abstraction_boundary`). Promotion rules (§6.3) consume
`core_modules` and `entry_points` the way `/audit` consumes `trust_boundaries`
and `entry_points`.

---

## 6. Phase 2 — Triage

Per-file LLM triage, precision over recall, exactly as `_phase2_triage_one`.
Output: `REVIEW_HIGH`, `REVIEW_LOW`, or `SKIP`. Promotion rules override SKIPs;
a confirmation pass recovers low-confidence SKIPs.

### 6.1 Triage schema

```python
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
```

### 6.2 Triage system prompt (retargeted scope)

The prompt mirrors `_PHASE2_SYSTEM`'s discipline: name three things before
escalating; list review lenses as hints; enumerate explicit out-of-scope cases.

> You are performing phase 2 code-review triage for one committed file with its
> direct local context.
>
> Goal: decide whether this file deserves deep review. Optimize for precision
> over recall. Avoid false positives and confirmation bias.
>
> Allowed priority labels: `REVIEW_HIGH`, `REVIEW_LOW`, `SKIP`.
>
> Scope is **general code review**, across six categories: `design`,
> `consistency`, `flaw`, `smell`, `bug`, `performance` (definitions provided). A
> finding is in scope only when you can name, from the evidence:
> 1. **Subject** — the concrete code element (function, module, boundary, block).
> 2. **Symptom** — the concrete thing wrong with it, observable in the code.
> 3. **Consequence** — the concrete maintenance, correctness, or runtime cost it
>    imposes.
>
> If you cannot name all three, SKIP.
>
> Out of scope and must SKIP:
> - style preferences and bikeshedding with no concrete consequence
> - "I would have structured this differently" without a concrete cost
> - missing tests, missing docs, missing comments (unless the absence breaks a
>   contract)
> - generic hardening / "this should also validate X" / defense-in-depth
> - hypothetical bugs with no concrete trigger path in committed code
> - micro-optimizations and premature optimization with no realistic scaling cost
>
> Review lenses to consider (hints, not sufficient reasons to escalate):
> - responsibility_violation · leaky_abstraction · wrong_indirection
> - circular_or_excessive_coupling · god_object · missing_module
> - divergent_patterns · naming_drift · convention_break
> - wrong_algorithm · broken_invariant · unhandled_edge_case · latent_race
> - dead_code · duplication · long_method · deep_nesting · magic_number
> - complex_conditional · premature_abstraction · unclear_control_flow
> - incorrect_output · unhandled_exception · resource_leak · data_corruption
> - n_plus_one_query · redundant_allocation · wrong_complexity · unbounded_growth
> - hot_path_redundant_work · repeated_decode · quadratic_blowup

### 6.3 Promotion rules

Mirror `_compute_promotion_reasons`. A SKIP is promoted to `REVIEW_LOW` when any
deterministic signal fires:

- **review-surface score ≥ threshold** (calibrated to the §4 scale).
- **Phase 1 entry point** or **core module** (high fan-in) — design issues here
  ripple, so review even if triage was skeptical.
- **One-hop from a core module**: a file a core module depends on, with non-zero
  review score.
- **triage `needs_followup`**.
- **triage infrastructure failure** (LLM/parse error) — promoted and flagged as
  a warning, exactly as in `/audit`.
- **force-review** (`swival.toml`) or **named explicitly in `/review` focus**.

### 6.4 Confirmation pass

`_review_confirm_one` mirrors `_phase2_confirm_one`: a second LLM call on
low-confidence SKIPs that touch a core module or entry point, asking "is there
genuinely nothing worth reviewing here?" Recovered files are tagged
`confirmation_outcome == "promoted"`.

---

## 7. Phase 3 — Deep review

Per candidate file: 3a inventory lists candidate issues; 3b expansion fleshes
each out. Mirrors `_phase3a_inventory` / `_phase3b_expand_one` / `_canonicalize_finding`.

### 7.1 Phase 3a — Inventory schema

```python
_REVIEW3A_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="finding",
        required=("title", "category", "severity", "location", "symptom", "claim"),
        enums={"category": ("design","consistency","flaw","smell","bug","performance"),
               "severity": _SEVERITIES},
        cardinality="zero_or_more",
        allow_none=True,
    ),
)
```

The system prompt mirrors `_PHASE3A_SYSTEM`: either one-or-more `@@ finding @@`
blocks or the literal `@@ none @@` sentinel. Each block carries the
category-specific three-gate evidence (subject→location, symptom, claim). At
most 3 findings per file; every finding must be provable from provided evidence;
prefer the narrowest issue the evidence proves.

### 7.2 Phase 3b — Expansion schema

```python
_REVIEW3B_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="expansion",
        required=(
            "category", "subject", "symptom", "consequence",
            "evidence", "fix_outline",
        ),
        enums={"category": (...)},
        multiline=("evidence",),
    ),
    cardinality="one",
    allow_none=False,
)
```

`evidence` is the propagation/proof field (multiline, like `/audit`'s `proof`):
the exact code path, the line(s) that exhibit the symptom, and why the
consequence follows. `fix_outline` is the smallest correct fix, under 20 words.

### 7.3 Canonicalization & discard

`_canonicalize_review_finding` merges 3a stub + 3b expansion into a
`ReviewFinding` (the `/review` analogue of `FindingRecord`). Discard rules:

- **Out-of-category**: if expansion marks the issue as style/nitpick with no
  consequence, emit `category: out-of-scope` and drop (mirrors
  `_is_out_of_scope_expansion`).
- **Below floor**: a `bug` or `performance` finding below `medium` is dropped
  unless the reproduction/measurement (Phase 4) confirms it — mirrors
  `_SECURITY_CONTROL_FAILURE_MIN_SEVERITIES`.

### 7.4 Scope gate (per category)

Applied in the 3a prompt before emitting any finding, mirroring `/audit`'s
attacker/trigger/gain gate. Each category has its own three-name test:

| Category | Subject | Symptom | Consequence |
|---|---|---|---|
| `design` | the abstraction/module/boundary | what it does wrong (leaks, couples, misplaces responsibility) | the concrete cost to callers or maintainers |
| `consistency` | the divergent structures | how they diverge | the confusion/maintenance cost for the next contributor |
| `flaw` | the logic/invariant | what is incorrect | the edge case or invariant violation it produces |
| `smell` | the code construct | the smell (duplication, length, dead code) | the real maintenance cost (not "ugly") |
| `bug` | the code path | the defect | the incorrect/crashing/corrupting runtime outcome |
| `performance` | the hot path / data structure / query | the inefficiency (wrong complexity class, redundant work, unbounded growth) | the concrete runtime cost (latency, memory, scaling cliff) under realistic input |

If any of the three is missing or vague, emit `@@ none @@`.

---

## 8. Phase 4 — Grounding (first confirmation gate)

This is the core "confirm each finding" mechanism, directly analogous to
`/audit` Phase 4. A finding proposed in Phase 3 is a **hypothesis**. An
independent verifier checks it and emits `CONFIRMED` or `REFUTED` on its own
line. REFUTED findings are discarded.

### 8.1 The `bug` path — worktree reproduction

For `category == "bug"`, reuse `/audit`'s reproduction path verbatim:
`_worktree` + `_make_isolated_loop_kwargs` + an agent loop with
`_PHASE4_VERIFY_SYSTEM`-style instructions. The verifier may inspect code and
compile/run small proof-of-concept code in the isolated worktree.

The verdict prompt is retargeted:

> You are verifying one proposed **bug** finding using the committed source in an
> isolated worktree. Determine whether the finding describes a real defect that
> manifests in practice. Treat the finding as a hypothesis, not ground truth.
>
> A proof counts if you can identify the trigger path, the failing operation or
> violated invariant, and the practical incorrect/crashing/corrupting outcome
> from the code, or demonstrate equivalent runtime evidence.
>
> Reject as REFUTED when the code does not support a practical trigger path, or
> when an existing guard already prevents the defect (reject defense-in-depth).
>
> End your final response with exactly one token on its own line:
> `CONFIRMED` or `REFUTED`.

Verdict parsing mirrors `_parse_verdict_line`; no-verdict is a transient
infrastructure failure (retry), not a negative verdict — same as `/audit`.

### 8.2 The `performance` path — worktree measurement

For `category == "performance"`, the claim is about runtime cost, so it is
verified by *measurement* in the same isolated worktree mechanism (`_worktree` +
`_make_isolated_loop_kwargs` + agent loop). The verifier may inspect the code,
reason about complexity, and run small timing or allocation-count
proof-of-concepts against realistic-sized input.

The verdict prompt is retargeted:

> You are verifying one proposed **performance** finding using the committed
> source in an isolated worktree. Determine whether the finding describes a real
> inefficiency with a concrete runtime cost under realistic input. Treat the
> finding as a hypothesis, not ground truth.
>
> A proof counts if you can identify the inefficient construct, the input shape
> or size that triggers it, and the concrete cost (latency, memory, scaling
> cliff) it produces — either by source-based complexity reasoning grounded in
> the actual code path, or by a small measurement (timing, allocation count)
> against realistic-sized input.
>
> Reject as REFUTED when:
> - the cost only appears at input sizes no realistic caller produces, or
> - an existing bound, cache, or early-exit already caps the cost, or
> - the finding is a micro-optimization or premature optimization with no
>   realistic scaling cost (reject bike-shedding over constant factors).
>
> End your final response with exactly one token on its own line:
> `CONFIRMED` or `REFUTED`.

This gives `performance` a stronger grounding gate than the subjective
categories: the verifier can actually *measure* the claim, not merely re-read
it. Verdict parsing and retry semantics are identical to the `bug` path.

### 8.3 The subjective-category path — evidence-grounded check

For `design`/`consistency`/`flaw`/`smell`, reproduction in a worktree is not the
right tool — the claim is about structure and consequence, not runtime behavior.
Instead the verifier gets the committed evidence bundle (via `_gather_evidence`,
the same callee-context-enriched bundle `/audit` uses) and must independently
confirm the three named facts:

> You are verifying one proposed code-review finding (category: {category}) using
> the committed source. Treat the finding as a hypothesis, not ground truth.
>
> Confirm the finding only if, from the committed evidence, all three hold:
> 1. **Subject is real**: the cited location exists and the described element is
>    as claimed.
> 2. **Symptom is real**: the code at the cited location actually exhibits the
>    described problem (read it; do not trust the proposal's characterization).
> 3. **Consequence is real**: the claimed maintenance/correctness cost genuinely
>    follows from the symptom, under today's code. "Could be argued", "might
>    confuse someone", and "I prefer X" do not qualify.
>
> For `consistency` findings, independently locate the divergent structure the
> finding references; if you cannot find it, REFUTE.
> For `flaw` findings, identify the concrete edge case or invariant violation;
> hypothetical "could break if X" without a code-grounded path is REFUTED.
> For `smell` findings, confirm the smell is non-trivial: the maintenance cost
> must be concrete (e.g. duplication that must be kept in sync, a method too
> long to hold in working memory), not a cosmetic preference.
>
> Reject defense-in-depth and "would be more robust if" arguments as REFUTED.
>
> End with exactly one token on its own line: `CONFIRMED` or `REFUTED`.

This keeps the *two-gate* discipline intact for subjective categories: even
though there is no PoC, the verifier must independently re-read the cited code
and re-derive the consequence. A finding whose "consequence" does not survive an
independent read is REFUTED.

### 8.4 Retry & state

`_review_verify_one` mirrors `_verify_one_finding`: transient errors
(`_TransientVerifierError`) retry once; a confirmed finding becomes a
`ConfirmedFinding` (analogue of `VerifiedFinding`); a refuted finding is
recorded in `verification_state[key] = {"status": "refuted"}`. The finding key
is content-based (`_review_finding_key`, mirroring `_finding_key`) so resume is
stable.

---

## 9. Phase 4.5 — Adjudication (second confirmation gate)

The adversarial refutation panel, directly analogous to
`_phase45_adjudicate` / `_adjudicate_one`. This is where subjective findings are
stress-tested for *significance*: even if a finding is grounded, is it worth
reporting?

### 9.1 Three lenses

Mirror `_PHASE45_LENSES` — three independent LLM calls, each through a lens:

```python
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
```

### 9.2 Verdict schema & default-to-reject

```python
_REVIEW_VERDICT_SCHEMA = PhaseSchema(
    record=RecordSchema(
        name="verdict",
        required=("verdict", "category_fit", "severity", "reason"),
        enums={"verdict": ("real", "false_positive"),
               "category_fit": ("yes", "no"),
               "severity": _SEVERITIES},
    ),
    cardinality="one",
    allow_none=False,
)
```

The system prompt mirrors `_PHASE45_REVIEW_SYSTEM`'s adversarial posture:

> You are adjudicating one already-confirmed code-review finding. A prior phase
> checked it against the evidence and accepted it. Your job is the opposite: try
> to REFUTE this finding. Default to false_positive unless the evidence forces
> otherwise. We would rather drop a real issue than ship noise.
>
> {lens}
>
> A finding is real only if, under today's committed code, all hold:
> - the symptom is actually present at the cited location
> - the consequence is concrete and real (not "could", "might", "I prefer")
> - the category and severity are justified for the realistic consequence
>
> Set category_fit to `no` when the finding is mislabeled (e.g. a style nitpick
> filed as `design`, a non-reproducible concern filed as `bug`, or a constant-
> factor micro-optimization filed as `performance` with no scaling cost).
>
> Recalibrate severity to the realistic maintenance/runtime cost; tie-break
> downward.

### 9.3 Consolidation & demote-only

`_review_consolidate` mirrors `_phase45_consolidate`: produces corrected
title/category/severity/fix_outline from the panel verdicts. Severity is
**demote-only** (`_demote_only` reused verbatim): the panel may lower or keep
severity, never raise it. The panel consensus severity caps the consolidation's
severity, exactly as in `/audit`.

### 9.4 Majority rule

Strict majority of usable verdicts must confirm (`verdict == "real"` **and**
`category_fit == "yes"`). Ties drop (refute-by-default). Fewer than 2 usable
verdicts → keep as-is (a panel that mostly failed has not earned the right to
overrule a confirmed finding). Drops are recorded in
`adjudication_discarded` so the README can explain confirmed-vs-final counts.

---

## 10. Phase 5 — Artifacts

Per final finding, mirror `/audit` Phase 5:

- **Patch** (`_review_patch`): agent loop in a worktree produces the smallest
  correct fix; `git diff` captured. Reuses `_phase5_patch`'s structure
  (pre-seed file tracker, turn budget, error codes). For `design`/`smell`
  findings the "fix" may be a refactor rather than a one-line patch; the prompt
  allows multi-edit refactors but forbids unrelated changes.
- **Report** (`_review_report`): markdown from a template
  (`_REVIEW_REPORT_TEMPLATE`, mirroring `_PHASE5_REPORT_TEMPLATE`) with sections
  Title · Classification (category+severity) · Affected Locations · Summary ·
  Preconditions · Evidence · Why This Is A Real Issue · Fix Requirement · Patch
  Rationale · Residual Risk · Patch.
- **README** (`_render_review_readme`): index table of all final findings,
  mirroring `_render_findings_readme`, grouped by category then severity.

`--regen --finding N` re-runs Phase 5 for one finding; `--resume` continues an
interrupted run — identical UX to `/audit`.

---

## 11. State & resumability

`ReviewRunState` mirrors `AuditRunState` field-for-field with domain renames:

```python
@dataclass
class ReviewRunState:
    run_id: str
    scope: AuditScope
    queued_files: list[str]
    reviewed_files: set[str]
    triage_records: dict[str, ReviewTriageRecord]
    candidate_files: list[str]
    deep_reviewed_files: set[str]
    proposed_findings: list[ReviewFinding]
    confirmed_findings: list[ConfirmedFinding]   # = VerifiedFinding analogue
    repo_profile: dict | None
    import_index / dependency_index / symbol_spans_index / review_scores
    verification_state: dict[str, dict]
    artifact_state: dict[str, dict]
    adjudication_discarded: list[dict]
    truncated_files: dict[str, int]
    phase: str   # init → triage → deep_review → grounding → adjudication → artifacts → done
    metrics: dict[str, int]
    select_all: bool
    ...
```

`save()`/`load()`/`find_resumable()` are structurally identical to
`AuditRunState`'s — atomic temp-file rename, JSON blob, commit+focus matching
for resume. State lives under `.swival/review/<run_id>/state.json`;
artifacts under `review-findings/`.

---

## 12. The confirmation guarantee, summarized

Every finding in the final report has passed **two independent gates**, each
defaulting to reject:

| Gate | Question | Mechanism | Default |
|---|---|---|---|
| Phase 4 — Grounding | "Is the finding actually true against committed code?" | Independent verifier re-reads evidence; `bug` findings reproduced and `performance` findings measured in a worktree. | REFUTED unless CONFIRMED |
| Phase 4.5 — Adjudication | "Is it worth reporting?" | Three-lens adversarial panel, default-to-reject, strict majority. | false_positive unless real |

Plus structural safeguards inherited from `/audit`:
- **Demote-only severity** — the panel can never inflate a finding.
- **Evidence-grounded prompts** — every phase gets committed source + callee
  context; "use only the provided evidence; do not speculate" appears in every
  system prompt.
- **Structured `@@ record @@` parsing with LLM repair** — findings are
  machine-validated before they enter a gate; malformed output triggers a repair
  pass, not a silent accept.
- **Resumable, phase-gated state** — a finding cleared a gate only if
  `verification_state` and the adjudication records say so; resume cannot skip a
  gate.
- **Discard accounting** — `adjudication_discarded` and verification refusals
  are persisted so the README explains the gap between proposed, confirmed, and
  final counts.

This is the same discipline that makes `/audit` trustworthy on security, applied
to the broader question "what is actually wrong with this code, and is it
actually worth fixing?"
