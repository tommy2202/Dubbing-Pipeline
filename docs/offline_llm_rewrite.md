## Offline LLM rewrite / transcreation (Feature M)

This repo supports an **optional, offline-only** rewrite hook used during **timing-fit**. It is **OFF by default** and the pipeline always falls back to the deterministic heuristic fitter.

### What it does
- For each segment, the pipeline can optionally ask a **local** LLM to rewrite the translated line to better fit the segment time budget.
- It then still runs the normal deterministic `timing-fit` as a safety net.
- It records per-segment provenance to:
  - `Output/<job>/analysis/rewrite_provider.jsonl`

### Safety rules
- **No internet calls are allowed.**
- If using an HTTP endpoint, it must be **localhost-only** (`localhost`, `127.0.0.1`, or `::1`).
- With `--rewrite-strict` enabled, generation settings are conservative (low temperature, bounded tokens).
- If the local LLM is unavailable or returns invalid output (e.g., missing required glossary terms), the pipeline **falls back** to heuristic timing-fit.

### How to enable (CLI)
You must enable `--timing-fit` and opt in to the provider:

```bash
anime-v2 Input/Test.mp4 \
  --timing-fit \
  --rewrite-provider local_llm \
  --rewrite-endpoint http://127.0.0.1:8080/completion \
  --rewrite-strict
```

### Endpoint formats supported
- llama.cpp legacy endpoint: `http://127.0.0.1:8080/completion`
- OpenAI-compatible chat endpoint: `http://127.0.0.1:8080/v1/chat/completions`

### Verification
- Heuristic path always:

```bash
python3 scripts/verify_rewrite_provider.py
```

- To test an endpoint, set:
  - `REWRITE_ENDPOINT=http://127.0.0.1:8080/completion`
  - then run the same script.

