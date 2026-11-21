# Release note — tools bind to a role, and availability became real

**Affects:** every deployment. Two behaviour changes, both visible.

## 1. Which model answers may change

`MODEL_CATALOG` marked a model available from an environment variable:

```python
"available": os.getenv("LLM_BIG_AVAILABLE", "true") not in ("0","false","no")
```

It defaults to **true**. So any deployment that never set that flag advertised
`gpt-oss-20b` as servable and resolved **every reasoning tool** to it — a
22.5 GB model — whether or not anything could run it. The failure was invisible
until a call was made, and then looked like the assistant being broken rather
than a default nobody chose.

Availability is now computed across **every licensed worker**, from the same
accelerator-fit path placement uses, with capacity already held by running
stacks excluded. A model no worker can host is reported unavailable **with the
reason**, and a default never resolves to one.

**What you will see:** on hardware that cannot host `gpt-oss-20b`, tools that
silently pointed at it now point at something that runs. Different model,
different output. That is the fix, not a regression — but check any prompt tuned
against the old one.

## 2. Managed mode binds a ROLE, not a model name

In **Local (managed)** mode a tool now declares which *role* it needs —
generation or reasoning — and the active hardware tier supplies the model.
Change tier and every tool follows, with nothing about the tools edited.

**A `local_model` pin no longer governs a managed tool.** It is remembered, and
still shown in the Advanced picker, but managed means the tier decides.
Overriding is done in **Advanced**, where the surface states plainly that MOD
will not check your choice can be served and will not re-point it when the tier
changes.

**What you will see:** if you had pinned a model on a tool in Local mode, that
tool now uses the tier's model for its role. To keep the pin, move that tool to
Advanced.

**Where a tier serves no such role** — `minimal` has no reasoner — a tool bound
to it is reported unresolved, naming both ways out, and answers from a fallback
in the meantime rather than failing.

## 3. The model store holds language models

Uploads now declare their kind (`embedding` or `llm`). Validation differs by
kind: the old check demanded `config.json` plus safetensors, which a GGUF does
not carry, so one validator over both kinds rejected valid language models.

Nothing already stored moved. Existing objects keep the `embeddings/` prefix and
are typed as embeddings, which is what they are.

**The Models tab is now revealed only when a tool is in Advanced mode**, because
managing models is meaningful only when you are choosing them.

## Nothing to do if

You configured your tools explicitly and none of them resolved to a model your
hardware cannot serve. In that case only the wording changes.
