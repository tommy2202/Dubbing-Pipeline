from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class EmbedConfig:
    model: str = "speechbrain/spkrec-ecapa-voxceleb"


def l2_normalize(x):
    import numpy as np  # type: ignore

    x = np.asarray(x, dtype=np.float32).reshape(-1)
    n = float((x**2).sum()) ** 0.5
    if n == 0.0:
        return x
    return x / n


def ecapa_embedding(wav_path: str | Path, device: str = "cpu", cfg: EmbedConfig = EmbedConfig()):
    """
    Return an L2-normalized 1D numpy vector, or None if SpeechBrain isn't available.
    """
    path = Path(wav_path)
    if not path.exists():
        return None

    try:
        import torch  # type: ignore
        from speechbrain.inference.speaker import EncoderClassifier  # type: ignore
    except Exception as ex:
        logger.warning("ECAPA embedding unavailable (%s)", ex)
        return None

    dev = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
    try:
        classifier = EncoderClassifier.from_hparams(source=cfg.model, run_opts={"device": dev})
        # SpeechBrain accepts path
        emb = classifier.encode_file(str(path)).squeeze().detach().cpu().numpy()
        return l2_normalize(emb)
    except Exception as ex:
        logger.warning("ECAPA embedding failed (%s)", ex)
        return None
