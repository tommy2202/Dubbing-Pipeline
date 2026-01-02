from __future__ import annotations

import asyncio
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path

from anime_v2.config import get_settings
from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from anime_v2.jobs.limits import get_limits
from anime_v2.jobs.models import Job, JobState, now_utc
from anime_v2.jobs.store import JobStore
from anime_v2.jobs.watchdog import PhaseTimeout, run_with_timeout
from anime_v2.ops.metrics import (
    job_errors,
    jobs_finished,
    pipeline_job_degraded_total,
    pipeline_job_failed_total,
    pipeline_mux_seconds,
    pipeline_transcribe_seconds,
    pipeline_tts_seconds,
    time_hist,
    tts_seconds,
    whisper_seconds,
)
from anime_v2.runtime.scheduler import Scheduler
from anime_v2.stages import audio_extractor, mkv_export, tts
from anime_v2.stages.character_store import CharacterStore
from anime_v2.stages.diarization import DiarizeConfig
from anime_v2.stages.diarization import diarize as diarize_v2
from anime_v2.stages.mixing import MixConfig, mix
from anime_v2.stages.transcription import transcribe
from anime_v2.stages.translation import TranslationConfig, translate_segments
from anime_v2.utils.circuit import Circuit
from anime_v2.utils.embeds import ecapa_embedding
from anime_v2.utils.ffmpeg_safe import extract_audio_mono_16k
from anime_v2.utils.hashio import hash_audio_from_video
from anime_v2.utils.log import logger
from anime_v2.utils.net import install_egress_policy
from anime_v2.utils.time import format_srt_timestamp

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-")


class JobCanceled(Exception):
    pass


def _select_device(device: str) -> str:
    device = (device or "auto").lower()
    if device in {"cpu", "cuda"}:
        return device
    try:
        import torch  # type: ignore

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


MODE_TO_MODEL: dict[str, str] = {
    "high": "large-v3",
    "medium": "medium",
    "low": "small",
}


def _parse_srt_to_cues(srt_path: Path) -> list[dict]:
    from anime_v2.utils.cues import parse_srt_to_cues

    return parse_srt_to_cues(srt_path)


def _assign_speakers(cues: list[dict], diar_segments: list[dict] | None) -> list[dict]:
    from anime_v2.utils.cues import assign_speakers

    return assign_speakers(cues, diar_segments)


def _write_srt(lines: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, line in enumerate(lines, 1):
            st = format_srt_timestamp(float(line["start"]))
            en = format_srt_timestamp(float(line["end"]))
            txt = str(line.get("text", "") or "").strip()
            f.write(f"{i}\n{st} --> {en}\n{txt}\n\n")


class JobQueue:
    def __init__(
        self, store: JobStore, *, concurrency: int = 1, app_root: Path | None = None
    ) -> None:
        self.store = store
        self.concurrency = max(1, int(concurrency))
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._cancel: set[str] = set()
        self._cancel_lock = asyncio.Lock()
        if app_root is not None:
            self.app_root = app_root.resolve()
        else:
            self.app_root = Path(get_settings().app_root).resolve()

    async def start(self) -> None:
        if self._tasks:
            return

        # Enforce OFFLINE_MODE / ALLOW_EGRESS policy for background workers.
        install_egress_policy()

        # Recover unfinished jobs (durable-ish single node)
        # If Scheduler is installed, route recoveries through it so caps/backpressure apply.
        sched = Scheduler.instance_optional()
        for j in self.store.list(limit=1000):
            if j.state in {JobState.QUEUED, JobState.RUNNING}:
                self.store.update(j.id, state=JobState.QUEUED, message="Recovered after restart")
                if sched is not None:
                    try:
                        from anime_v2.runtime.scheduler import JobRecord

                        sched.submit(
                            JobRecord(
                                job_id=j.id,
                                mode=j.mode,
                                device_pref=j.device,
                                created_at=time.time(),
                                priority=100,
                            )
                        )
                    except Exception:
                        await self._q.put(j.id)
                else:
                    await self._q.put(j.id)

        for _ in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._worker()))
        logger.info("JobQueue started (concurrency=%s)", self.concurrency)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def graceful_shutdown(self, *, timeout_s: int = 120) -> None:
        """
        Stop accepting new work (handled by lifecycle/scheduler) and let active tasks finish.
        After timeout, cancel remaining workers.
        """
        # Wait for queued items to be processed (best-effort).
        with suppress(Exception):
            await asyncio.wait_for(self._q.join(), timeout=float(timeout_s))
        # Cancel workers (if any still running, they will be interrupted)
        await self.stop()

    async def enqueue(self, job: Job) -> None:
        self.store.put(job)
        await self._q.put(job.id)

    async def cancel(self, id: str) -> Job | None:
        async with self._cancel_lock:
            self._cancel.add(id)
        j = self.store.update(id, state=JobState.CANCELED, message="Canceled")
        return j

    async def pause(self, id: str) -> Job | None:
        j = self.store.get(id)
        if j is None:
            return None
        if j.state != JobState.QUEUED:
            # cannot pause running/done/failed jobs in this simple implementation
            return j
        return self.store.update(id, state=JobState.PAUSED, message="Paused")

    async def resume(self, id: str) -> Job | None:
        j = self.store.get(id)
        if j is None:
            return None
        if j.state != JobState.PAUSED:
            return j
        return self.store.update(id, state=JobState.QUEUED, message="Resumed")

    async def _is_canceled(self, id: str) -> bool:
        async with self._cancel_lock:
            return id in self._cancel

    async def _check_canceled(self, id: str) -> None:
        if await self._is_canceled(id):
            raise JobCanceled()

    async def _worker(self) -> None:
        while True:
            job_id = await self._q.get()
            try:
                # Pause support (best-effort): if job is paused, requeue and yield.
                j = self.store.get(job_id)
                if j is not None and j.state == JobState.PAUSED:
                    await asyncio.sleep(0.25)
                    await self._q.put(job_id)
                    continue
                await self._run_job(job_id)
            finally:
                self._q.task_done()

    async def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        limits = get_limits()
        sched = Scheduler.instance_optional()

        if await self._is_canceled(job_id):
            self.store.update(
                job_id, state=JobState.CANCELED, progress=0.0, message="Canceled before start"
            )
            return

        # Establish work/log paths before writing logs.
        video_path = Path(job.video_path)
        out_root = Path(get_settings().output_dir).resolve()
        # Optional project output subdir (stored on job.runtime by batch/project submission).
        runtime = dict(job.runtime or {})
        proj_sub = ""
        try:
            proj = runtime.get("project")
            if isinstance(proj, dict):
                proj_sub = str(proj.get("output_subdir") or "").strip().strip("/")
        except Exception:
            proj_sub = ""
        if proj_sub:
            base_dir = (out_root / proj_sub / video_path.stem).resolve()
        else:
            base_dir = (out_root / video_path.stem).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        # Temp artifacts live under Output/<stem>/work/<job_id>/...
        work_dir = (base_dir / "work" / job_id).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        log_path = base_dir / "job.log"
        ckpt_path = base_dir / ".checkpoint.json"
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or {}
        # runtime report fields persisted on the job
        runtime.setdefault("attempts", {})
        runtime.setdefault("fallback_used", {})
        runtime.setdefault("breaker_state", {})
        self.store.update(
            job_id,
            work_dir=str(work_dir),
            log_path=str(log_path),
            output_mkv=str(base_dir / f"{video_path.stem}.dub.mkv"),
            output_srt=str(base_dir / f"{video_path.stem}.translated.srt"),
            runtime=runtime,
        )

        t0 = time.perf_counter()
        settings = get_settings()
        self.store.update(job_id, state=JobState.RUNNING, progress=0.0, message="Starting")
        self.store.append_log(job_id, f"[{now_utc()}] start job={job_id}")

        degraded_marked = False

        def _mark_degraded(reason: str) -> None:
            nonlocal degraded_marked
            try:
                curj = self.store.get(job_id)
                rt = dict((curj.runtime or {}) if curj else runtime)
                rt.setdefault("metadata", {})
                md = rt.get("metadata")
                if not isinstance(md, dict):
                    md = {}
                    rt["metadata"] = md
                if not bool(md.get("degraded")):
                    md["degraded"] = True
                    degraded_marked = True
                    pipeline_job_degraded_total.inc()
                md.setdefault("degraded_reasons", [])
                if isinstance(md["degraded_reasons"], list) and reason:
                    md["degraded_reasons"].append(str(reason))
                self.store.update(job_id, runtime=rt)
            except Exception:
                pass

        try:
            await self._check_canceled(job_id)

            # Compute audio hash once per job (used for cross-job caching)
            audio_hash = None
            try:
                audio_hash = hash_audio_from_video(video_path)
                curj = self.store.get(job_id)
                rt = dict((curj.runtime or {}) if curj else runtime)
                rt["audio_hash"] = audio_hash
                self.store.update(job_id, runtime=rt)
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] audio_hash failed: {ex}")

            # a) audio_extractor.extract (~0.10)
            self.store.update(job_id, progress=0.05, message="Extracting audio")
            self.store.append_log(job_id, f"[{now_utc()}] audio_extractor")
            try:
                wav_guess = work_dir / "audio.wav"
                if wav_guess.exists() and stage_is_done(ckpt, "audio"):
                    wav = wav_guess
                    self.store.append_log(job_id, f"[{now_utc()}] audio_extractor (checkpoint hit)")
                else:
                    if sched is None:
                        wav = run_with_timeout(
                            "audio_extract",
                            timeout_s=limits.timeout_audio_s,
                            fn=audio_extractor.extract,
                            args=(),
                            kwargs={
                                "video": video_path,
                                "out_dir": work_dir,
                                "wav_out": work_dir / "audio.wav",
                                "job_id": job_id,
                            },
                        )
                    else:
                        with sched.phase("audio"):
                            wav = run_with_timeout(
                                "audio_extract",
                                timeout_s=limits.timeout_audio_s,
                                fn=audio_extractor.extract,
                                args=(),
                                kwargs={
                                    "video": video_path,
                                    "out_dir": work_dir,
                                    "wav_out": work_dir / "audio.wav",
                                    "job_id": job_id,
                                },
                            )
                    try:
                        write_ckpt(
                            job_id,
                            "audio",
                            {"audio_wav": Path(str(wav))},
                            {"work_dir": str(work_dir)},
                            ckpt_path=ckpt_path,
                        )
                        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                    except Exception:
                        pass
            except PhaseTimeout as ex:
                job_errors.labels(stage="audio_timeout").inc()
                raise RuntimeError(str(ex)) from ex
            self.store.update(job_id, progress=0.10, message="Audio extracted")
            await self._check_canceled(job_id)

            # Tier-Next A/B: optional music/singing region detection (opt-in; OFF by default).
            analysis_dir = work_dir / "analysis"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            base_analysis_dir = base_dir / "analysis"
            base_analysis_dir.mkdir(parents=True, exist_ok=True)
            music_regions_path_work: Path | None = None
            try:
                if bool(getattr(settings, "music_detect", False)):
                    from anime_v2.audio.music_detect import (
                        analyze_audio_for_music_regions,
                        detect_op_ed,
                        write_oped_json,
                        write_regions_json,
                    )
                    from anime_v2.utils.io import atomic_copy

                    regs = analyze_audio_for_music_regions(
                        Path(str(wav)),
                        mode=str(getattr(settings, "music_mode", "auto") or "auto"),
                        threshold=float(getattr(settings, "music_threshold", 0.70)),
                    )
                    music_regions_path_work = analysis_dir / "music_regions.json"
                    write_regions_json(regs, music_regions_path_work)
                    with suppress(Exception):
                        atomic_copy(music_regions_path_work, base_analysis_dir / "music_regions.json")
                    self.store.append_log(
                        job_id,
                        f"[{now_utc()}] music_detect regions={len(regs)} threshold={float(getattr(settings, 'music_threshold', 0.70)):.2f}",
                    )
                    if bool(getattr(settings, "op_ed_detect", False)):
                        oped = detect_op_ed(
                            Path(str(wav)),
                            music_regions=regs,
                            seconds=int(getattr(settings, "op_ed_seconds", 90)),
                            threshold=float(getattr(settings, "music_threshold", 0.70)),
                        )
                        oped_path = analysis_dir / "op_ed.json"
                        write_oped_json(oped, oped_path)
                        with suppress(Exception):
                            atomic_copy(oped_path, base_analysis_dir / "op_ed.json")
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] music_detect failed: {ex}")

            # Tier-1 A: dialogue isolation + enhanced mixing (opt-in).
            # Defaults preserve behavior (SEPARATION=off, MIX=legacy).
            stems_dir = work_dir / "stems"
            stems_dir.mkdir(parents=True, exist_ok=True)
            audio_dir = work_dir / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            base_stems_dir = base_dir / "stems"
            base_stems_dir.mkdir(parents=True, exist_ok=True)
            base_audio_dir = base_dir / "audio"
            base_audio_dir.mkdir(parents=True, exist_ok=True)

            sep_mode = str(getattr(settings, "separation", "off") or "off").lower()
            mix_mode = str(getattr(settings, "mix_mode", "legacy") or "legacy").lower()
            background_wav: Path | None = None
            if mix_mode == "enhanced":
                if sep_mode == "demucs":
                    try:
                        from anime_v2.audio.separation import separate_dialogue
                        from anime_v2.utils.io import atomic_copy

                        res = separate_dialogue(
                            Path(str(wav)),
                            stems_dir,
                            model=str(getattr(settings, "separation_model", "htdemucs")),
                            device=str(getattr(settings, "separation_device", "auto")),
                        )
                        background_wav = res.background_wav
                        # Persist stable copies (best-effort)
                        with suppress(Exception):
                            atomic_copy(res.dialogue_wav, base_stems_dir / "dialogue.wav")
                            atomic_copy(res.background_wav, base_stems_dir / "background.wav")
                            if res.meta_path.exists():
                                atomic_copy(res.meta_path, base_stems_dir / "meta.json")
                    except Exception as ex:
                        self.store.append_log(
                            job_id,
                            f"[{now_utc()}] separation requested but unavailable; falling back to no separation ({ex})",
                        )
                        background_wav = Path(str(wav))
                else:
                    background_wav = Path(str(wav))

            # b) diarize.identify (~0.25) (optional)
            diar_json_work = work_dir / "diarization.work.json"
            diar_json_public = base_dir / "diarization.json"
            diar_segments: list[dict] = []
            speaker_embeddings: dict[str, str] = {}
            try:
                self.store.update(job_id, progress=0.12, message="Diarizing speakers")
                self.store.append_log(job_id, f"[{now_utc()}] diarize")
                cfg = DiarizeConfig(diarizer=str(settings.diarizer))
                utts = diarize_v2(str(wav), device=_select_device(job.device), cfg=cfg)

                # Tier-Next F: optional scene-aware speaker smoothing (opt-in; default off).
                try:
                    curj = self.store.get(job_id)
                    rt2 = dict((curj.runtime or {}) if curj else runtime)
                    eff_sm = bool(rt2.get("speaker_smoothing")) or bool(
                        getattr(settings, "speaker_smoothing", False)
                    )
                    eff_scene = str(rt2.get("scene_detect") or getattr(settings, "scene_detect", "audio")).lower()
                    if eff_sm and eff_scene != "off":
                        from anime_v2.diarization.smoothing import (
                            detect_scenes_audio,
                            smooth_speakers_in_scenes,
                            write_speaker_smoothing_report,
                        )

                        analysis_dir = work_dir / "analysis"
                        analysis_dir.mkdir(parents=True, exist_ok=True)
                        base_analysis_dir = base_dir / "analysis"
                        base_analysis_dir.mkdir(parents=True, exist_ok=True)

                        scenes = detect_scenes_audio(Path(str(wav)))
                        utts2, changes = smooth_speakers_in_scenes(
                            utts,
                            scenes,
                            min_turn_s=float(getattr(settings, "smoothing_min_turn_s", 0.6)),
                            surround_gap_s=float(getattr(settings, "smoothing_surround_gap_s", 0.4)),
                        )
                        utts = utts2
                        rep_path = analysis_dir / "speaker_smoothing.json"
                        write_speaker_smoothing_report(
                            rep_path,
                            scenes=scenes,
                            changes=changes,
                            enabled=True,
                            config={
                                "scene_detect": eff_scene,
                                "min_turn_s": float(getattr(settings, "smoothing_min_turn_s", 0.6)),
                                "surround_gap_s": float(getattr(settings, "smoothing_surround_gap_s", 0.4)),
                            },
                        )
                        with suppress(Exception):
                            from anime_v2.utils.io import atomic_copy

                            atomic_copy(rep_path, base_analysis_dir / "speaker_smoothing.json")
                        self.store.append_log(
                            job_id,
                            f"[{now_utc()}] speaker_smoothing scenes={len(scenes)} changes={len(changes)}",
                        )
                except Exception:
                    self.store.append_log(job_id, f"[{now_utc()}] speaker_smoothing failed; continuing")

                seg_dir = work_dir / "segments"
                seg_dir.mkdir(parents=True, exist_ok=True)
                by_label: dict[str, list[tuple[float, float, Path]]] = {}
                for i, u in enumerate(utts):
                    s = float(u["start"])
                    e = float(u["end"])
                    lab = str(u["speaker"])
                    seg_wav = seg_dir / f"{i:04d}_{lab}.wav"
                    try:
                        extract_audio_mono_16k(
                            src=Path(str(wav)), dst=seg_wav, start_s=s, end_s=e, timeout_s=120
                        )
                    except Exception:
                        seg_wav = Path(str(wav))
                    by_label.setdefault(lab, []).append((s, e, seg_wav))

                show = str(settings.show_id) if settings.show_id else video_path.stem
                sim = float(settings.char_sim_thresh)
                thresholds = {"sim": sim}
                lab_to_char: dict[str, str] = {}
                # Tier-2A voice memory (optional, opt-in)
                vm_store = None
                vm_map: dict[str, str] = {}
                vm_meta: dict[str, dict[str, object]] = {}
                vm_enabled = bool(getattr(settings, "voice_memory", False))
                if vm_enabled:
                    try:
                        from anime_v2.voice_memory.store import (
                            VoiceMemoryStore,
                            compute_episode_key,
                        )

                        vm_dir = Path(settings.voice_memory_dir).resolve()
                        vm_store = VoiceMemoryStore(vm_dir)
                        # optional manual diar_label -> character_id overrides
                        mp = getattr(settings, "voice_character_map", None)
                        if mp:
                            from anime_v2.utils.io import read_json

                            mdata = read_json(Path(str(mp)), default={})
                            if isinstance(mdata, dict):
                                vm_map = {
                                    str(k): str(v)
                                    for k, v in mdata.items()
                                    if str(k).strip() and str(v).strip()
                                }
                        episode_key = compute_episode_key(
                            audio_hash=audio_hash, video_path=video_path
                        )
                    except Exception as ex:
                        self.store.append_log(
                            job_id, f"[{now_utc()}] voice_memory unavailable: {ex}"
                        )
                        vm_store = None
                        episode_key = ""

                for lab, segs in by_label.items():
                    rep_wav = sorted(segs, key=lambda t: (t[1] - t[0]), reverse=True)[0][2]
                    if vm_store is not None:
                        # voice memory path (offline-first; falls back if embeddings unavailable)
                        try:
                            manual = vm_map.get(lab)
                            if manual:
                                cid = vm_store.ensure_character(character_id=manual)
                                sim_score = 1.0
                                provider = "manual"
                            else:
                                cid, sim_score, provider = vm_store.match_or_create_from_wav(
                                    rep_wav,
                                    device=_select_device(job.device),
                                    threshold=float(
                                        getattr(settings, "voice_match_threshold", 0.75)
                                    ),
                                    auto_enroll=bool(getattr(settings, "voice_auto_enroll", True)),
                                )
                            lab_to_char[lab] = cid
                            vm_meta[lab] = {
                                "character_id": cid,
                                "similarity": float(sim_score),
                                "provider": str(provider),
                                "confidence": float(max(0.0, min(1.0, sim_score))),
                            }
                        except Exception as ex:
                            self.store.append_log(
                                job_id, f"[{now_utc()}] voice_memory match failed ({lab}): {ex}"
                            )
                            lab_to_char[lab] = lab
                    else:
                        # legacy path (CharacterStore)
                        store_chars = None
                        try:
                            store_chars = CharacterStore.default()
                            store_chars.load()
                        except Exception:
                            store_chars = None
                        if store_chars is None:
                            lab_to_char[lab] = lab
                            continue
                        emb = ecapa_embedding(rep_wav, device=_select_device(job.device))
                        if emb is None:
                            lab_to_char[lab] = lab
                            continue
                        cid = store_chars.match_or_create(emb, show_id=show, thresholds=thresholds)
                        store_chars.link_speaker_wav(cid, str(rep_wav))
                        lab_to_char[lab] = cid
                        with suppress(Exception):
                            store_chars.save()

                # Persist episode mapping (best-effort)
                if vm_store is not None and episode_key:
                    with suppress(Exception):
                        vm_store.write_episode_mapping(
                            episode_key,
                            source={
                                "video_path": str(video_path),
                                "audio_hash": str(audio_hash or ""),
                                "show_id": str(show),
                            },
                            mapping=vm_meta,
                        )

                diar_segments = []
                for lab, segs in by_label.items():
                    for s, e, wav_p in segs:
                        diar_segments.append(
                            {
                                "start": s,
                                "end": e,
                                "diar_label": lab,
                                "speaker_id": lab_to_char.get(lab, lab),
                                "wav_path": str(wav_p),
                            }
                        )

                from anime_v2.utils.io import write_json

                # Work version includes wav_path for TTS voice selection.
                write_json(
                    diar_json_work,
                    {
                        "audio_path": str(wav),
                        "segments": diar_segments,
                        "speaker_embeddings": speaker_embeddings,
                    },
                )
                # Public version excludes temp wav paths (workdir is pruned after completion).
                pub_segments = []
                for seg in diar_segments:
                    try:
                        pub_segments.append(
                            {
                                "start": float(seg["start"]),
                                "end": float(seg["end"]),
                                "diar_label": str(seg.get("diar_label") or ""),
                                "speaker_id": str(seg.get("speaker_id") or ""),
                            }
                        )
                    except Exception:
                        continue
                write_json(diar_json_public, {"audio_path": str(wav), "segments": pub_segments})
                self.store.update(
                    job_id,
                    progress=0.25,
                    message=f"Diarized ({len(set(s.get('speaker_id') for s in diar_segments))} speakers)",
                )
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] diarize failed: {ex}")
                self.store.update(job_id, progress=0.25, message="Diarize skipped")
            await self._check_canceled(job_id)

            # c) transcription.transcribe (~0.60)
            mode = (job.mode or "medium").lower()
            model_name = MODE_TO_MODEL.get(mode, "medium")
            device = _select_device(job.device)
            srt_out = work_dir / f"{video_path.stem}.srt"
            # Persist a stable copy in Output/<stem>/ for inspection / playback.
            srt_public = base_dir / f"{video_path.stem}.srt"
            self.store.update(job_id, progress=0.30, message=f"Transcribing (Whisper {model_name})")
            self.store.append_log(
                job_id, f"[{now_utc()}] transcribe model={model_name} device={device}"
            )
            try:
                with time_hist(pipeline_transcribe_seconds) as elapsed:
                    t_wh0 = time.perf_counter()
                    srt_meta = srt_out.with_suffix(".json")
                    if srt_out.exists() and srt_meta.exists() and stage_is_done(ckpt, "transcribe"):
                        self.store.append_log(job_id, f"[{now_utc()}] transcribe (checkpoint hit)")
                    else:
                        if sched is None:
                            transcribe(
                                audio_path=wav,
                                srt_out=srt_out,
                                device=device,
                                model_name=model_name,
                                task="transcribe",
                                src_lang=job.src_lang,
                                tgt_lang=job.tgt_lang,
                                job_id=job_id,
                                audio_hash=audio_hash,
                                word_timestamps=bool(get_settings().whisper_word_timestamps),
                            )
                        else:
                            with sched.phase("transcribe"):
                                transcribe(
                                    audio_path=wav,
                                    srt_out=srt_out,
                                    device=device,
                                    model_name=model_name,
                                    task="transcribe",
                                    src_lang=job.src_lang,
                                    tgt_lang=job.tgt_lang,
                                    job_id=job_id,
                                    audio_hash=audio_hash,
                                    word_timestamps=bool(get_settings().whisper_word_timestamps),
                                )
                        # reflect circuit state into job (only when we actually ran transcribe)
                        try:
                            curj = self.store.get(job_id)
                            rt = dict((curj.runtime or {}) if curj else runtime)
                            rt.setdefault("breaker_state", {})
                            rt["breaker_state"]["whisper"] = Circuit.get("whisper").snapshot().state
                            rt.setdefault("attempts", {})
                            rt["attempts"]["whisper"] = int(rt["attempts"].get("whisper", 0)) + 1
                            self.store.update(job_id, runtime=rt)
                        except Exception:
                            pass
                        try:
                            write_ckpt(
                                job_id,
                                "transcribe",
                                {"srt": srt_out, "meta": srt_meta},
                                {"work_dir": str(work_dir), "model": model_name, "device": device},
                                ckpt_path=ckpt_path,
                            )
                            ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                        except Exception:
                            pass
                    whisper_seconds.observe(max(0.0, time.perf_counter() - t_wh0))
                    dt = elapsed()
                    if dt > float(settings.budget_transcribe_sec):
                        _mark_degraded("budget_transcribe_exceeded")
            except Exception:
                job_errors.labels(stage="whisper").inc()
                raise
            try:
                from anime_v2.utils.io import atomic_copy

                atomic_copy(srt_out, srt_public)
                if srt_out.with_suffix(".json").exists():
                    atomic_copy(srt_out.with_suffix(".json"), srt_public.with_suffix(".json"))
            except Exception:
                pass
            # Prefer rich segment metadata (avg_logprob) when available.
            cues: list[dict] = []
            try:
                from anime_v2.utils.io import read_json

                meta = read_json(srt_out.with_suffix(".json"), default={})
                segs_detail = meta.get("segments_detail", []) if isinstance(meta, dict) else []
                cues = segs_detail if isinstance(segs_detail, list) else []
            except Exception:
                cues = _parse_srt_to_cues(srt_out)
            self.store.update(job_id, progress=0.60, message=f"Transcribed ({len(cues)} segments)")
            await self._check_canceled(job_id)

            # d) translation manager (~0.75) when needed
            # Prefer diarization utterances for timing; assign text/logprob from transcription overlaps.
            diar_utts = sorted(
                [
                    {
                        "start": float(s["start"]),
                        "end": float(s["end"]),
                        "speaker": str(s.get("speaker_id") or "SPEAKER_01"),
                    }
                    for s in diar_segments
                ],
                key=lambda x: (x["start"], x["end"]),
            )

            def _ov(a0, a1, b0, b1) -> float:
                return max(0.0, min(a1, b1) - max(a0, b0))

            segments_for_mt: list[dict] = []
            if diar_utts:
                for u in diar_utts:
                    txt_parts = []
                    lp_parts = []
                    w_parts = []
                    for seg in cues:
                        try:
                            s0 = float(seg["start"])
                            s1 = float(seg["end"])
                            ov = _ov(u["start"], u["end"], s0, s1)
                            if ov <= 0:
                                continue
                            t = str(seg.get("text") or "").strip()
                            if t:
                                txt_parts.append(t)
                            lp = seg.get("avg_logprob")
                            if lp is not None:
                                lp_parts.append(float(lp))
                                w_parts.append(ov)
                        except Exception:
                            continue
                    text_src = " ".join(txt_parts).strip()
                    logprob = None
                    if lp_parts and w_parts and len(lp_parts) == len(w_parts):
                        tot = sum(w_parts)
                        if tot > 0:
                            logprob = (
                                sum(lp * w for lp, w in zip(lp_parts, w_parts, strict=False)) / tot
                            )
                    segments_for_mt.append(
                        {
                            "start": u["start"],
                            "end": u["end"],
                            "speaker": u["speaker"],
                            "text": text_src,
                            "logprob": logprob,
                        }
                    )
            else:
                for seg in cues:
                    try:
                        segments_for_mt.append(
                            {
                                "start": float(seg["start"]),
                                "end": float(seg["end"]),
                                "speaker": "SPEAKER_01",
                                "text": str(seg.get("text") or ""),
                                "logprob": seg.get("avg_logprob"),
                            }
                        )
                    except Exception:
                        continue
            translated_json = base_dir / "translated.json"
            translated_srt = base_dir / f"{video_path.stem}.translated.srt"

            do_translate = job.src_lang.lower() != job.tgt_lang.lower()
            subs_srt_path: Path | None = srt_public
            # Resynthesis path: if transcript edits exist and a resynth was requested,
            # we will skip MT and synthesize only approved segments (others become silence).
            resynth = None
            try:
                curj = self.store.get(job_id)
                rt = dict((curj.runtime or {}) if curj else runtime)
                resynth = rt.get("resynth")
            except Exception:
                resynth = None

            def _apply_transcript_to_translated_json(*, approved_only: bool) -> Path | None:
                try:
                    from anime_v2.utils.io import read_json, write_json

                    store_path = base_dir / "transcript_store.json"
                    if not store_path.exists():
                        return None
                    st = read_json(store_path, default={})
                    seg_over = st.get("segments", {}) if isinstance(st, dict) else {}
                    if not isinstance(seg_over, dict):
                        return None

                    # base segments from translated.json if present; else from translated_srt
                    segments: list[dict] = []
                    if translated_json.exists():
                        data = read_json(translated_json, default={})
                        segs = data.get("segments") if isinstance(data, dict) else None
                        if isinstance(segs, list):
                            segments = [dict(s) for s in segs if isinstance(s, dict)]
                    if not segments and translated_srt.exists():
                        # minimal parse (no speaker)
                        txt = translated_srt.read_text(encoding="utf-8", errors="replace")
                        blocks = [b for b in txt.split("\n\n") if b.strip()]

                        def parse_ts(ts: str) -> float:
                            hh, mm, rest = ts.split(":")
                            ss, ms = rest.split(",")
                            return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

                        for b in blocks:
                            lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
                            if len(lines) < 2 or "-->" not in lines[1]:
                                continue
                            start_s, end_s = (p.strip() for p in lines[1].split("-->", 1))
                            seg_text = " ".join(lines[2:]).strip() if len(lines) > 2 else ""
                            segments.append(
                                {
                                    "start": parse_ts(start_s),
                                    "end": parse_ts(end_s),
                                    "speaker": "SPEAKER_01",
                                    "text": seg_text,
                                }
                            )

                    if not segments:
                        return None

                    out_segments: list[dict] = []
                    for i, s in enumerate(segments, 1):
                        ov = seg_over.get(str(i), {})
                        if isinstance(ov, dict):
                            tgt_text = str(
                                ov.get("tgt_text") if "tgt_text" in ov else (s.get("text") or "")
                            )
                            approved = bool(ov.get("approved"))
                        else:
                            tgt_text = str(s.get("text") or "")
                            approved = False
                        if approved_only and not approved:
                            tgt_text = ""
                        ss = dict(s)
                        ss["text"] = tgt_text
                        out_segments.append(ss)

                    # Tier-Next C: apply PG mode to transcript-edited text (so edits can't bypass PG).
                    try:
                        curj = self.store.get(job_id)
                        rt2 = dict((curj.runtime or {}) if curj else runtime)
                        # Tier-Next E: optional style guide on transcript-edited text (best-effort).
                        try:
                            proj = rt2.get("project") if isinstance(rt2.get("project"), dict) else {}
                            proj_name = str(proj.get("name") or rt2.get("project_name") or "").strip()
                            sg_path = str(rt2.get("style_guide_path") or "").strip()
                            if proj_name or sg_path:
                                from anime_v2.text.style_guide import (
                                    apply_style_guide_to_segments,
                                    load_style_guide,
                                    resolve_style_guide_path,
                                )

                                eff_path = (
                                    Path(sg_path).resolve()
                                    if sg_path
                                    else resolve_style_guide_path(project=proj_name, style_guide_path=None)
                                )
                                if eff_path and Path(eff_path).exists():
                                    guide = load_style_guide(Path(eff_path), project=proj_name)
                                    analysis_dir = work_dir / "analysis"
                                    analysis_dir.mkdir(parents=True, exist_ok=True)
                                    out_jsonl = analysis_dir / "style_guide_applied.jsonl"
                                    out_segments = apply_style_guide_to_segments(
                                        out_segments,
                                        guide=guide,
                                        out_jsonl=out_jsonl,
                                        stage="post_translate",
                                        job_id=str(job_id),
                                    )
                        except Exception:
                            pass
                        eff_pg = str(rt2.get("pg") or "off").strip().lower()
                        eff_pg_policy = str(rt2.get("pg_policy_path") or "").strip()
                        if eff_pg != "off":
                            from anime_v2.text.pg_filter import apply_pg_filter_to_segments

                            analysis_dir = work_dir / "analysis"
                            analysis_dir.mkdir(parents=True, exist_ok=True)
                            report_p = analysis_dir / "pg_filter_report.json"
                            out_segments, _ = apply_pg_filter_to_segments(
                                out_segments,
                                pg=eff_pg,
                                pg_policy_path=(Path(eff_pg_policy).resolve() if eff_pg_policy else None),
                                report_path=report_p,
                                job_id=str(job_id),
                            )
                    except Exception:
                        pass

                    out_json = work_dir / "translated.edited.json"
                    write_json(
                        out_json,
                        {
                            "src_lang": job.src_lang,
                            "tgt_lang": job.tgt_lang,
                            "segments": out_segments,
                        },
                    )
                    # also write an edited SRT for review
                    try:
                        out_srt = base_dir / f"{video_path.stem}.translated.edited.srt"
                        _write_srt(
                            [
                                {
                                    "start": float(s["start"]),
                                    "end": float(s["end"]),
                                    "speaker_id": str(s.get("speaker") or "SPEAKER_01"),
                                    "text": str(s.get("text") or ""),
                                }
                                for s in out_segments
                            ],
                            out_srt,
                        )
                        # prefer edited subtitles for mux when resynth is requested
                        nonlocal subs_srt_path
                        subs_srt_path = out_srt
                    except Exception:
                        pass
                    return out_json
                except Exception:
                    return None

            if do_translate:
                self.store.update(job_id, progress=0.62, message="Translating subtitles")
                self.store.append_log(
                    job_id, f"[{now_utc()}] translate src={job.src_lang} tgt={job.tgt_lang}"
                )
                try:
                    from anime_v2.utils.io import write_json

                    cfg = TranslationConfig(
                        mt_engine=str(settings.mt_engine),
                        mt_lowconf_thresh=float(settings.mt_lowconf_thresh),
                        glossary_path=settings.glossary_path,
                        style_path=settings.style_path,
                        show_id=(str(settings.show_id) if settings.show_id else video_path.stem),
                        whisper_model=model_name,
                        audio_path=str(wav),
                        device=device,
                    )
                    translated_segments = translate_segments(
                        segments_for_mt, src_lang=job.src_lang, tgt_lang=job.tgt_lang, cfg=cfg
                    )
                    # Tier-Next E: optional project style guide (best-effort; OFF by default).
                    try:
                        curj = self.store.get(job_id)
                        rt2 = dict((curj.runtime or {}) if curj else runtime)
                        proj = rt2.get("project") if isinstance(rt2.get("project"), dict) else {}
                        proj_name = str(proj.get("name") or rt2.get("project_name") or "").strip()
                        sg_path = str(rt2.get("style_guide_path") or "").strip()
                        if proj_name or sg_path:
                            from anime_v2.text.style_guide import (
                                apply_style_guide_to_segments,
                                load_style_guide,
                                resolve_style_guide_path,
                            )

                            eff_path = (
                                Path(sg_path).resolve()
                                if sg_path
                                else resolve_style_guide_path(project=proj_name, style_guide_path=None)
                            )
                            if eff_path and Path(eff_path).exists():
                                guide = load_style_guide(Path(eff_path), project=proj_name)
                                analysis_dir = work_dir / "analysis"
                                analysis_dir.mkdir(parents=True, exist_ok=True)
                                base_analysis_dir = base_dir / "analysis"
                                base_analysis_dir.mkdir(parents=True, exist_ok=True)
                                out_jsonl = analysis_dir / "style_guide_applied.jsonl"
                                translated_segments = apply_style_guide_to_segments(
                                    translated_segments,
                                    guide=guide,
                                    out_jsonl=out_jsonl,
                                    stage="post_translate",
                                    job_id=str(job_id),
                                )
                                with suppress(Exception):
                                    from anime_v2.utils.io import atomic_copy

                                    atomic_copy(out_jsonl, base_analysis_dir / "style_guide_applied.jsonl")
                    except Exception:
                        self.store.append_log(job_id, f"[{now_utc()}] style_guide failed; continuing")
                    # Tier-Next C: per-job PG mode (opt-in; OFF by default), before timing-fit/TTS/subs.
                    try:
                        curj = self.store.get(job_id)
                        rt2 = dict((curj.runtime or {}) if curj else runtime)
                        eff_pg = str(rt2.get("pg") or "off").strip().lower()
                        eff_pg_policy = str(rt2.get("pg_policy_path") or "").strip()
                        if eff_pg != "off":
                            from anime_v2.text.pg_filter import apply_pg_filter_to_segments

                            analysis_dir = work_dir / "analysis"
                            analysis_dir.mkdir(parents=True, exist_ok=True)
                            base_analysis_dir = base_dir / "analysis"
                            base_analysis_dir.mkdir(parents=True, exist_ok=True)
                            report_p = analysis_dir / "pg_filter_report.json"
                            translated_segments, _ = apply_pg_filter_to_segments(
                                translated_segments,
                                pg=eff_pg,
                                pg_policy_path=(Path(eff_pg_policy).resolve() if eff_pg_policy else None),
                                report_path=report_p,
                                job_id=str(job_id),
                            )
                            with suppress(Exception):
                                from anime_v2.utils.io import atomic_copy

                                atomic_copy(report_p, base_analysis_dir / "pg_filter_report.json")
                    except Exception:
                        self.store.append_log(job_id, f"[{now_utc()}] pg_filter failed; continuing")
                    # Optional timing-aware translation fit (Tier-1 B).
                    if bool(getattr(settings, "timing_fit", False)):
                        try:
                            from anime_v2.timing.fit_text import fit_translation_to_time

                            wps = float(getattr(settings, "timing_wps", 2.7))
                            tol = float(getattr(settings, "timing_tolerance", 0.10))
                            for seg in translated_segments:
                                try:
                                    tgt_s = max(0.0, float(seg["end"]) - float(seg["start"]))
                                    pre = str(seg.get("text") or "")
                                    fitted, stats = fit_translation_to_time(
                                        pre, tgt_s, tolerance=tol, wps=wps, max_passes=4
                                    )
                                    seg["text_pre_fit"] = pre
                                    seg["text"] = fitted
                                    seg["timing_fit"] = stats.to_dict()
                                except Exception:
                                    continue
                        except Exception:
                            pass
                    write_json(
                        translated_json,
                        {
                            "src_lang": job.src_lang,
                            "tgt_lang": job.tgt_lang,
                            "segments": translated_segments,
                        },
                    )
                    srt_lines = [
                        {
                            "start": s["start"],
                            "end": s["end"],
                            "speaker_id": s["speaker"],
                            "text": (
                                s.get("text")
                                if bool(getattr(settings, "subs_use_fitted_text", True))
                                else (s.get("text_pre_fit") or s.get("text"))
                            ),
                        }
                        for s in translated_segments
                    ]
                    _write_srt(srt_lines, translated_srt)
                    subs_srt_path = translated_srt
                    self.store.update(job_id, progress=0.75, message="Translation done")
                except Exception as ex:
                    self.store.append_log(job_id, f"[{now_utc()}] translate failed: {ex}")
                    self.store.update(
                        job_id, progress=0.75, message="Translation failed (using original text)"
                    )
            else:
                self.store.update(job_id, progress=0.75, message="Translation skipped")

            # If resynth requested, generate an edited translation JSON for TTS.
            edited_json = None
            if isinstance(resynth, dict) and str(resynth.get("type") or "") == "approved":
                edited_json = _apply_transcript_to_translated_json(approved_only=True)
                if edited_json is not None:
                    translated_json = edited_json
            await self._check_canceled(job_id)

            # e) tts.synthesize aligned track (~0.95)
            tts_wav = work_dir / f"{video_path.stem}.tts.wav"

            def on_tts_progress(done: int, total: int) -> None:
                # map [0..1] => [0.76..0.95]
                frac = 0.0 if total <= 0 else float(done) / float(total)
                self.store.update(
                    job_id, progress=0.76 + 0.19 * frac, message=f"TTS {done}/{total}"
                )

            def cancel_cb() -> bool:
                return job_id in self._cancel

            self.store.update(job_id, progress=0.76, message="Synthesizing TTS")
            self.store.append_log(job_id, f"[{now_utc()}] tts")
            try:
                with time_hist(pipeline_tts_seconds) as elapsed:
                    t_tts0 = time.perf_counter()

                    # Per-job voice mapping for TTS (persisted via /api/jobs/{id}/characters).
                    voice_map_json = None
                    try:
                        curj = self.store.get(job_id)
                        rt = dict((curj.runtime or {}) if curj else runtime)
                        items = rt.get("voice_map", [])
                        if isinstance(items, list) and items:
                            voice_map_json = work_dir / "voice_map.json"
                            from anime_v2.utils.io import write_json

                            write_json(voice_map_json, {"items": items})
                    except Exception:
                        voice_map_json = None

                    # Run TTS in a separate process so watchdog can SIGKILL if it hangs.
                    def _tts_phase():
                        # Preset overrides for this job (lang/speaker/wav).
                        # Default TTS language should match the requested target language.
                        tts_lang = str(job.tgt_lang) if getattr(job, "tgt_lang", None) else None
                        tts_speaker = None
                        tts_speaker_wav = None
                        director_on = False
                        director_strength = 0.5
                        try:
                            curj = self.store.get(job_id)
                            rt = dict((curj.runtime or {}) if curj else runtime)
                            preset = rt.get("preset")
                            if isinstance(preset, dict):
                                tl = str(preset.get("tts_lang") or "").strip()
                                if tl:
                                    tts_lang = tl
                                sp = str(preset.get("tts_speaker") or "").strip()
                                if sp:
                                    tts_speaker = sp
                                wp = str(preset.get("tts_speaker_wav") or "").strip()
                                if wp:
                                    tts_speaker_wav = Path(wp)
                            director_on = bool(rt.get("director")) or bool(
                                getattr(settings, "director", False)
                            )
                            director_strength = float(
                                rt.get("director_strength") or getattr(settings, "director_strength", 0.5)
                            )
                        except Exception:
                            pass
                        review_state = base_dir / "review" / "state.json"
                        return tts.run(
                            out_dir=work_dir,
                            translated_json=translated_json if translated_json.exists() else None,
                            diarization_json=diar_json_work if diar_json_work.exists() else None,
                            wav_out=tts_wav,
                            review_state_path=review_state if review_state.exists() else None,
                            voice_map_json_path=voice_map_json,
                            tts_lang=tts_lang,
                            tts_speaker=tts_speaker,
                            tts_speaker_wav=tts_speaker_wav,
                            voice_mode=str(settings.voice_mode),
                            voice_ref_dir=settings.voice_ref_dir,
                            voice_store_dir=settings.voice_store_dir,
                            tts_provider=str(settings.tts_provider),
                            # expressiveness (best-effort)
                            emotion_mode=str(settings.emotion_mode),
                            expressive=str(getattr(settings, "expressive", "off")),
                            expressive_strength=float(getattr(settings, "expressive_strength", 0.5)),
                            expressive_debug=bool(getattr(settings, "expressive_debug", False)),
                            source_audio_wav=Path(str(wav)),
                            music_regions_path=music_regions_path_work,
                            director=bool(director_on),
                            director_strength=float(director_strength),
                            speech_rate=float(settings.speech_rate),
                            pitch=float(settings.pitch),
                            energy=float(settings.energy),
                            # Tier-2A voice memory controls (opt-in)
                            voice_memory=bool(getattr(settings, "voice_memory", False)),
                            voice_memory_dir=(
                                Path(settings.voice_memory_dir).resolve()
                                if getattr(settings, "voice_memory_dir", None)
                                else None
                            ),
                            voice_match_threshold=float(
                                getattr(settings, "voice_match_threshold", 0.75)
                            ),
                            voice_auto_enroll=bool(getattr(settings, "voice_auto_enroll", True)),
                            voice_character_map=(
                                Path(str(settings.voice_character_map)).resolve()
                                if getattr(settings, "voice_character_map", None)
                                else None
                            ),
                            pacing=bool(getattr(settings, "pacing", False)),
                            pacing_min_ratio=float(getattr(settings, "pacing_min_ratio", 0.88)),
                            pacing_max_ratio=float(getattr(settings, "pacing_max_ratio", 1.18)),
                            timing_tolerance=float(getattr(settings, "timing_tolerance", 0.10)),
                            timing_debug=bool(getattr(settings, "timing_debug", False)),
                            # callbacks omitted (not picklable); progress updates remain coarse for this phase
                            progress_cb=None,
                            cancel_cb=None,
                            max_stretch=float(settings.max_stretch),
                            job_id=job_id,
                            audio_hash=audio_hash,
                        )

                    # checkpoint-aware skip
                    tts_manifest = work_dir / "tts_manifest.json"
                    if (
                        tts_wav.exists()
                        and tts_manifest.exists()
                        and stage_is_done(ckpt, "tts")
                        and not (
                            isinstance(resynth, dict)
                            and str(resynth.get("type") or "") == "approved"
                        )
                    ):
                        self.store.append_log(job_id, f"[{now_utc()}] tts (checkpoint hit)")
                    else:
                        # Force rerun on resynth.
                        if (
                            isinstance(resynth, dict)
                            and str(resynth.get("type") or "") == "approved"
                        ):
                            with suppress(Exception):
                                tts_wav.unlink(missing_ok=True)
                            with suppress(Exception):
                                tts_manifest.unlink(missing_ok=True)
                        if sched is None:
                            run_with_timeout("tts", timeout_s=limits.timeout_tts_s, fn=_tts_phase)
                        else:
                            with sched.phase("tts"):
                                run_with_timeout(
                                    "tts", timeout_s=limits.timeout_tts_s, fn=_tts_phase
                                )
                        with suppress(Exception):
                            rt = dict(
                                (self.store.get(job_id).runtime or {})
                                if self.store.get(job_id)
                                else runtime
                            )
                            rt.setdefault("breaker_state", {})
                            rt["breaker_state"]["tts"] = Circuit.get("tts").snapshot().state
                            rt.setdefault("attempts", {})
                            rt["attempts"]["tts"] = int(rt["attempts"].get("tts", 0)) + 1
                            self.store.update(job_id, runtime=rt)
                        with suppress(Exception):
                            write_ckpt(
                                job_id,
                                "tts",
                                {"tts_wav": tts_wav, "manifest": tts_manifest},
                                {"work_dir": str(work_dir)},
                                ckpt_path=ckpt_path,
                            )
                            ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                    tts_seconds.observe(max(0.0, time.perf_counter() - t_tts0))
                    dt = elapsed()
                    if dt > float(settings.budget_tts_sec):
                        _mark_degraded("budget_tts_exceeded")
            except tts.TTSCanceled:
                job_errors.labels(stage="tts").inc()
                raise JobCanceled() from None
            except PhaseTimeout as ex:
                job_errors.labels(stage="tts_timeout").inc()
                self.store.append_log(job_id, f"[{now_utc()}] tts watchdog timeout: {ex}")
                raise RuntimeError(str(ex)) from ex
            except JobCanceled:
                job_errors.labels(stage="tts").inc()
                raise
            except Exception as ex:
                job_errors.labels(stage="tts").inc()
                self.store.append_log(
                    job_id, f"[{now_utc()}] tts failed: {ex} (continuing with silence)"
                )
                # Silence track is already best-effort within tts.run; ensure file exists.
                if not tts_wav.exists():
                    from anime_v2.stages.tts import _write_silence_wav  # type: ignore

                    # best-effort duration from diarization-timed segments
                    dur = max((float(s["end"]) for s in segments_for_mt), default=0.0)
                    _write_silence_wav(tts_wav, duration_s=dur)

            self.store.update(job_id, progress=0.95, message="TTS done")

            # Tier-2B canonicalization: if "resynth approved" was requested, persist the
            # generated clips into review/state.json as locked segments.
            if isinstance(resynth, dict) and str(resynth.get("type") or "") == "approved":
                try:
                    from anime_v2.review.ops import lock_from_tts_manifest

                    n_locked = lock_from_tts_manifest(
                        job_dir=base_dir,
                        tts_manifest=work_dir / "tts_manifest.json",
                        video_path=video_path,
                        lock_nonempty_only=True,
                    )
                    self.store.append_log(
                        job_id, f"[{now_utc()}] review: locked {n_locked} segments from resynth"
                    )
                except Exception as ex:
                    self.store.append_log(job_id, f"[{now_utc()}] review lock-from-resynth failed: {ex}")
            await self._check_canceled(job_id)

            # f) mixing (~1.00)
            out_mkv = work_dir / f"{video_path.stem}.dub.mkv"
            out_mp4 = work_dir / f"{video_path.stem}.dub.mp4"
            final_mkv = base_dir / f"{video_path.stem}.dub.mkv"
            final_mp4 = base_dir / f"{video_path.stem}.dub.mp4"
            self.store.update(job_id, progress=0.97, message="Mixing & muxing")
            self.store.append_log(job_id, f"[{now_utc()}] mix")
            with time_hist(pipeline_mux_seconds) as elapsed_mux:
                try:
                    # checkpoint-aware skip (accept either our "mix" stage marker or legacy "mux")
                    if stage_is_done(ckpt, "mix") or stage_is_done(ckpt, "mux"):
                        self.store.append_log(job_id, f"[{now_utc()}] mix (checkpoint hit)")
                        # best-effort: reuse previous output paths if present in store state
                        existing = self.store.get(job_id)
                        if existing and existing.output_mkv and Path(existing.output_mkv).exists():
                            out_mkv = Path(existing.output_mkv)
                        if existing and existing.output_srt and Path(existing.output_srt).exists():
                            subs_srt_path = Path(existing.output_srt)
                    else:
                        emit_env = str(settings.emit_formats or "mkv,mp4")
                        cfg_mix = MixConfig(
                            profile=str(settings.mix_profile),
                            separate_vocals=bool(settings.separate_vocals),
                            emit=tuple(
                                sorted(
                                    {
                                        "mkv",
                                        "mp4",
                                        *[
                                            p.strip().lower()
                                            for p in emit_env.split(",")
                                            if p.strip()
                                        ],
                                    }
                                )
                            ),
                        )
                        if mix_mode == "enhanced":
                            # Tier-1 A enhanced mix: background + TTS → final_mix.wav, then export container(s).
                            from anime_v2.audio.mix import MixParams, mix_dubbed_audio
                            from anime_v2.stages.export import (
                                export_hls,
                                export_m4a,
                                export_mkv,
                                export_mkv_multitrack,
                                export_mp4,
                            )
                            from anime_v2.utils.io import atomic_copy

                            bg = background_wav or Path(str(wav))
                            # If separation is on, background stem likely removes vocals. For detected music
                            # regions, preserve original audio (singing) by switching bed source.
                            if (
                                background_wav is not None
                                and music_regions_path_work is not None
                                and music_regions_path_work.exists()
                            ):
                                try:
                                    from anime_v2.audio.music_detect import build_music_preserving_bed
                                    from anime_v2.utils.io import read_json

                                    data = read_json(music_regions_path_work, default={})
                                    regs = data.get("regions", []) if isinstance(data, dict) else []
                                    if isinstance(regs, list) and regs:
                                        bed = audio_dir / "background_music_preserve.wav"
                                        bg = build_music_preserving_bed(
                                            background_wav=background_wav,
                                            original_wav=Path(str(wav)),
                                            regions=[r for r in regs if isinstance(r, dict)],
                                            out_wav=bed,
                                        )
                                except Exception:
                                    self.store.append_log(
                                        job_id, f"[{now_utc()}] music bed build failed; continuing"
                                    )
                            final_mix_wav = audio_dir / "final_mix.wav"

                            def _enhanced_phase():
                                mix_dubbed_audio(
                                    background_wav=bg,
                                    tts_dialogue_wav=tts_wav,
                                    out_wav=final_mix_wav,
                                    params=MixParams(
                                        lufs_target=float(getattr(settings, "lufs_target", -16.0)),
                                        ducking=bool(getattr(settings, "ducking", True)),
                                        ducking_strength=float(
                                            getattr(settings, "ducking_strength", 1.0)
                                        ),
                                        limiter=bool(getattr(settings, "limiter", True)),
                                    ),
                                )
                                with suppress(Exception):
                                    atomic_copy(final_mix_wav, base_audio_dir / "final_mix.wav")

                                outs2: dict[str, Path] = {}
                                emit = set(cfg_mix.emit or ())
                                # Multi-track output (opt-in): write track artifacts + mux multi-audio MKV (preferred).
                                if bool(getattr(settings, "multitrack", False)):
                                    try:
                                        from anime_v2.audio.tracks import build_multitrack_artifacts

                                        tracks = build_multitrack_artifacts(
                                            job_dir=base_dir,
                                            original_wav=Path(str(wav)),
                                            dubbed_wav=final_mix_wav,
                                            dialogue_wav=tts_wav,
                                            background_wav=bg if background_wav is not None else None,
                                        )
                                        if str(getattr(settings, "container", "mkv")).lower() == "mkv" and "mkv" in emit:
                                            outs2["mkv"] = export_mkv_multitrack(
                                                video_in=video_path,
                                                tracks=[
                                                    {
                                                        "path": str(tracks.original_full_wav),
                                                        "title": "Original (JP)",
                                                        "language": "jpn",
                                                        "default": "0",
                                                    },
                                                    {
                                                        "path": str(tracks.dubbed_full_wav),
                                                        "title": "Dubbed (EN)",
                                                        "language": "eng",
                                                        "default": "1",
                                                    },
                                                    {
                                                        "path": str(tracks.background_only_wav),
                                                        "title": "Background Only",
                                                        "language": "und",
                                                        "default": "0",
                                                    },
                                                    {
                                                        "path": str(tracks.dialogue_only_wav),
                                                        "title": "Dialogue Only",
                                                        "language": "eng",
                                                        "default": "0",
                                                    },
                                                ],
                                                srt=subs_srt_path,
                                                out_path=work_dir / f"{video_path.stem}.dub.mkv",
                                            )
                                        elif str(getattr(settings, "container", "mkv")).lower() == "mp4":
                                            sidecar_dir = base_dir / "audio" / "tracks"
                                            export_m4a(
                                                tracks.original_full_wav,
                                                sidecar_dir / "original_full.m4a",
                                                title="Original (JP)",
                                                language="jpn",
                                            )
                                            export_m4a(
                                                tracks.background_only_wav,
                                                sidecar_dir / "background_only.m4a",
                                                title="Background Only",
                                                language="und",
                                            )
                                            export_m4a(
                                                tracks.dialogue_only_wav,
                                                sidecar_dir / "dialogue_only.m4a",
                                                title="Dialogue Only",
                                                language="eng",
                                            )
                                            export_m4a(
                                                tracks.dubbed_full_wav,
                                                sidecar_dir / "dubbed_full.m4a",
                                                title="Dubbed (EN)",
                                                language="eng",
                                            )
                                    except Exception as ex:
                                        self.store.append_log(job_id, f"[{now_utc()}] multitrack failed; continuing ({ex})")

                                if "mkv" in emit and "mkv" not in outs2:
                                    outs2["mkv"] = export_mkv(
                                        video_path,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}.dub.mkv",
                                    )
                                if "mp4" in emit:
                                    outs2["mp4"] = export_mp4(
                                        video_path,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}.dub.mp4",
                                    )
                                if "fmp4" in emit:
                                    outs2["fmp4"] = export_mp4(
                                        video_path,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}.dub.frag.mp4",
                                        fragmented=True,
                                    )
                                if "hls" in emit:
                                    outs2["hls"] = export_hls(
                                        video_path,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}_hls",
                                    )
                                return outs2

                            if sched is None:
                                outs = _enhanced_phase()
                            else:
                                with sched.phase("mux"):
                                    outs = _enhanced_phase()
                        else:
                            if sched is None:
                                outs = mix(
                                    video_in=video_path,
                                    tts_wav=tts_wav,
                                    srt=subs_srt_path,
                                    out_dir=work_dir,
                                    cfg=cfg_mix,
                                )
                            else:
                                with sched.phase("mux"):
                                    outs = mix(
                                        video_in=video_path,
                                        tts_wav=tts_wav,
                                        srt=subs_srt_path,
                                        out_dir=work_dir,
                                        cfg=cfg_mix,
                                    )
                        out_mkv = outs.get("mkv", out_mkv)
                        out_mp4 = outs.get("mp4", out_mp4)
                        if bool(getattr(settings, "multitrack", False)):
                            try:
                                from anime_v2.audio.tracks import build_multitrack_artifacts
                                from anime_v2.stages.export import export_m4a, export_mkv_multitrack

                                mixed_wav = outs.get("mixed_wav", None)
                                if mixed_wav is not None and Path(mixed_wav).exists():
                                    stems_bg = (
                                        work_dir / "stems" / "background.wav"
                                        if (work_dir / "stems" / "background.wav").exists()
                                        else None
                                    )
                                    tracks = build_multitrack_artifacts(
                                        job_dir=base_dir,
                                        original_wav=Path(str(wav)),
                                        dubbed_wav=Path(mixed_wav),
                                        dialogue_wav=tts_wav,
                                        background_wav=stems_bg,
                                    )
                                    if str(getattr(settings, "container", "mkv")).lower() == "mkv" and out_mkv:
                                        out_mkv = export_mkv_multitrack(
                                            video_in=video_path,
                                            tracks=[
                                                {"path": str(tracks.original_full_wav), "title": "Original (JP)", "language": "jpn", "default": "0"},
                                                {"path": str(tracks.dubbed_full_wav), "title": "Dubbed (EN)", "language": "eng", "default": "1"},
                                                {"path": str(tracks.background_only_wav), "title": "Background Only", "language": "und", "default": "0"},
                                                {"path": str(tracks.dialogue_only_wav), "title": "Dialogue Only", "language": "eng", "default": "0"},
                                            ],
                                            srt=subs_srt_path,
                                            out_path=Path(out_mkv),
                                        )
                                    elif str(getattr(settings, "container", "mkv")).lower() == "mp4":
                                        sidecar_dir = base_dir / "audio" / "tracks"
                                        export_m4a(tracks.original_full_wav, sidecar_dir / "original_full.m4a", title="Original (JP)", language="jpn")
                                        export_m4a(tracks.background_only_wav, sidecar_dir / "background_only.m4a", title="Background Only", language="und")
                                        export_m4a(tracks.dialogue_only_wav, sidecar_dir / "dialogue_only.m4a", title="Dialogue Only", language="eng")
                                        export_m4a(tracks.dubbed_full_wav, sidecar_dir / "dubbed_full.m4a", title="Dubbed (EN)", language="eng")
                            except Exception as ex:
                                self.store.append_log(job_id, f"[{now_utc()}] multitrack failed; continuing ({ex})")
                        try:
                            art = {"mkv": out_mkv}
                            if out_mp4 and Path(out_mp4).exists():
                                art["mp4"] = out_mp4
                            write_ckpt(
                                job_id, "mix", art, {"work_dir": str(work_dir)}, ckpt_path=ckpt_path
                            )
                            ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                        except Exception:
                            pass
                except Exception as ex:
                    self.store.append_log(
                        job_id, f"[{now_utc()}] mix failed: {ex} (falling back to mux)"
                    )
                    if sched is None:
                        mkv_export.mux(
                            src_video=video_path,
                            dub_wav=tts_wav,
                            srt_path=subs_srt_path,
                            out_mkv=out_mkv,
                            job_id=job_id,
                        )
                    else:
                        with sched.phase("mux"):
                            mkv_export.mux(
                                src_video=video_path,
                                dub_wav=tts_wav,
                                srt_path=subs_srt_path,
                                out_mkv=out_mkv,
                                job_id=job_id,
                            )
                finally:
                    dt = elapsed_mux()
                    if dt > float(settings.budget_mux_sec):
                        _mark_degraded("budget_mux_exceeded")

            def _move_best_effort(src: Path, dst: Path) -> None:
                try:
                    if not src.exists():
                        return
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        src.replace(dst)
                    except Exception:
                        shutil.move(str(src), str(dst))
                except Exception:
                    return

            _move_best_effort(Path(out_mkv), final_mkv)
            if out_mp4 and Path(out_mp4).exists():
                _move_best_effort(Path(out_mp4), final_mp4)

            # Tier-3A: optional lip-sync plugin (default off).
            try:
                mode = str(getattr(settings, "lipsync", "off") or "off").strip().lower()
                if mode != "off":
                    from anime_v2.plugins.lipsync.base import LipSyncRequest
                    from anime_v2.plugins.lipsync.registry import resolve_lipsync_plugin
                    from anime_v2.plugins.lipsync.wav2lip_plugin import _parse_bbox

                    plugin = resolve_lipsync_plugin(
                        mode,
                        wav2lip_dir=getattr(settings, "wav2lip_dir", None),
                        wav2lip_checkpoint=getattr(settings, "wav2lip_checkpoint", None),
                    )
                    if plugin is None or not plugin.is_available():
                        msg = (
                            "Lip-sync plugin requested but unavailable. "
                            "Place Wav2Lip at third_party/wav2lip and set WAV2LIP_CHECKPOINT "
                            "(or pass --wav2lip-dir/--wav2lip-checkpoint)."
                        )
                        if bool(getattr(settings, "strict_plugins", False)):
                            raise RuntimeError(msg)
                        self.store.append_log(job_id, f"[{now_utc()}] {msg}")
                    else:
                        tmp_dir = base_dir / "tmp" / "lipsync"
                        tmp_dir.mkdir(parents=True, exist_ok=True)

                        # Prefer enhanced mix audio when present, else use TTS track.
                        audio_for_lip = (
                            (base_dir / "audio" / "final_mix.wav")
                            if (base_dir / "audio" / "final_mix.wav").exists()
                            else tts_wav
                        )
                        out_lip = base_dir / "final_lipsynced.mp4"
                        bbox = (
                            _parse_bbox(str(getattr(settings, "lipsync_box", "") or ""))
                            if str(getattr(settings, "lipsync_face", "auto")).lower() == "bbox"
                            else None
                        )
                        req = LipSyncRequest(
                            input_video=video_path,
                            dubbed_audio_wav=audio_for_lip,
                            output_video=out_lip,
                            work_dir=tmp_dir,
                            face_mode=str(getattr(settings, "lipsync_face", "auto")).lower(),
                            device=str(getattr(settings, "lipsync_device", "auto")).lower(),
                            bbox=bbox,
                            timeout_s=int(getattr(settings, "lipsync_timeout_s", 1200)),
                        )
                        plugin.run(req)
                        self.store.append_log(job_id, f"[{now_utc()}] lipsync ok → {out_lip}")
            except Exception as ex:
                if bool(getattr(settings, "strict_plugins", False)):
                    raise
                self.store.append_log(job_id, f"[{now_utc()}] lipsync skipped: {ex}")
            # Update checkpoint to point at final artifacts (so a restart doesn't reference temp paths).
            try:
                art2 = {"mkv": final_mkv}
                if final_mp4.exists():
                    art2["mp4"] = final_mp4
                write_ckpt(job_id, "mix", art2, {"work_dir": str(base_dir)}, ckpt_path=ckpt_path)
                ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
            except Exception:
                pass

            # Tier-Next D: optional QA scoring (offline-only; writes reports, does not change outputs)
            try:
                curj = self.store.get(job_id)
                rt2 = dict((curj.runtime or {}) if curj else runtime)
                if bool(rt2.get("qa")):
                    from anime_v2.qa.scoring import score_job

                    score_job(base_dir, enabled=True, write_outputs=True)
                    self.store.append_log(job_id, f"[{now_utc()}] qa: ok")
            except Exception:
                self.store.append_log(job_id, f"[{now_utc()}] qa failed; continuing")

            self.store.update(
                job_id,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(final_mkv if final_mkv.exists() else out_mkv),
                output_srt=str(subs_srt_path) if subs_srt_path else "",
                work_dir=str(base_dir),
            )
            # Clear resynth flag after completion.
            with suppress(Exception):
                curj = self.store.get(job_id)
                rt2 = dict((curj.runtime or {}) if curj else runtime)
                if "resynth" in rt2:
                    rt2.pop("resynth", None)
                    self.store.update(job_id, runtime=rt2)
            jobs_finished.labels(state="DONE").inc()
            self.store.append_log(job_id, f"[{now_utc()}] done in {time.perf_counter()-t0:.2f}s")
            # Cleanup temp workdir (keep logs + checkpoint + final outputs in Output/<stem>/).
            with suppress(Exception):
                shutil.rmtree(work_dir, ignore_errors=True)
        except JobCanceled:
            self.store.append_log(job_id, f"[{now_utc()}] canceled")
            self.store.update(job_id, state=JobState.CANCELED, message="Canceled", error=None)
            jobs_finished.labels(state="CANCELED").inc()
            # optional cleanup of in-progress outputs: leave as-is (ignored by state)
        except Exception as ex:
            self.store.append_log(job_id, f"[{now_utc()}] failed: {ex}")
            self.store.update(job_id, state=JobState.FAILED, message="Failed", error=str(ex))
            jobs_finished.labels(state="FAILED").inc()
            pipeline_job_failed_total.inc()
        finally:
            dt = time.perf_counter() - t0
            logger.info(
                "job %s finished state=%s in %.2fs",
                job_id,
                (self.store.get(job_id).state if self.store.get(job_id) else "unknown"),
                dt,
            )
            with suppress(Exception):
                if sched is not None:
                    sched.on_job_done(job_id)
