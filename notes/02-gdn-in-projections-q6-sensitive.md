*Ethan Jones · September 2026 · one model, one 8 GB GPU*

# Gated-DeltaNet in-projections are Q6-sensitive, not Q4-tolerant (Qwen3.8-9B-Distill)

## 1. TL;DR

- Varying only the Gated-DeltaNet in-projection group (`ssm_out`, `ssm_alpha`, `ssm_beta`, `attn_qkv`, `attn_gate`; 240 tensors, 17.59% of the 9.197B parameters) on 20 holdout chunks gives Q4_K = 0.0871 ± 0.0076, Q5_K = 0.0915 ± 0.0079, Q6_K = 0.0549 ± 0.0054. Q4 ≈ Q5 (z ~0.4, noise); Q6 is a −0.032 KLD step (z ~3.4). There is no useful Q5 intermediate.
- That single per-tensor decision moved mean KLD more than adding the imatrix did at stock Q4_K_M (−0.027, ~2σ on the same 20-chunk dump).
- Shipped mix on 50 chunks × 512 = 25,600 tokens: mean KLD 0.0436 ± 0.0030 vs published Q4_K_M 0.1362 ± 0.0068, z ≈ 12.5, at 5.824 GiB (+0.441 GiB vs published).

## 2. Setup

- Model: `empero-ai/Qwen3.8-9B-Distill` (`qwen3_5` architecture), 9.197B parameters. Hybrid: 8 full-attention layers + 24 Gated-DeltaNet linear-attention layers (3:1, `full_attention_interval=4`).
- `ssm_linear` = `ssm_out`, `ssm_alpha`, `ssm_beta`, `attn_qkv`, `attn_gate` on the 24 GDN layers, source dtype f32 (`mamba_ssm_dtype`). 240 tensors, 1,617,793,536 elements, 17.59% of parameters — larger than the entire full-attention block (5.11%) and second only to the FFN (52.54%).
- Hardware: RTX 4060 8 GB / 16 GB RAM. CUDA llama.cpp commit `7339054`.
- KLD reference: BF16 `Qwen3.8-9B-BF16.gguf`. Holdout: `calib/kld-holdout.txt`. Ablations used 20 chunks × 512 = 10,240 tokens; the headline used the first 50 chunks × 512 = 25,600 tokens. Holdout does not overlap the imatrix/shipping corpus.

## 3. The ablation

Everything else held fixed (`token_embd` Q4_K, `output`/`attn_*` Q6_K, `ffn_down` Q5_K, `ffn_gate`/`ffn_up` Q4_K, norms/conv/`ssm_a`/`ssm_dt` F32); only the `ssm_linear` bit-width varied. Mean KLD on 20 chunks:

| `ssm_linear` width | Mean KLD | Δ vs Q4 | z vs Q4 |
|---|---:|---:|---:|
| Q4_K | 0.0871 ± 0.0076 | — | — |
| Q5_K | 0.0915 ± 0.0079 | +0.004 | ~0.4 (noise) |
| Q6_K | 0.0549 ± 0.0054 | −0.032 | ~3.4 |

Q4 and Q5 are statistically indistinguishable; the entire gain sits at the Q5→Q6 step.

## 4. Final mix vs published Q4_K_M

50-chunk KLD (25,600 tokens):

| Arm | GiB | Mean KLD | p99 | Same top-1 |
|---|---:|---:|---:|---:|
| BF16 reference | 17.14 | 0 | 0 | 100 % |
| winner C | 5.824 | 0.0436 ± 0.0030 | 1.084 | 96.65 ± 0.16 % |
| published Q4_K_M | 5.383 | 0.1362 ± 0.0068 | 3.752 | 93.96 ± 0.21 % |

Δ mean KLD 0.0926, SE_diff 0.00743, z ≈ 12.5. p99 3.752 → 1.084 (−71 %); top-1 +2.69 pp. Winner is +0.441 GiB vs published.

The mix: `token_embd` Q4_K · `output` Q6_K · `attn_*` Q6_K · `ffn_down` Q5_K · `ffn_gate` Q5_K · `ffn_up` Q4_K · GDN `ssm_out`/`ssm_alpha`/`ssm_beta`/`attn_qkv`/`attn_gate` Q6_K · norms/conv/`ssm_a`/`ssm_dt` F32. MTP pruned.

## 5. Caveats

- The 50-chunk headline uses the same holdout file whose first 20 chunks selected the mix. It is not a clean held-out confirmation.
- The winner is 0.441 GiB larger than the published Q4_K_M.
- One model only (`qwen3_5` / GDN family); not shown to generalize to other GDN models or to other backends (AWQ, GPTQ, native PyTorch quantization).
- PPL is misleading here: winner PPL 2.417 sits *below* BF16 2.423 on this dump (published 2.455). KLD is the discriminator.
- Literature: a single search (2026-09-02) found no public per-tensor GGUF mix showing this Q4≈Q5≪Q6 pattern on GDN in-projections. That is "not observed elsewhere," not "proven novel." Closest adjacent result is a 2026 W4A16 GDN inversion paper — a different object (weight-only 4-bit with 16-bit activations at the framework level).
- Also: not verified whether the effect is uniform across all 24 GDN layers; the Q4/Q5 arms rest on 20 chunks (small sample; p99 is noisy).

## 6. Reproduce

Mix string: as in §4 above. KLD measured with llama.cpp's standard two-pass workflow (save BF16 logits, then score the candidate):

```
llama-perplexity -m REF.gguf  -f holdout.txt --kl-divergence-base kld-ref.dat
llama-perplexity -m cand.gguf -f holdout.txt --kl-divergence-base kld-ref.dat --kl-divergence
```

Ablations run with `--chunks 20`, `n_ctx=512`; the headline with `--chunks 50`.
