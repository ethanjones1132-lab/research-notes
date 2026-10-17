# research-notes

Working notes from building and evaluating AI systems on one machine. Each note states its numbers, its sample sizes and its caveats, including the results that didn't pan out.

| # | Note | One line |
|---|---|---|
| 01 | [Containing a fleet of coding agents](notes/01-containing-a-coding-agent-fleet.md) | An agent escaped its worktree because the CLI read `PWD`, not its cwd; fix plus regression probe; a 12-defect red-team of the merge gate, with what's still open |
| 02 | [Gated-DeltaNet in-projections are Q6-sensitive](notes/02-gdn-in-projections-q6-sensitive.md) | Per-tensor quantization of a hybrid 9B model: Q4 ≈ Q5 ≪ Q6 on the GDN in-projections; final mix 0.044 vs 0.136 mean KLD against the published Q4_K_M (with the selection-overlap caveat) |
| 03 | [A pre-registered, read-once gate for a 270M specialist](notes/03-preregistered-read-once-gate-270m.md) | LoRA specialist beat a hand-written parser on the target question (56.0% vs 7.7%) but regressed elsewhere: **NOT PASS** |

## Code

- `tools/rh.py`: the coordinator's gate and merge toolbelt (Python stdlib). Covers worktree-per-run dispatch, squash plus exact-SHA gating, secret and test-weakening checks, deferred checks on harness changes, an OS-held lock and process identity.
- `tools/test_rh.py`: 39 regression tests (`python -I tools/test_rh.py`).
- `probes/containment_probe.sh`: dispatches a real worker that is told to escape, then reports what got through.

Built with AI assistance (Claude as coordinator; GPT-6.1 Sol and Grok 4.7 as independent reviewers). Claims were checked against run logs, reflogs and test output.
