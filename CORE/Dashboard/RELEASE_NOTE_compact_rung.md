# Release note — the `compact` tier now reasons, and changes model

**Affects:** deployments running `MANAGED_OLLAMA_PRESET=compact`.
**Breaking on NVIDIA.** Read before upgrading.

## What changed

`compact` previously served **Devstral-Small-2 24B (AWQ INT4) on vLLM** as its
generator, and inherited a **gpt-oss-20b reasoner needing 22.5 GB** — on a rung
named for a 16 GB card. That reasoner could never be placed there: not on the
card the rung names, and not on a second one either, because the model alone
exceeds the floor. The reasoning that rung appeared to offer never existed. It
was a stack that provisioned, waited, and never ran.

`compact` now serves a **measured pair on one card**, both GGUF on llama.cpp:

| role | model | vendor | floor |
|---|---|---|---|
| generator | `granite-3.3-8b-instruct` Q4_K_M | IBM (US) | 9.0 GB |
| reasoner | `Phi-4-mini-reasoning` Q4_K_M | Microsoft (US) | 5.5 GB |

Measured on an RTX 5000 Ada at a 16K window, one slot, peak under a ~12k-token
prompt: **13958 MiB with both resident** — 12.72 GiB attributable, against a
15.0 GB declared floor.

## What breaks

- **A different model answers.** Devstral 24B → granite 8B for generation.
  Expect different output; re-check any prompt tuned against Devstral.
- **Weights re-download.** Different model and different serving stack, so the
  first start pulls ~7.4 GB. Subsequent starts load from cache in seconds.
- **Vision is gone.** Devstral accepts images; granite-3.3-8b does not. **No
  code path in the platform currently sends images to an LLM**, so nothing
  regresses today — but if you have built one, this tier will ignore them.
- **~2.4 GiB of a 16 GB card is left** for accelerated node tasks with both
  models resident. If this box also runs GPU node work, that is the trade.

## What you gain

- **Reasoning on a 16 GB card**, which was previously impossible at any tier
  below `balanced`.
- **AMD, Intel and Vulkan reach.** AWQ-Marlin is CUDA-only, so the old `compact`
  was NVIDIA-only in fact while reading as general-purpose. GGUF carries no
  Marlin dependency. Those vendors ship **unverified** — offered because the
  configuration is identical, not because they have been run here.
- **No published host ports.** The stacks join the platform network under the
  aliases each role already used (`ollama`, `vllm-big`), so consumers need no
  change and nothing can collide with the API's own port.

## If you want the old behaviour

Move to **`balanced`**, which still serves Devstral-Small-2 on vLLM. Note its
real requirement: **two cards of 22.5 GB**, one per model — the generator and
reasoner are placed independently. Settings → AI Configuration now states each
tier's card count and total demand against your fleet before you select it.

## Nothing to do if

You are on `cpu`, `minimal`, `balanced` or `production`. Those rungs are
unchanged; only their *reporting* improved.
