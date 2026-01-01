from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.log import logger


def _cosine(a, b) -> float:
    import numpy as np  # type: ignore

    a = np.asarray(a, dtype=np.float32).reshape(-1)
    b = np.asarray(b, dtype=np.float32).reshape(-1)
    denom = float((a**2).sum() ** 0.5 * (b**2).sum() ** 0.5)
    if denom == 0.0:
        return -1.0
    return float((a * b).sum() / denom)


@dataclass
class Character:
    id: str
    embedding: list[float]
    count: int
    shows: dict[str, int]
    speaker_wavs: list[str]


class CharacterStore:
    """
    Persistent store for per-character voiceprints across scenes/episodes.
    Backed by JSON at data/characters.json.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.characters: dict[str, Character] = {}
        self._next_n = 1

    @classmethod
    def default(cls) -> "CharacterStore":
        return cls(Path("data") / "characters.json")

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        chars = data.get("characters", {})
        self.characters = {}
        for cid, c in chars.items():
            self.characters[cid] = Character(
                id=cid,
                embedding=list(c.get("embedding", [])),
                count=int(c.get("count", 1)),
                shows=dict(c.get("shows", {})),
                speaker_wavs=list(c.get("speaker_wavs", [])),
            )
        self._next_n = int(data.get("next_n", 1))

    def save(self) -> None:
        data = {
            "version": 1,
            "next_n": self._next_n,
            "characters": {
                cid: {
                    "embedding": c.embedding,
                    "count": c.count,
                    "shows": c.shows,
                    "speaker_wavs": c.speaker_wavs,
                }
                for cid, c in self.characters.items()
            },
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _new_id(self) -> str:
        cid = f"SPEAKER_{self._next_n:02d}"
        self._next_n += 1
        return cid

    def match_or_create(self, embedding, show_id: str, thresholds: dict[str, float]) -> str:
        """
        Returns stable character id.
        thresholds: {"sim": float}
        """
        import numpy as np  # type: ignore

        sim_thresh = float(thresholds.get("sim", 0.72))
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)

        best_id = None
        best_sim = -1.0

        # Prefer matching within same show_id, else global.
        candidates = list(self.characters.values())
        show_first = [c for c in candidates if show_id in c.shows]
        rest = [c for c in candidates if show_id not in c.shows]
        for c in show_first + rest:
            try:
                sim = _cosine(emb, c.embedding)
            except Exception:
                continue
            if sim > best_sim:
                best_sim = sim
                best_id = c.id

        if best_id is not None and best_sim >= sim_thresh:
            self.update(best_id, emb)
            self.characters[best_id].shows[show_id] = int(self.characters[best_id].shows.get(show_id, 0)) + 1
            logger.info("CharacterStore match %s sim=%.3f show=%s", best_id, best_sim, show_id)
            return best_id

        cid = self._new_id()
        self.characters[cid] = Character(id=cid, embedding=emb.astype(float).tolist(), count=1, shows={show_id: 1}, speaker_wavs=[])
        logger.info("CharacterStore new %s show=%s", cid, show_id)
        return cid

    def update(self, id: str, embedding) -> None:
        import numpy as np  # type: ignore

        if id not in self.characters:
            return
        c = self.characters[id]
        old = np.asarray(c.embedding, dtype=np.float32)
        new = np.asarray(embedding, dtype=np.float32).reshape(-1)
        n = max(1, int(c.count))
        avg = (old * n + new) / float(n + 1)
        c.embedding = avg.astype(float).tolist()
        c.count = n + 1

    def link_speaker_wav(self, id: str, path: str) -> None:
        if id not in self.characters:
            return
        c = self.characters[id]
        if path not in c.speaker_wavs:
            c.speaker_wavs.append(path)
