from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _norm_text(s: str) -> str:
    return " ".join(str(s or "").strip().lower().split())


@dataclass(slots=True)
class ContextItem:
    abs_start_s: float
    abs_end_s: float
    speaker: str
    src_text: str
    tgt_text: str


@dataclass(slots=True)
class DedupReport:
    dropped: int
    kept: int

    def to_dict(self) -> dict[str, Any]:
        return {"dropped": int(self.dropped), "kept": int(self.kept)}


class StreamContextBuffer:
    """
    Streaming context buffer used to:
    - provide a best-effort translation context hint across chunk boundaries
    - suppress obvious duplicate ASR segments from overlap windows

    This is intentionally lightweight and fully offline.
    """

    def __init__(self, *, context_seconds: float, max_hint_chars: int = 1200):
        self.context_seconds = float(max(0.0, context_seconds))
        self.max_hint_chars = int(max(200, max_hint_chars))
        self._items: list[ContextItem] = []

    def _prune(self, *, now_s: float) -> None:
        if self.context_seconds <= 0.0:
            self._items = []
            return
        keep_after = float(now_s) - float(self.context_seconds)
        self._items = [it for it in self._items if float(it.abs_end_s) >= keep_after]

    def add_translated_segments(
        self,
        *,
        chunk_start_s: float,
        src_segments: list[dict[str, Any]],
        translated_segments: list[dict[str, Any]],
    ) -> None:
        """
        Record both source and translated text for the current chunk.
        Segments are chunk-relative; we store absolute times.
        """
        if self.context_seconds <= 0.0:
            return

        # Keep index alignment best-effort.
        n = min(len(src_segments), len(translated_segments))
        for i in range(n):
            ss = src_segments[i] if isinstance(src_segments[i], dict) else {}
            tt = translated_segments[i] if isinstance(translated_segments[i], dict) else {}
            try:
                a0 = float(chunk_start_s) + float(ss.get("start", 0.0))
                a1 = float(chunk_start_s) + float(ss.get("end", 0.0))
            except Exception:
                continue
            speaker = str(tt.get("speaker") or ss.get("speaker") or "SPEAKER_01")
            self._items.append(
                ContextItem(
                    abs_start_s=float(a0),
                    abs_end_s=float(max(a0, a1)),
                    speaker=speaker,
                    src_text=str(ss.get("text") or ""),
                    tgt_text=str(tt.get("text") or ""),
                )
            )
        self._prune(now_s=float(chunk_start_s))

    def build_translation_hint(self) -> str:
        """
        Build a compact context hint string for MT/translation.
        Intended to be passed to providers that support prompts (e.g., Whisper initial_prompt).
        """
        if self.context_seconds <= 0.0 or not self._items:
            return ""
        lines: list[str] = []
        for it in self._items[-50:]:
            t = it.tgt_text.strip()
            if not t:
                continue
            lines.append(f"{it.speaker}: {t}")
        hint = "\n".join(lines).strip()
        if len(hint) <= self.max_hint_chars:
            return hint
        return hint[-self.max_hint_chars :].lstrip()

    def dedup_src_segments(
        self,
        *,
        chunk_start_s: float,
        src_segments: list[dict[str, Any]],
        overlap_window_s: float,
    ) -> tuple[list[dict[str, Any]], DedupReport]:
        """
        Drop obvious duplicate ASR segments inside the overlap window by comparing against
        buffered recent source text + time overlap.
        """
        if not src_segments or self.context_seconds <= 0.0 or not self._items:
            return src_segments, DedupReport(dropped=0, kept=len(src_segments))

        ov = float(max(0.0, overlap_window_s))
        if ov <= 0.0:
            return src_segments, DedupReport(dropped=0, kept=len(src_segments))

        # Candidate buffer items: near the start boundary of this chunk.
        boundary_s = float(chunk_start_s)
        cand = [
            it
            for it in self._items
            if float(it.abs_end_s) >= (boundary_s - ov - 0.25)
            and float(it.abs_start_s) <= (boundary_s + ov + 0.25)
        ]
        if not cand:
            return src_segments, DedupReport(dropped=0, kept=len(src_segments))

        kept: list[dict[str, Any]] = []
        dropped = 0
        for s in src_segments:
            if not isinstance(s, dict):
                continue
            try:
                a0 = float(chunk_start_s) + float(s.get("start", 0.0))
                a1 = float(chunk_start_s) + float(s.get("end", 0.0))
            except Exception:
                kept.append(s)
                continue
            # Only consider dropping early segments inside/near the overlap window.
            if float(a0) > (boundary_s + ov + 0.25):
                kept.append(s)
                continue

            nt = _norm_text(str(s.get("text") or ""))
            if not nt:
                kept.append(s)
                continue

            is_dup = False
            for it in cand:
                # time overlap test (absolute)
                ov_s = max(
                    0.0, min(float(a1), float(it.abs_end_s)) - max(float(a0), float(it.abs_start_s))
                )
                if ov_s <= 0.0:
                    continue
                pt = _norm_text(it.src_text)
                if not pt:
                    continue
                if nt == pt or (len(nt) > 10 and (nt in pt or pt in nt)):
                    is_dup = True
                    break

            if is_dup:
                dropped += 1
                continue
            kept.append(s)

        return kept, DedupReport(dropped=dropped, kept=len(kept))
