import pathlib

from anime_v1.utils import logger


def run(video: pathlib.Path, audio: pathlib.Path, ckpt_dir: pathlib.Path):
    """
    v1 compatibility wrapper for Tier-3A lipsync plugin.
    """
    try:
        from config.settings import get_settings

        from anime_v2.plugins.lipsync.base import LipSyncRequest
        from anime_v2.plugins.lipsync.registry import resolve_lipsync_plugin

        s = get_settings()
        plugin = resolve_lipsync_plugin(
            "wav2lip",
            wav2lip_dir=getattr(s, "wav2lip_dir", None),
            wav2lip_checkpoint=getattr(s, "wav2lip_checkpoint", None),
        )
        if plugin is None or not plugin.is_available():
            logger.info("Wav2Lip not available; skipping.")
            return None
        out = pathlib.Path(ckpt_dir) / "lipsynced.mp4"
        req = LipSyncRequest(
            input_video=pathlib.Path(video),
            dubbed_audio_wav=pathlib.Path(audio),
            output_video=pathlib.Path(out),
            work_dir=pathlib.Path(ckpt_dir) / "tmp_lipsync",
            face_mode=str(getattr(s, "lipsync_face", "auto")).lower(),
            device=str(getattr(s, "lipsync_device", "auto")).lower(),
            timeout_s=int(getattr(s, "lipsync_timeout_s", 1200)),
        )
        outp = plugin.run(req)
        logger.info("Lip-sync complete → %s", outp)
        return pathlib.Path(outp)
    except Exception as ex:  # pragma: no cover
        logger.warning("Wav2Lip failed (%s)", ex)
        return None
