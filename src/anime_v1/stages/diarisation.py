import pathlib, time
from anime_v1.utils import logger, checkpoints

try:
    from pyannote.audio import Pipeline  # type: ignore
except Exception:  # pragma: no cover
    Pipeline = None  # type: ignore


def run(audio_wav: pathlib.Path, ckpt_dir: pathlib.Path, **_):
    out = ckpt_dir / "speaker_segments.json"
    if out.exists():
        logger.info("Diarisation exists, skip.")
        return out
    if Pipeline is None:
        logger.info("pyannote not available; writing placeholder diarisation.")
        meta = {"segments": [{"speaker": "Speaker_1", "start": 0.0, "end": 0.0}]}
        checkpoints.save(meta, out)
        return out
    try:
        logger.info("Running pyannote diarization…")
        # NOTE: Requires a pretrained pipeline and HF token env, omitted here;
        # keep robust fallback above.
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization")
        diar = pipeline(str(audio_wav))
        segs = []
        for turn, _, speaker in diar.itertracks(yield_label=True):
            segs.append({
                "speaker": str(speaker),
                "start": float(turn.start),
                "end": float(turn.end),
            })
        checkpoints.save({"segments": segs}, out)
        return out
    except Exception as ex:  # pragma: no cover
        logger.warning("pyannote failed (%s); writing placeholder diarisation.", ex)
        meta = {"segments": [{"speaker": "Speaker_1", "start": 0.0, "end": 0.0}]}
        checkpoints.save(meta, out)
        return out
