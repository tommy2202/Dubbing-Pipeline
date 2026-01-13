from __future__ import annotations

import os
import sys

from dubbing_pipeline.timing.rewrite_provider import fit_with_rewrite_provider


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_heuristic() -> None:
    text = "Well, I do not really want to do that, actually."
    fitted, stats, attempt = fit_with_rewrite_provider(
        provider_name="heuristic",
        endpoint=None,
        model_path=None,
        strict=True,
        text=text,
        target_seconds=1.0,
        tolerance=0.10,
        wps=2.7,
        constraints={"required_terms": []},
        context={"context_hint": "prior line"},
    )
    _assert(isinstance(fitted, str) and fitted.strip(), "heuristic must return non-empty string")
    _assert(attempt.provider_used in {"heuristic", "local_llm"}, "provider_used must be set")
    _assert(attempt.chosen == "heuristic", "heuristic provider should choose heuristic path")
    _assert(stats.passes >= 0, "stats must be present")


def test_local_endpoint_optional() -> None:
    ep = os.environ.get("REWRITE_ENDPOINT", "").strip()
    if not ep:
        print("verify_rewrite_provider: SKIP local_llm (set REWRITE_ENDPOINT to test)", file=sys.stderr)
        return
    text = "Please, do not change the name Demon Slayer Corps."
    fitted, stats, attempt = fit_with_rewrite_provider(
        provider_name="local_llm",
        endpoint=ep,
        model_path=None,
        strict=True,
        text=text,
        target_seconds=2.0,
        tolerance=0.10,
        wps=2.7,
        constraints={"required_terms": ["Demon Slayer Corps"]},
        context={"context_hint": "Character is speaking calmly."},
    )
    _assert(isinstance(fitted, str) and fitted.strip(), "local_llm must return a string (or fallback)")
    _assert(attempt.provider_requested == "local_llm", "provider_requested must reflect request")
    # We accept fallback to heuristic if endpoint fails.
    _assert(attempt.provider_used in {"heuristic", "local_llm"}, "provider_used must be set")


def main() -> int:
    try:
        test_heuristic()
        test_local_endpoint_optional()
    except Exception as ex:
        print(f"verify_rewrite_provider: FAIL: {ex}", file=sys.stderr)
        return 2
    print("verify_rewrite_provider: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

