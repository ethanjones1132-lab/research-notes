# A pre-registered, read-once gate for a 270M function-calling specialist (result: NOT PASS)

*Ethan Jones · September 2026 · source repo is private; file and commit references are to it, and excerpts are available on request*

## 1. TL;DR

- The Freeze 3 gate (`freeze3-v3`) returns **NOT PASS**: both pass buckets (`implicature`, `paraphrase`) came back **inconclusive**, and both no-regression buckets (`canonical`, `regex`) **FAIL**.
- On implicature the specialist reached **56.0%** vs the parser's **7.7%**; on paraphrase **60.6%** vs **12.1%**. The paired freeze bar (lower bound > 0) cleared decisively (**+24.0** and **+27.3**); the ship bar (point ≥ 0.80) was missed by a wide margin.
- The adapter, not just the grammar, does the work: on both pass buckets parser < base+grammar < specialist. Held-out was read **exactly once**, under a rule registered before generation.

## 2. Question, arms, predicate, unit, statistics

Freeze 3 asks whether a tiny single-tool specialist can infer an *unstated* `rg_search` target better than a hand-written parser. Three arms: **parser** (`parserEmitter`, deterministic, no LM); **base+grammar** (un-adapted FunctionGemma 270M under the same grammar, zero-shot); **specialist** (FunctionGemma 270M + trainraw LoRA, serve-normalized, P8h-2/P8k). Inference was `--temp 0` greedy under `grammars/rg_search.gbnf`.

Predicate `traceAccepted`: accepted iff **legal ∧ recall** (a `mustHit` target retrieved) **∧ precision** (no `mustNotHit`) **∧ breadth** (distinct files matched ≤ 20). Unit: **seed-pass-fraction**. Statistics: **paired seed-clustered bootstrap** on (specialist − parser), level **0.95**, **10,000** resamples, seed 1. Registered parser baselines under the shared L1: implicature 0.0769 (2/26), paraphrase 0.1237 (12/97), canonical 1.0000 (25/25), regex 0.9524 (20/21). Freeze bar = paired lower bound > 0; ship bar = specialist point ≥ 0.80.

## 3. Pre-registration and read-once discipline

The gate was registered before any treated outcome (`freeze3-v3`, re-registered at `6efa573` when the gate buckets moved onto vendored real repos at goldCommit `b54d7eb`). `runtime/gate-rules.ts` `GATE_CONFIG` is authoritative; prose is not.

`eval/read-log.json` records every held-out read with a class: **`selection`** (a decision was conditioned on the read; stales the gate) or **`verification`** (an assertion checks an already-fixed, pre-registered constant). The gate run's entry was appended **before** generation, classed `selection`, at commit `d2c99ff`; and only `d2c99ff` was added to `GATE_CONFIG.allowedHeldoutReads`. `staleHeldoutReads` is fail-closed — an unclassed read is treated as selection, so omitting the class cannot exempt it — and once the entry exists, any later `gate.ts` at another commit exits **3**. Held-out was read **once**; this gate is that read. Re-running requires a new registration and a visible config edit. The log is a log, not a permission system: its value is completeness, and judging a read "harmless" is exactly the judgment it exists to audit.

## 4. Results

Per bucket (n = L2 reach; seed point and 95% CI):

| bucket | arm | n | point | 95% CI |
|---|---|---:|---:|---|
| implicature | parser | 26 | 7.7% | [0.00, 0.19] |
| implicature | base+grammar | 26 | 34.6% | [0.15, 0.54] |
| implicature | specialist | 25 | 56.0% | [0.36, 0.76] |
| paraphrase | parser | 97 | 12.1% | [0.03, 0.24] |
| paraphrase | base+grammar | 97 | 39.4% | [0.24, 0.58] |
| paraphrase | specialist | 97 | 60.6% | [0.45, 0.76] |
| canonical | parser | 25 | 100.0% | [1.00, 1.00] |
| canonical | base+grammar | 25 | 55.6% | [0.22, 0.89] |
| canonical | specialist | 25 | 88.9% | [0.67, 1.00] |
| regex | parser | 21 | 95.2% | [0.86, 1.00] |
| regex | base+grammar | 18 | 44.4% | [0.22, 0.67] |
| regex | specialist | 17 | 88.2% | [0.71, 1.00] |

Paired (specialist − parser), seed-clustered:

| bucket | diff | 95% lower | freeze (>0) | ship (≥0.80) | outcome |
|---|---:|---:|---|---|---|
| implicature | +48.0 | **+24.0** | PASS | miss (56.0%) | **inconclusive** |
| paraphrase | +48.5 | **+27.3** | PASS | miss (60.6%) | **inconclusive** |

No-regression (specialist vs registered parser baseline):

| bucket | baseline | specialist | outcome |
|---|---:|---:|---|
| canonical | 100.00% | 88.0% (seed 88.9%) | **FAIL** |
| regex | 95.24% | 88.2% (seed 88.2%) | **FAIL** |

Guardrails: illegal regex = 0. The held-out `mustNotHit` violations (60) and refuse precision (36.9%) are **fallback-L1 frozen** figures from `scores.json`, not per-arm — they are not a specialist measurement and must not be read as one.

## 5. Verdict and interpretation

**NOT PASS.** Quoting the record faithfully: "The freeze bar cleared decisively on both pass buckets… a 270M model with the schema in its decoder beats a hand-written parser at inferring an unstated search target." The control arm earns its place: roughly half the gain is any constrained small LM (base+grammar), roughly half the LoRA on top.

But "Not shippable at the registered bar" (56.0 / 60.6 against 0.80), and "Not a drop-in replacement" — both no-regression checks failed; the parser is perfect on canonical and near-perfect on regex, and the specialist gives back 12 and 7 points there. And "Generalization is not demonstrated": the pooled LORO re-read found cross-repo transfer **undetected and bounded** (implicature +2.9, CI [−17.1, 22.9]), while having trained on the repo was worth +23.5 / +36.4 paired-significant, so the measured advantage is "largely repo-specific learning." Held-out uses the same four vendored repos as train, so the gate alone cannot separate capability from fixture memorization.

Context: dev read 67.6% / 81.8%; held-out 56.0 / 60.6 — the gap is what selecting on dev was worth. The dev frontier 20-shot ceiling was 87.5 / 93.8; no held-out ceiling was measured, so the specialist's distance from what this split admits is unknown. The gate is now spent.

What it does **not** establish: shippability, parser replacement, or generalization.

## 6. Lessons for eval integrity (DECISIONS.md)

- **Class reads by mechanism, not label.** A verification read is benign only because the L2 emitter seam is arm-agnostic, so any bias is common-mode and cancels in the paired statistic. The test: if a fix could move one arm relative to another, it is selection.
- **Fail closed.** Unclassed reads count as selection; log the read *before* taking it, so a crash mid-generation still spends it.
- **Make the control as strong as possible.** A prompt artifact that suppresses `base+grammar` *inflates* the specialist margin rather than making it conservative.
- **Use paired seed-clustered comparisons.** Comparing marginal intervals was the same error the repo corrected twice.
- **Register bars before the number exists;** amend the *form* of a rule from a baseline, never the level.
- **Don't iterate on the same small held-out set;** spend selection on dev.
