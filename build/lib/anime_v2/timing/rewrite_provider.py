from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from anime_v2.timing.fit_text import FitStats, fit_translation_to_time
from anime_v2.utils.io import atomic_write_text


class RewriteProvider(Protocol):
    """
    Optional offline rewrite/transcreation provider hook.

    Implementations MUST be offline-only (no remote calls).
    """

    name: str

    def is_available(self) -> bool: ...

    def rewrite(
        self,
        *,
        text: str,
        target_seconds: float,
        constraints: dict[str, Any],
        context: dict[str, Any],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class RewriteAttempt:
    provider_requested: str
    provider_used: str
    original: str
    heuristic_fit: str
    heuristic_stats: FitStats
    llm_rewrite: str | None
    llm_fit: str | None
    llm_stats: FitStats | None
    chosen: str  # heuristic|llm
    reason: str
    target_s: float
    tolerance: float
    wps: float
    constraints: dict[str, Any]
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "provider_requested": self.provider_requested,
            "provider_used": self.provider_used,
            "original": self.original,
            "heuristic_fit": self.heuristic_fit,
            "heuristic_stats": self.heuristic_stats.to_dict(),
            "llm_rewrite": self.llm_rewrite,
            "llm_fit": self.llm_fit,
            "llm_stats": (self.llm_stats.to_dict() if self.llm_stats else None),
            "chosen": self.chosen,
            "reason": self.reason,
            "target_s": float(self.target_s),
            "tolerance": float(self.tolerance),
            "wps": float(self.wps),
            "constraints": dict(self.constraints),
            "context": dict(self.context),
        }


class HeuristicRewriteProvider:
    name = "heuristic"

    def is_available(self) -> bool:
        return True

    def rewrite(
        self,
        *,
        text: str,
        target_seconds: float,
        constraints: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        # For heuristic provider, we do not attempt semantic rewrite; return original text and let
        # fit_translation_to_time do deterministic shortening.
        return str(text or "").strip()


def _is_localhost_url(url: str) -> bool:
    try:
        u = urllib.parse.urlparse(str(url))
        host = (u.hostname or "").strip().lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        return False
    except Exception:
        return False


def _strip_code_fences(s: str) -> str:
    t = str(s or "").strip()
    # remove trivial ``` wrappers
    if t.startswith("```") and t.endswith("```"):
        t = t.strip("`").strip()
    return t.strip()


def _contains_all_required_terms(text: str, required_terms: list[str]) -> bool:
    if not required_terms:
        return True
    hay = " ".join(str(text or "").lower().split())
    for term in required_terms:
        tt = " ".join(str(term or "").lower().split())
        if tt and tt not in hay:
            return False
    return True


class LocalLLMRewriteProvider:
    """
    Optional local LLM rewrite provider.

    Supported backends (offline only):
    - llama.cpp server endpoint (localhost only)
    - transformers local model path (optional import)
    """

    name = "local_llm"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model_path: str | Path | None = None,
        strict: bool = False,
        timeout_s: int = 30,
        max_new_tokens: int = 180,
    ) -> None:
        self.endpoint = str(endpoint or "").strip()
        self.model_path = Path(model_path).resolve() if model_path else None
        self.strict = bool(strict)
        self.timeout_s = int(timeout_s)
        self.max_new_tokens = int(max(32, min(512, max_new_tokens)))

    def is_available(self) -> bool:
        if self.endpoint:
            return _is_localhost_url(self.endpoint)
        if self.model_path is not None:
            return self.model_path.exists()
        return False

    def _prompt(
        self,
        *,
        text: str,
        target_seconds: float,
        constraints: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        required_terms = constraints.get("required_terms") or []
        if not isinstance(required_terms, list):
            required_terms = []
        required_terms = [str(x) for x in required_terms if str(x).strip()]
        hint = str(context.get("context_hint") or "").strip()
        # Keep prompt small and deterministic-ish.
        return (
            "Rewrite the line to fit the time budget while preserving meaning.\n"
            "Rules:\n"
            "- Keep names/terms exactly when provided.\n"
            "- Keep the same language as the input.\n"
            "- Output ONLY the rewritten line (no quotes, no explanations).\n"
            f"- Time budget: {float(target_seconds):.2f} seconds.\n"
            f"- Required terms: {', '.join(required_terms) if required_terms else '(none)'}\n"
            + (f"- Context: {hint}\n" if hint else "")
            + "\n"
            f"Input: {str(text or '').strip()}\n"
            "Output:"
        )

    def _call_llamacpp_completion(self, prompt: str) -> str:
        """
        llama.cpp legacy /completion endpoint.
        """
        url = self.endpoint
        if not url:
            raise RuntimeError("rewrite endpoint not set")
        if not _is_localhost_url(url):
            raise RuntimeError("rewrite endpoint must be localhost-only")

        payload = {
            "prompt": prompt,
            "n_predict": int(self.max_new_tokens),
            "temperature": 0.0 if self.strict else 0.2,
            "top_p": 1.0 if self.strict else 0.95,
            "top_k": 1 if self.strict else 40,
            "stop": ["\n"],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=float(self.timeout_s)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict) and "content" in data:
            return str(data.get("content") or "")
        if isinstance(data, dict) and "response" in data:
            return str(data.get("response") or "")
        return str(data)

    def _call_openai_compat_chat(self, prompt: str) -> str:
        """
        OpenAI-compatible /v1/chat/completions style endpoints (local only).
        """
        url = self.endpoint
        if not url:
            raise RuntimeError("rewrite endpoint not set")
        if not _is_localhost_url(url):
            raise RuntimeError("rewrite endpoint must be localhost-only")

        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": "You are a precise rewriting assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0 if self.strict else 0.2,
            "top_p": 1.0 if self.strict else 0.95,
            "max_tokens": int(self.max_new_tokens),
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=float(self.timeout_s)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        try:
            return str(data["choices"][0]["message"]["content"])
        except Exception:
            return str(data)

    def _rewrite_via_endpoint(self, prompt: str) -> str:
        url = str(self.endpoint).strip()
        if re.search(r"/completion/?$", url):
            return self._call_llamacpp_completion(prompt)
        return self._call_openai_compat_chat(prompt)

    def _rewrite_via_transformers(self, prompt: str) -> str:
        if self.model_path is None:
            raise RuntimeError("rewrite model path not set")
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        except Exception as ex:
            raise RuntimeError(f"transformers not installed: {ex}") from ex

        tok = AutoTokenizer.from_pretrained(str(self.model_path), local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(str(self.model_path), local_files_only=True)
        inputs = tok(prompt, return_tensors="pt")
        # Deterministic-ish decode (no sampling when strict)
        gen = model.generate(
            **inputs,
            do_sample=False if self.strict else True,
            temperature=0.0 if self.strict else 0.2,
            max_new_tokens=int(self.max_new_tokens),
            eos_token_id=getattr(tok, "eos_token_id", None),
        )
        out = tok.decode(gen[0], skip_special_tokens=True)
        # Return only the tail after "Output:" to reduce prompt echoing
        if "Output:" in out:
            out = out.split("Output:", 1)[1]
        return out

    def rewrite(
        self,
        *,
        text: str,
        target_seconds: float,
        constraints: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        if not self.is_available():
            raise RuntimeError("local_llm provider unavailable (missing endpoint/model)")
        prompt = self._prompt(
            text=text, target_seconds=target_seconds, constraints=constraints, context=context
        )
        if self.endpoint:
            out = self._rewrite_via_endpoint(prompt)
        else:
            out = self._rewrite_via_transformers(prompt)
        return _strip_code_fences(out)


def build_rewrite_provider(
    *,
    name: str,
    endpoint: str | None,
    model_path: str | Path | None,
    strict: bool,
) -> RewriteProvider:
    n = str(name or "heuristic").strip().lower()
    if n in {"heuristic", "default", ""}:
        return HeuristicRewriteProvider()
    if n in {"local_llm", "llm", "local"}:
        return LocalLLMRewriteProvider(endpoint=endpoint, model_path=model_path, strict=bool(strict))
    return HeuristicRewriteProvider()


def fit_with_rewrite_provider(
    *,
    provider_name: str,
    endpoint: str | None,
    model_path: str | Path | None,
    strict: bool,
    text: str,
    target_seconds: float,
    tolerance: float,
    wps: float,
    constraints: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, FitStats, RewriteAttempt]:
    """
    Always returns a fitted string (falls back to pure heuristic).
    """
    original = str(text or "").strip()
    heuristic_fit, hstats = fit_translation_to_time(
        original, float(target_seconds), tolerance=float(tolerance), wps=float(wps), max_passes=4
    )

    req_name = str(provider_name or "heuristic").strip().lower()
    prov = build_rewrite_provider(name=req_name, endpoint=endpoint, model_path=model_path, strict=bool(strict))

    # Default decision: heuristic
    chosen = heuristic_fit
    chosen_stats = hstats
    used = "heuristic"
    reason = "default_heuristic"
    llm_rewrite = None
    llm_fit = None
    llm_stats = None

    if prov.name == "local_llm" and prov.is_available():
        try:
            llm_rewrite = prov.rewrite(
                text=original,
                target_seconds=float(target_seconds),
                constraints=constraints,
                context=context,
            ).strip()
            # Enforce required glossary terms (deterministic guard)
            required_terms = constraints.get("required_terms") or []
            if not isinstance(required_terms, list):
                required_terms = []
            required_terms = [str(x) for x in required_terms if str(x).strip()]
            if not _contains_all_required_terms(llm_rewrite, required_terms):
                raise RuntimeError("rewrite_missing_required_terms")

            llm_fit, llm_stats = fit_translation_to_time(
                llm_rewrite,
                float(target_seconds),
                tolerance=float(tolerance),
                wps=float(wps),
                max_passes=4,
            )

            # Choose LLM path only if it fits at least as well as heuristic and isn't "more compressed" by length.
            limit = float(target_seconds) * (1.0 + float(tolerance))
            h_ok = float(hstats.est_after_s) <= limit + 1e-6
            l_ok = float(llm_stats.est_after_s) <= limit + 1e-6
            h_ratio = (len(original) - len(heuristic_fit)) / max(1.0, float(len(original)))
            l_ratio = (len(original) - len(llm_fit)) / max(1.0, float(len(original)))
            if l_ok and (not h_ok or (l_ratio <= h_ratio + 0.02) or (llm_stats.passes <= hstats.passes)):
                chosen = llm_fit
                chosen_stats = llm_stats
                used = "local_llm"
                reason = "llm_selected"
            else:
                used = "heuristic"
                reason = "llm_rejected"
        except Exception as ex:
            used = "heuristic"
            reason = f"llm_failed:{ex}"

    attempt = RewriteAttempt(
        provider_requested=req_name,
        provider_used=used,
        original=original,
        heuristic_fit=str(heuristic_fit),
        heuristic_stats=hstats,
        llm_rewrite=llm_rewrite,
        llm_fit=llm_fit,
        llm_stats=llm_stats,
        chosen=("llm" if used == "local_llm" else "heuristic"),
        reason=str(reason),
        target_s=float(target_seconds),
        tolerance=float(tolerance),
        wps=float(wps),
        constraints=dict(constraints),
        context=dict(context),
    )
    return chosen, chosen_stats, attempt


def append_rewrite_jsonl(path: Path, row: dict[str, Any]) -> None:
    """
    Append a single JSONL line (atomic best-effort by rewriting file).
    This file is job-local and small; avoids extra deps.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        existing = ""
    line = json.dumps(row, sort_keys=True, ensure_ascii=False)
    blob = (existing if existing.endswith("\n") or existing == "" else existing + "\n") + line + "\n"
    atomic_write_text(path, blob, encoding="utf-8")

