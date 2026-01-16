from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from dubbing_pipeline.jobs.limits import get_limits
from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.jobs.watchdog import PhaseTimeout, run_with_timeout
from dubbing_pipeline.ops.metrics import (
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
from dubbing_pipeline.runtime.scheduler import Scheduler
from dubbing_pipeline.security.crypto import CryptoConfigError, decrypt_file, is_encrypted_path
from dubbing_pipeline.stages import audio_extractor, mkv_export, tts
from dubbing_pipeline.ops import audit
from dubbing_pipeline.stages.diarization import DiarizeConfig
from dubbing_pipeline.stages.diarization import diarize as diarize_v2
from dubbing_pipeline.stages.mixing import MixConfig, mix
from dubbing_pipeline.stages.transcription import transcribe
from dubbing_pipeline.stages.translation import TranslationConfig, translate_segments
from dubbing_pipeline.utils.circuit import Circuit
from dubbing_pipeline.utils.ffmpeg_safe import extract_audio_mono_16k
from dubbing_pipeline.utils.hashio import hash_audio_from_video
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.net import install_egress_policy
from dubbing_pipeline.utils.time import format_srt_timestamp

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-")


class JobCanceled(Exception):
    pass


class _DiarizeCheckpointHit(RuntimeError):
    """
    Internal control-flow marker to skip re-diarization when checkpoint artifacts exist.
    """

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


def _parse_srt_to_cues(srt_path: Path) -> list[dict]:
    from dubbing_pipeline.utils.cues import parse_srt_to_cues

    return parse_srt_to_cues(srt_path)


def _assign_speakers(cues: list[dict], diar_segments: list[dict] | None) -> list[dict]:
    from dubbing_pipeline.utils.cues import assign_speakers

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
        self,
        store: JobStore,
        *,
        concurrency: int = 1,
        app_root: Path | None = None,
        queue_backend: object | None = None,
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
        # Optional Level-2 queue backend hooks (Redis locks/counters). Kept generic to avoid import cycles.
        self.queue_backend = queue_backend

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
                        from dubbing_pipeline.runtime.scheduler import JobRecord

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

    async def enqueue_id(self, job_id: str) -> None:
        """
        Enqueue an existing job id without rewriting the JobStore.
        This is used by Redis-backed queue intake (Level 2).
        """
        jid = str(job_id or "").strip()
        if not jid:
            return
        await self._q.put(jid)

    async def cancel(self, id: str) -> Job | None:
        async with self._cancel_lock:
            self._cancel.add(id)
        j = self.store.update(id, state=JobState.CANCELED, message="Canceled")
        return j

    async def kill(self, id: str, *, reason: str = "Killed by admin") -> Job | None:
        """
        Force-stop a job.
        Implementation: mark as CANCELED and rely on watchdog cancel checks to terminate active phases quickly.
        """
        async with self._cancel_lock:
            self._cancel.add(id)
        # Preserve state semantics (no new enum) but include explicit reason.
        j = self.store.update(id, state=JobState.CANCELED, message=str(reason), error=None)
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

                # Level-2: acquire distributed lock/counters before running.
                # If unavailable or denied, the backend may defer/requeue via Redis and we skip execution.
                backend = getattr(self, "queue_backend", None)
                if backend is not None:
                    try:
                        uid = str(getattr(j, "owner_id", "") or "") if j is not None else ""
                        ok_to_run = await backend.before_job_run(job_id=str(job_id), user_id=(uid or None))
                        if not ok_to_run:
                            # backend handled deferral/cancel; do not run locally.
                            continue
                    except Exception as ex:
                        logger.warning(
                            "queue_before_job_run_failed",
                            job_id=str(job_id),
                            error=str(ex),
                        )
                        # Conservative: skip execution if backend is present but failed.
                        continue

                await self._run_job(job_id)

                # Best-effort post-run hook: ack/release locks based on persisted final state.
                if backend is not None:
                    try:
                        j2 = self.store.get(job_id)
                        st = ""
                        if j2 is not None and getattr(j2, "state", None) is not None:
                            st = str(getattr(j2.state, "value", "") or "")
                        uid2 = str(getattr(j2, "owner_id", "") or "") if j2 is not None else ""
                        await backend.after_job_run(
                            job_id=str(job_id),
                            user_id=(uid2 or None),
                            final_state=st,
                            ok=st == "DONE",
                            error=None,
                        )
                    except Exception:
                        pass
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
        # Base stem used for Output/<stem>/... and artifact naming.
        stem = str(runtime.get("source_stem") or video_path.stem or str(job.id)).strip() or str(job.id)
        # Privacy/data minimization (opt-in; default off). Also triggers minimal retention.
        try:
            from dubbing_pipeline.security.privacy import resolve_privacy

            priv = resolve_privacy(runtime)
            runtime.update(priv.to_runtime_patch())
        except Exception:
            priv = None

        # Canonical Output/<...>/ layout (single source of truth).
        try:
            from dubbing_pipeline.library.paths import get_job_output_root

            base_dir = get_job_output_root(job)
        except Exception:
            # Fallback to legacy inline behavior (should match get_job_output_root).
            proj_sub = ""
            try:
                proj = runtime.get("project")
                if isinstance(proj, dict):
                    proj_sub = str(proj.get("output_subdir") or "").strip().strip("/")
            except Exception:
                proj_sub = ""
            if proj_sub:
                base_dir = (out_root / proj_sub / stem).resolve()
            else:
                base_dir = (out_root / stem).resolve()
        else:
            # Ensure downstream naming uses the resolved output root leaf.
            with suppress(Exception):
                stem = str(base_dir.name or stem).strip() or stem
        base_dir.mkdir(parents=True, exist_ok=True)
        # Stable per-job pointer (best-effort) so mobile/API users can find artifacts under Output/jobs/<job_id>/...
        # We keep the canonical base_dir at Output/<stem>/... for backwards compatibility.
        with suppress(Exception):
            jobs_dir = (out_root / "jobs" / job_id).resolve()
            jobs_dir.mkdir(parents=True, exist_ok=True)
            (jobs_dir / "target.txt").write_text(str(base_dir) + "\n", encoding="utf-8")
            (jobs_dir / "job_id.txt").write_text(str(job_id) + "\n", encoding="utf-8")
            # Privacy: avoid writing source filename/path when enabled.
            if priv is not None and bool(getattr(priv, "privacy_on", False)):
                (jobs_dir / "video.txt").write_text("(redacted)\n", encoding="utf-8")
            else:
                (jobs_dir / "video.txt").write_text(str(video_path) + "\n", encoding="utf-8")
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
            output_mkv=str(base_dir / f"{stem}.dub.mkv"),
            output_srt=str(base_dir / f"{stem}.translated.srt"),
            runtime=runtime,
        )

        t0 = time.perf_counter()
        settings = get_settings()
        # Two-pass voice cloning orchestration (pass1: no-clone; pass2: rerun TTS+mix using extracted refs).
        mode_req = str(job.mode or "medium").strip().lower()
        two_pass_enabled = False
        two_pass_phase = "pass1"
        two_pass_request = ""
        try:
            rt0 = dict(runtime or {})
            # Explicit per-job override wins. Otherwise: HIGH on, LOW off, MEDIUM follows config default.
            if "voice_clone_two_pass" in rt0:
                two_pass_enabled = bool(rt0.get("voice_clone_two_pass"))
            else:
                if mode_req == "high":
                    two_pass_enabled = True
                elif mode_req == "low":
                    two_pass_enabled = False
                else:
                    two_pass_enabled = bool(getattr(settings, "voice_clone_two_pass", False))
            tp = rt0.get("two_pass") if isinstance(rt0.get("two_pass"), dict) else {}
            two_pass_phase = str((tp or {}).get("phase") or "").strip().lower() or "pass1"
            two_pass_request = str((tp or {}).get("request") or "").strip().lower()
            if two_pass_request in {"rerun_pass2", "rerun", "pass2"}:
                two_pass_phase = "pass2"
                two_pass_enabled = True
        except Exception:
            two_pass_enabled = False
            two_pass_phase = "pass1"
            two_pass_request = ""
        is_pass2_outer = bool(str(two_pass_phase or "") == "pass2")
        # Persist two-pass state into runtime for UI/debugging (best-effort).
        with suppress(Exception):
            curj0 = self.store.get(job_id)
            rt0b = dict((curj0.runtime or {}) if curj0 else runtime)
            tp0 = rt0b.get("two_pass") if isinstance(rt0b.get("two_pass"), dict) else {}
            tp0 = dict(tp0 or {})
            tp0["enabled"] = bool(two_pass_enabled)
            tp0["phase"] = str(two_pass_phase or "pass1")
            tp0.setdefault("markers", [])
            tp0.setdefault("skipped_in_pass2", [])
            rt0b["two_pass"] = tp0
            rt0b["two_pass_clone"] = bool(two_pass_enabled)
            self.store.update(job_id, runtime=rt0b)
        self.store.update(job_id, state=JobState.RUNNING, progress=0.0, message="Starting")
        self.store.append_log(job_id, f"[{now_utc()}] start job={job_id}")
        if is_pass2_outer:
            self.store.append_log(job_id, f"[{now_utc()}] passB_cloning_started")
            with suppress(Exception):
                curj0 = self.store.get(job_id)
                rt0b = dict((curj0.runtime or {}) if curj0 else runtime)
                tp0 = rt0b.get("two_pass") if isinstance(rt0b.get("two_pass"), dict) else {}
                tp0 = dict(tp0 or {})
                mk = tp0.get("markers")
                if not isinstance(mk, list):
                    mk = []
                mk.append("passB_cloning_started")
                tp0["markers"] = mk
                rt0b["two_pass"] = tp0
                self.store.update(job_id, runtime=rt0b)
        with suppress(Exception):
            audit.emit(
                "job.started",
                user_id=str(job.owner_id or "") or None,
                job_id=str(job_id),
                meta={"mode": str(job.mode), "device": str(job.device)},
            )

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

        class _Pass2Skip(Exception):
            """Internal control-flow for pass2 skip-only blocks."""

        def _fail_if_forbidden_stage(stage: str) -> None:
            """
            Test-only guard used by scripts/verify_two_pass_orchestration.py.
            If DUBBING_PIPELINE_FORBID_STAGES contains this stage name, raise.
            """
            raw = str(os.environ.get("DUBBING_PIPELINE_FORBID_STAGES", "") or "").strip()
            if not raw:
                raw = str(os.environ.get("DP_FORBID_STAGES", "") or "").strip()
            if not raw:
                return
            forbidden = {s.strip().lower() for s in raw.split(",") if s.strip()}
            if str(stage or "").strip().lower() in forbidden:
                raise RuntimeError(f"forbidden_stage:{stage}")

        decrypted_video: Path | None = None
        try:
            await self._check_canceled(job_id)

            def _cancel_check_sync() -> bool:
                """
                Sync cancel check for watchdog phases.
                Allows immediate termination of long-running stage processes when a job is canceled/killed.
                """
                try:
                    cur = self.store.get(job_id)
                    return cur is not None and cur.state == JobState.CANCELED
                except Exception:
                    return False

            def _note_pass2_skip(stage: str, reason: str = "") -> None:
                if not is_pass2_outer:
                    return
                with suppress(Exception):
                    curj = self.store.get(job_id)
                    rt = dict((curj.runtime or {}) if curj else runtime)
                    tp = rt.get("two_pass") if isinstance(rt.get("two_pass"), dict) else {}
                    tp = dict(tp or {})
                    lst = tp.get("skipped_in_pass2")
                    if not isinstance(lst, list):
                        lst = []
                    lst.append({"stage": str(stage), "reason": str(reason or "")})
                    tp["skipped_in_pass2"] = lst
                    rt["two_pass"] = tp
                    self.store.update(job_id, runtime=rt)
                with suppress(Exception):
                    msg = f"[{now_utc()}] pass2_skip stage={stage}"
                    if reason:
                        msg += f" reason={reason}"
                    self.store.append_log(job_id, msg)

            # If input video is encrypted-at-rest, decrypt once into the work dir for the duration of the job.
            # Fail-safe: if encryption is enabled but misconfigured, the job must fail.
            video_in = video_path
            if is_encrypted_path(video_path):
                decrypted_video = (work_dir / "_input_video").with_suffix(".mp4")
                try:
                    decrypt_file(video_path, decrypted_video, kind="uploads", job_id=job_id)
                except CryptoConfigError as ex:
                    raise RuntimeError(str(ex)) from ex
                except Exception as ex:
                    raise RuntimeError(f"Failed to decrypt input video: {ex}") from ex
                video_in = decrypted_video

            # Per-job ffmpeg stderr capture (concurrency-safe via ContextVar)
            try:
                from dubbing_pipeline.utils.ffmpeg_safe import set_ffmpeg_log_dir

                set_ffmpeg_log_dir((base_dir / "logs" / "ffmpeg").resolve())
            except Exception:
                pass

            # Compute audio hash once per job (used for cross-job caching)
            audio_hash = None
            try:
                audio_hash = hash_audio_from_video(video_in)
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
                if is_pass2_outer and not (wav_guess.exists() and stage_is_done(ckpt, "audio")):
                    # Pass B must not run audio extraction; fail-safe: skip pass B.
                    self.store.append_log(
                        job_id,
                        f"[{now_utc()}] pass2 skipped: missing audio checkpoint",
                    )
                    self.store.update(
                        job_id,
                        state=JobState.DONE,
                        progress=1.0,
                        message="Done (pass2 skipped: missing audio checkpoint)",
                    )
                    _auto_match_speakers_best_effort()
                    self.store.append_log(job_id, f"[{now_utc()}] passB_complete")
                    return
                if wav_guess.exists() and stage_is_done(ckpt, "audio"):
                    wav = wav_guess
                    self.store.append_log(job_id, f"[{now_utc()}] audio_extractor (checkpoint hit)")
                    if is_pass2_outer:
                        _note_pass2_skip("audio_extractor", "checkpoint_hit")
                else:
                    _fail_if_forbidden_stage("audio_extractor")
                    if sched is None:
                        wav = run_with_timeout(
                            "audio_extract",
                            timeout_s=limits.timeout_audio_s,
                            fn=audio_extractor.extract,
                            args=(),
                            kwargs={
                                "video": video_in,
                                "out_dir": work_dir,
                                "wav_out": work_dir / "audio.wav",
                                "job_id": job_id,
                            },
                            cancel_check=_cancel_check_sync,
                            cancel_exc=JobCanceled(),
                        )
                    else:
                        with sched.phase("audio"):
                            wav = run_with_timeout(
                                "audio_extract",
                                timeout_s=limits.timeout_audio_s,
                                fn=audio_extractor.extract,
                                args=(),
                                kwargs={
                                    "video": video_in,
                                    "out_dir": work_dir,
                                    "wav_out": work_dir / "audio.wav",
                                    "job_id": job_id,
                                },
                                cancel_check=_cancel_check_sync,
                                cancel_exc=JobCanceled(),
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
            # Stage manifest (resume-safe metadata; best-effort)
            try:
                if is_pass2_outer:
                    raise _Pass2Skip()
                from dubbing_pipeline.jobs.manifests import file_fingerprint, write_stage_manifest

                # Project profile provenance (best-effort; affects resume params hash)
                proj_name = ""
                proj_hash = ""
                try:
                    curj = self.store.get(job_id)
                    rt2 = dict((curj.runtime or {}) if curj else runtime)
                    proj_name = str(rt2.get("project_name") or "").strip()
                    if proj_name:
                        from dubbing_pipeline.projects.loader import (
                            load_project_profile,
                            write_profile_artifacts,
                        )

                        prof = load_project_profile(proj_name)
                        if prof is not None:
                            proj_hash = prof.profile_hash
                            with suppress(Exception):
                                self.store.update(
                                    job_id,
                                    runtime={
                                        **rt2,
                                        "project_profile_hash": proj_hash,
                                        "project_name": prof.name,
                                    },
                                )
                            # Persist under Output/<job>/analysis/
                            with suppress(Exception):
                                write_profile_artifacts(base_dir, prof)
                except Exception:
                    pass

                write_stage_manifest(
                    job_dir=base_dir,
                    stage="audio",
                    inputs={"video": file_fingerprint(video_in)},
                    params={
                        "wav_out": "audio.wav",
                        "project": proj_name,
                        "project_profile_hash": proj_hash,
                    },
                    outputs={"audio_wav": str(Path(str(wav)).resolve())},
                )
            except _Pass2Skip:
                pass
            except Exception:
                pass

            # Tier-Next A/B: optional music/singing region detection (opt-in; OFF by default).
            analysis_dir = work_dir / "analysis"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            base_analysis_dir = base_dir / "analysis"
            base_analysis_dir.mkdir(parents=True, exist_ok=True)
            music_regions_path_work: Path | None = None
            try:
                if is_pass2_outer:
                    # Pass B must not redo analysis; prefer existing effective regions if present.
                    eff = base_analysis_dir / "music_regions.effective.json"
                    raw = base_analysis_dir / "music_regions.json"
                    if eff.exists():
                        music_regions_path_work = eff
                    elif raw.exists():
                        music_regions_path_work = raw
                    _note_pass2_skip("music_detect", "reuse_existing")
                    raise _Pass2Skip()
                if bool(getattr(settings, "music_detect", False)):
                    from dubbing_pipeline.audio.music_detect import (
                        analyze_audio_for_music_regions,
                        detect_op_ed,
                        write_oped_json,
                        write_regions_json,
                    )
                    from dubbing_pipeline.utils.io import atomic_copy

                    regs = analyze_audio_for_music_regions(
                        Path(str(wav)),
                        mode=str(getattr(settings, "music_mode", "auto") or "auto"),
                        threshold=float(getattr(settings, "music_threshold", 0.70)),
                    )
                    music_regions_path_work = analysis_dir / "music_regions.json"
                    write_regions_json(regs, music_regions_path_work)
                    with suppress(Exception):
                        atomic_copy(
                            music_regions_path_work, base_analysis_dir / "music_regions.json"
                        )
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
            except _Pass2Skip:
                pass
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] music_detect failed: {ex}")
            # Feature D: per-job overrides can provide effective music regions even if detection is off.
            try:
                if is_pass2_outer:
                    _note_pass2_skip("overrides", "pass2_no_recompute")
                    raise _Pass2Skip()
                from dubbing_pipeline.review.overrides import apply_overrides, overrides_path

                if overrides_path(base_dir).exists() or music_regions_path_work is not None:
                    rep = apply_overrides(base_dir, write_manifest=True)
                    eff = base_analysis_dir / "music_regions.effective.json"
                    if eff.exists():
                        music_regions_path_work = eff
                    self.store.append_log(
                        job_id,
                        f"[{now_utc()}] overrides applied hash={rep.overrides_hash} music_regions={str(music_regions_path_work or '')}",
                    )
            except _Pass2Skip:
                pass
            except Exception:
                pass

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

            # Per-project profile: can provide mix presets + QA thresholds + provenance (best-effort).
            try:
                if is_pass2_outer:
                    _note_pass2_skip("project_profile", "pass2_no_recompute")
                    raise _Pass2Skip()
                curj = self.store.get(job_id)
                rt2 = dict((curj.runtime or {}) if curj else runtime)
                proj_name = str(rt2.get("project_name") or "").strip()
                if proj_name:
                    from dubbing_pipeline.projects.loader import (
                        load_project_profile,
                        log_profile_applied,
                        mix_overrides_from_profile,
                        write_profile_artifacts,
                    )

                    prof = load_project_profile(proj_name)
                    if prof is not None:
                        mo = mix_overrides_from_profile(prof)
                        # Only apply keys not already explicitly present in runtime.
                        applied = []
                        for k, v in mo.items():
                            if k not in rt2:
                                rt2[k] = v
                                applied.append(k)
                        rt2["project_name"] = prof.name
                        rt2["project_profile_hash"] = prof.profile_hash
                        with suppress(Exception):
                            self.store.update(job_id, runtime=rt2)
                        with suppress(Exception):
                            write_profile_artifacts(base_dir, prof)
                        log_profile_applied(
                            project=prof.name, profile_hash=prof.profile_hash, applied_keys=applied
                        )
            except _Pass2Skip:
                pass
            except Exception:
                pass

            sep_mode = str(getattr(settings, "separation", "off") or "off").lower()
            curj = self.store.get(job_id)
            rt2 = dict((curj.runtime or {}) if curj else runtime)
            mix_mode = str(
                rt2.get("mix_mode") or getattr(settings, "mix_mode", "legacy") or "legacy"
            ).lower()
            background_wav: Path | None = None
            if mix_mode == "enhanced":
                if is_pass2_outer:
                    bg = (base_stems_dir / "background.wav").resolve()
                    background_wav = bg if bg.exists() else Path(str(wav))
                    _note_pass2_skip("separation", "reuse_existing")
                elif sep_mode == "demucs":
                    try:
                        _fail_if_forbidden_stage("separation")
                        from dubbing_pipeline.audio.separation import separate_dialogue
                        from dubbing_pipeline.utils.io import atomic_copy

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
            vm_store = None
            diar_route = None

            def _build_voice_refs_best_effort() -> None:
                # Post-diarization: build per-speaker reference WAVs (best-effort, safe).
                try:
                    from dubbing_pipeline.jobs.manifests import file_fingerprint, write_stage_manifest
                    from dubbing_pipeline.voice_refs.extract_refs import (
                        ExtractRefsConfig,
                        extract_speaker_refs,
                    )

                    # Required output location: Output/<job_id>/voice_refs/...
                    job_refs_dir = (out_root / str(job_id) / "voice_refs").resolve()
                    job_refs_dir.mkdir(parents=True, exist_ok=True)
                    # Also keep legacy UI location (Output/<job>/analysis/voice_refs) best-effort.
                    analysis_dir = (base_dir / "analysis" / "voice_refs").resolve()
                    analysis_dir.mkdir(parents=True, exist_ok=True)

                    cfg_ref = ExtractRefsConfig(
                        target_seconds=float(getattr(settings, "voice_ref_target_s", 30.0) or 30.0),
                        min_seg_seconds=float(
                            getattr(settings, "voice_ref_min_candidate_s", 2.0) or 2.0
                        ),
                        max_seg_seconds=float(
                            getattr(settings, "voice_ref_max_candidate_s", 10.0) or 10.0
                        ),
                        overlap_eps_s=float(getattr(settings, "voice_ref_overlap_eps_s", 0.05) or 0.05),
                        min_speech_ratio=float(
                            getattr(settings, "voice_ref_min_speech_ratio", 0.60) or 0.60
                        ),
                    )

                    # Use diarization work segments when present (already includes wav_path).
                    man = extract_speaker_refs(
                        diarization_timeline=[
                            {
                                "start": float(s.get("start") or 0.0),
                                "end": float(s.get("end") or 0.0),
                                "speaker_id": str(s.get("speaker_id") or ""),
                                "wav_path": str(s.get("wav_path") or ""),
                            }
                            for s in diar_segments
                            if isinstance(s, dict)
                        ],
                        dialogue_wav=Path(str(diar_wav)),
                        out_dir=job_refs_dir,
                        config=cfg_ref,
                    )

                    # Update job runtime ("DB") with per-speaker ref info (best-effort).
                    with suppress(Exception):
                        curj = self.store.get(job_id)
                        rt3 = dict((curj.runtime or {}) if curj else runtime)
                        rt3["voice_refs"] = man.get("items") if isinstance(man.get("items"), dict) else {}
                        self.store.update(job_id, runtime=rt3)

                    # Mirror into legacy analysis dir for UI playback (best-effort).
                    with suppress(Exception):
                        from dubbing_pipeline.utils.io import atomic_copy

                        # manifest
                        if (job_refs_dir / "manifest.json").exists():
                            atomic_copy(job_refs_dir / "manifest.json", analysis_dir / "manifest.json")
                        # wavs
                        for p in job_refs_dir.glob("*_ref.wav"):
                            if p.is_file():
                                atomic_copy(p, analysis_dir / p.name.replace("_ref.wav", ".wav"))

                    # Persist a stage manifest for resume/debug (no secrets).
                    with suppress(Exception):
                        write_stage_manifest(
                            job_dir=base_dir,
                            stage="voice_refs",
                            inputs={
                                "audio_wav": file_fingerprint(Path(str(wav))),
                                "diarization_work_json": file_fingerprint(diar_json_work),
                            },
                            params={
                                "target_seconds": float(cfg_ref.target_seconds),
                                "min_seg_seconds": float(cfg_ref.min_seg_seconds),
                                "max_seg_seconds": float(cfg_ref.max_seg_seconds or 0.0),
                                "overlap_eps_s": float(cfg_ref.overlap_eps_s),
                                "min_speech_ratio": float(cfg_ref.min_speech_ratio),
                            },
                            outputs={
                                "job_manifest": str((job_refs_dir / "manifest.json").resolve()),
                                "speakers": list(sorted((man.get("items") or {}).keys()))
                                if isinstance(man, dict)
                                else [],
                            },
                        )
                    self.store.append_log(
                        job_id,
                        f"[{now_utc()}] voice_refs built speakers={len((man.get('items') or {}).keys())}",
                    )
                    self.store.append_log(job_id, f"[{now_utc()}] refs_extracted")
                    with suppress(Exception):
                        curj = self.store.get(job_id)
                        rt3 = dict((curj.runtime or {}) if curj else runtime)
                        tp3 = rt3.get("two_pass") if isinstance(rt3.get("two_pass"), dict) else {}
                        tp3 = dict(tp3 or {})
                        mk = tp3.get("markers")
                        if not isinstance(mk, list):
                            mk = []
                        mk.append("refs_extracted")
                        tp3["markers"] = mk
                        rt3["two_pass"] = tp3
                        self.store.update(job_id, runtime=rt3)
                except Exception as ex:
                    self.store.append_log(job_id, f"[{now_utc()}] voice_refs skipped: {ex}")

            def _auto_match_speakers_best_effort() -> None:
                try:
                    settings = get_settings()
                    if not bool(getattr(settings, "voice_auto_match", False)):
                        return
                    series_slug = str(getattr(job, "series_slug", "") or "").strip()
                    if not series_slug:
                        return
                    man_path = (base_dir / "analysis" / "voice_refs" / "manifest.json").resolve()
                    if not man_path.exists():
                        return
                    from dubbing_pipeline.utils.io import read_json

                    man = read_json(man_path, default={})
                    items = man.get("items") if isinstance(man, dict) else None
                    if not isinstance(items, dict) or not items:
                        return
                    speaker_refs: dict[str, Path] = {}
                    for sid, rec in items.items():
                        if not isinstance(rec, dict):
                            continue
                        safe_sid = Path(str(sid or "")).name.strip()
                        if not safe_sid:
                            continue
                        ref_raw = str(rec.get("job_ref_path") or rec.get("ref_path") or "").strip()
                        if not ref_raw:
                            continue
                        ref_path = Path(ref_raw).resolve()
                        if not ref_path.exists() or not ref_path.is_file():
                            continue
                        speaker_refs[safe_sid] = ref_path
                    if not speaker_refs:
                        return
                    from dubbing_pipeline.voice_store.embeddings import suggest_matches

                    matches = suggest_matches(
                        series_slug=series_slug,
                        speaker_refs=speaker_refs,
                        threshold=float(
                            getattr(settings, "voice_match_threshold", 0.75) or 0.75
                        ),
                        device=_select_device(job.device),
                    )
                    if not matches:
                        return
                    existing = {
                        str(rec.get("speaker_id") or ""): rec
                        for rec in self.store.list_speaker_mappings(str(job_id))
                        if isinstance(rec, dict)
                    }
                    suggested = 0
                    for rec in matches:
                        sid = str(rec.get("speaker_id") or "").strip()
                        cslug = str(rec.get("character_slug") or "").strip()
                        if not sid or not cslug:
                            continue
                        prev = existing.get(sid)
                        if prev is not None and bool(prev.get("locked")):
                            continue
                        try:
                            conf = float(rec.get("similarity") or 0.0)
                        except Exception:
                            conf = 0.0
                        if prev is not None:
                            try:
                                prev_conf = float(prev.get("confidence") or 0.0)
                            except Exception:
                                prev_conf = 0.0
                            if str(prev.get("character_slug") or "") == cslug and prev_conf >= conf:
                                continue
                        self.store.upsert_speaker_mapping(
                            job_id=str(job_id),
                            speaker_id=sid,
                            character_slug=cslug,
                            confidence=float(conf),
                            locked=False,
                            created_by="auto_match",
                        )
                        suggested += 1
                    if suggested:
                        self.store.append_log(
                            job_id, f"[{now_utc()}] voice_auto_match suggested={suggested}"
                        )
                except Exception as ex:
                    self.store.append_log(
                        job_id, f"[{now_utc()}] voice_auto_match skipped: {ex}"
                    )

            try:
                if is_pass2_outer:
                    # Pass B must NOT redo diarization or ref extraction.
                    diar_wav = Path(str(wav))
                    _note_pass2_skip("diarize", "pass2_no_rerun")
                    _note_pass2_skip("voice_refs", "pass2_no_rerun")
                    if diar_json_work.exists():
                        with suppress(Exception):
                            from dubbing_pipeline.utils.io import read_json

                            dj = read_json(diar_json_work, default={})
                            segs = dj.get("segments") if isinstance(dj, dict) else None
                            if isinstance(segs, list):
                                diar_segments = [s for s in segs if isinstance(s, dict)]
                    raise _DiarizeCheckpointHit()
                self.store.update(job_id, progress=0.12, message="Diarizing speakers")
                self.store.append_log(job_id, f"[{now_utc()}] diarize")
                try:
                    from dubbing_pipeline.audio.routing import resolve_diarization_input

                    diar_route = resolve_diarization_input(
                        job,
                        extracted_wav=Path(str(wav)),
                        base_dir=base_dir,
                        separation_enabled=bool(mix_mode == "enhanced" and sep_mode == "demucs"),
                    )
                except Exception:
                    diar_route = None
                diar_wav = diar_route.wav if diar_route is not None else Path(str(wav))
                diar_kind = diar_route.kind if diar_route is not None else "original"
                diar_rel = diar_route.rel_path if diar_route is not None else str(diar_wav.name)
                self.store.append_log(
                    job_id,
                    f"[{now_utc()}] diarize input={diar_kind} path={diar_rel}",
                )
                if diar_json_work.exists() and diar_json_public.exists() and stage_is_done(
                    ckpt, "diarize"
                ):
                    try:
                        from dubbing_pipeline.utils.io import read_json

                        dj = read_json(diar_json_work, default={})
                        segs = dj.get("segments") if isinstance(dj, dict) else None
                        if isinstance(segs, list):
                            diar_segments = [s for s in segs if isinstance(s, dict)]
                    except Exception:
                        diar_segments = []
                    raise _DiarizeCheckpointHit()

                cfg = DiarizeConfig(diarizer=str(settings.diarizer))
                _fail_if_forbidden_stage("diarize")
                utts = run_with_timeout(
                    "diarize",
                    timeout_s=limits.timeout_diarize_s,
                    fn=diarize_v2,
                    args=(str(diar_wav),),
                    kwargs={"device": _select_device(job.device), "cfg": cfg},
                    cancel_check=_cancel_check_sync,
                    cancel_exc=JobCanceled(),
                )

                # Tier-Next F: optional scene-aware speaker smoothing (opt-in; default off).
                try:
                    curj = self.store.get(job_id)
                    rt2 = dict((curj.runtime or {}) if curj else runtime)
                    eff_sm = bool(rt2.get("speaker_smoothing")) or bool(
                        getattr(settings, "speaker_smoothing", False)
                    )
                    eff_scene = str(
                        rt2.get("scene_detect") or getattr(settings, "scene_detect", "audio")
                    ).lower()
                    if eff_sm and eff_scene != "off":
                        from dubbing_pipeline.diarization.smoothing import (
                            detect_scenes_audio,
                            smooth_speakers_in_scenes,
                            write_speaker_smoothing_report,
                        )

                        analysis_dir = work_dir / "analysis"
                        analysis_dir.mkdir(parents=True, exist_ok=True)
                        base_analysis_dir = base_dir / "analysis"
                        base_analysis_dir.mkdir(parents=True, exist_ok=True)

                        scenes = detect_scenes_audio(Path(str(diar_wav)))
                        utts2, changes = smooth_speakers_in_scenes(
                            utts,
                            scenes,
                            min_turn_s=float(getattr(settings, "smoothing_min_turn_s", 0.6)),
                            surround_gap_s=float(
                                getattr(settings, "smoothing_surround_gap_s", 0.4)
                            ),
                        )
                        # Feature D: optional per-job smoothing overrides (disable smoothing in selected ranges/segments)
                        try:
                            from dubbing_pipeline.review.overrides import (
                                apply_smoothing_overrides_to_utts,
                                load_overrides,
                            )

                            ov = load_overrides(base_dir)
                            sm_ov = (
                                ov.get("smoothing_overrides", {}) if isinstance(ov, dict) else {}
                            )
                            utts2b, reverted = apply_smoothing_overrides_to_utts(
                                utts2, sm_ov, segment_ranges=None
                            )
                            if reverted:
                                self.store.append_log(
                                    job_id,
                                    f"[{now_utc()}] smoothing_overrides reverted_utts={reverted}",
                                )
                            utts = utts2b
                        except Exception:
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
                                "surround_gap_s": float(
                                    getattr(settings, "smoothing_surround_gap_s", 0.4)
                                ),
                            },
                        )
                        with suppress(Exception):
                            from dubbing_pipeline.utils.io import atomic_copy

                            atomic_copy(rep_path, base_analysis_dir / "speaker_smoothing.json")
                        self.store.append_log(
                            job_id,
                            f"[{now_utc()}] speaker_smoothing scenes={len(scenes)} changes={len(changes)}",
                        )
                except Exception:
                    self.store.append_log(
                        job_id, f"[{now_utc()}] speaker_smoothing failed; continuing"
                    )

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
                            src=Path(str(diar_wav)),
                            dst=seg_wav,
                            start_s=s,
                            end_s=e,
                            timeout_s=120,
                        )
                    except Exception:
                        seg_wav = Path(str(diar_wav))
                    by_label.setdefault(lab, []).append((s, e, seg_wav))

                show = str(settings.show_id) if settings.show_id else stem
                lab_to_char: dict[str, str] = {}
                # Tier-2A voice memory (optional, opt-in)
                vm_store = None
                vm_map: dict[str, str] = {}
                vm_meta: dict[str, dict[str, object]] = {}
                vm_enabled = bool(getattr(settings, "voice_memory", False)) and not bool(
                    runtime.get("minimal_artifacts")
                    or runtime.get("privacy_mode") in {"on", "1", True}
                )
                if vm_enabled:
                    try:
                        from dubbing_pipeline.voice_memory.store import (
                            VoiceMemoryStore,
                            compute_episode_key,
                        )

                        vm_dir = Path(settings.voice_memory_dir).resolve()
                        vm_store = VoiceMemoryStore(vm_dir)
                        # optional manual diar_label -> character_id overrides
                        mp = getattr(settings, "voice_character_map", None)
                        if mp:
                            from dubbing_pipeline.utils.io import read_json

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

                # Canonical speaker refs: use the ref extractor output per diar label.
                label_refs: dict[str, Path] = {}
                try:
                    from dubbing_pipeline.voice_refs.extract_refs import ExtractRefsConfig, extract_speaker_refs

                    label_ref_dir = (work_dir / "voice_refs_labels").resolve()
                    label_ref_dir.mkdir(parents=True, exist_ok=True)
                    cfg_label = ExtractRefsConfig(
                        target_seconds=float(getattr(settings, "voice_ref_target_s", 30.0) or 30.0),
                        min_seg_seconds=float(
                            getattr(settings, "voice_ref_min_candidate_s", 2.0) or 2.0
                        ),
                        max_seg_seconds=float(
                            getattr(settings, "voice_ref_max_candidate_s", 10.0) or 10.0
                        ),
                        overlap_eps_s=float(getattr(settings, "voice_ref_overlap_eps_s", 0.05) or 0.05),
                        min_speech_ratio=float(
                            getattr(settings, "voice_ref_min_speech_ratio", 0.60) or 0.60
                        ),
                    )
                    tl = []
                    for lab, segs in by_label.items():
                        for st, en, wav_p in segs:
                            tl.append(
                                {
                                    "start": float(st),
                                    "end": float(en),
                                    "speaker_id": str(lab),
                                    "wav_path": str(wav_p),
                                }
                            )
                    man_labels = extract_speaker_refs(
                        diarization_timeline=tl,
                        dialogue_wav=Path(str(diar_wav)),
                        out_dir=label_ref_dir,
                        config=cfg_label,
                    )
                    items = man_labels.get("items") if isinstance(man_labels, dict) else None
                    if isinstance(items, dict):
                        for sid, rec in items.items():
                            if not isinstance(rec, dict):
                                continue
                            rp = str(rec.get("ref_path") or "").strip()
                            if rp:
                                p = Path(rp).resolve()
                                if p.exists():
                                    label_refs[str(sid)] = p
                except Exception:
                    label_refs = {}

                for lab, _segs in by_label.items():
                    rep_wav = label_refs.get(str(lab))
                    if rep_wav is None:
                        lab_to_char[lab] = lab
                        self.store.append_log(
                            job_id, f"[{now_utc()}] voice_ref missing for diar_label={lab}"
                        )
                        continue
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
                        lab_to_char[lab] = lab

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

                from dubbing_pipeline.utils.io import write_json

                # Work version includes wav_path for TTS voice selection.
                write_json(
                    diar_json_work,
                    {
                        "audio_path": str(diar_wav),
                        "diarization_input": str(diar_kind),
                        "diarization_input_rel": str(diar_rel),
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
                write_json(
                    diar_json_public,
                    {
                        "audio_path": str(diar_wav),
                        "diarization_input": str(diar_kind),
                        "diarization_input_rel": str(diar_rel),
                        "segments": pub_segments,
                    },
                )
                self.store.update(
                    job_id,
                    progress=0.25,
                    message=f"Diarized ({len(set(s.get('speaker_id') for s in diar_segments))} speakers)",
                )
                # Checkpoint diarization artifacts (enables pass-2 reruns without re-diarization).
                with suppress(Exception):
                    write_ckpt(
                        job_id,
                        "diarize",
                        {"diar_work": diar_json_work, "diar_public": diar_json_public},
                        {"work_dir": str(work_dir)},
                        ckpt_path=ckpt_path,
                    )
                    ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt

                # Stage manifest: record diarization input routing decision (best-effort).
                with suppress(Exception):
                    from dubbing_pipeline.jobs.manifests import file_fingerprint, write_stage_manifest

                    write_stage_manifest(
                        job_dir=base_dir,
                        stage="diarize",
                        inputs={"audio_wav": file_fingerprint(Path(str(diar_wav)))},
                        params={
                            "diarizer": str(settings.diarizer),
                            "diarization_input": str(diar_kind),
                            "diarization_input_rel": str(diar_rel),
                        },
                        outputs={
                            "diarization_work_json": str(diar_json_work.resolve()),
                            "diarization_public_json": str(diar_json_public.resolve()),
                        },
                    )

                _build_voice_refs_best_effort()
            except _DiarizeCheckpointHit:
                # checkpoint hit: reuse diarization output as-is
                self.store.append_log(job_id, f"[{now_utc()}] diarize (checkpoint hit)")
                self.store.update(
                    job_id,
                    progress=0.25,
                    message=f"Diarized ({len(set(s.get('speaker_id') for s in diar_segments))} speakers)",
                )
                if not is_pass2_outer:
                    _build_voice_refs_best_effort()
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] diarize failed: {ex}")
                self.store.update(job_id, progress=0.25, message="Diarize skipped")
            await self._check_canceled(job_id)

            # c) transcription.transcribe (~0.60)
            mode = (job.mode or "medium").lower()
            try:
                from dubbing_pipeline.modes import resolve_effective_settings

                base = {
                    "diarizer": str(getattr(settings, "diarizer", "auto")),
                    "speaker_smoothing": bool(getattr(settings, "speaker_smoothing", False)),
                    "voice_memory": bool(getattr(settings, "voice_memory", False)),
                    "voice_mode": str(getattr(settings, "voice_mode", "clone")),
                    "voice_clone_two_pass": bool(
                        getattr(settings, "voice_clone_two_pass", False)
                    ),
                    "music_detect": bool(getattr(settings, "music_detect", False)),
                    "separation": str(getattr(settings, "separation", "off")),
                    "mix_mode": str(getattr(settings, "mix_mode", "legacy")),
                    "timing_fit": bool(getattr(settings, "timing_fit", False)),
                    "pacing": bool(getattr(settings, "pacing", False)),
                    "qa": bool(rt2.get("qa") or False) if isinstance(rt2, dict) else False,
                    "director": bool(getattr(settings, "director", False)),
                    "multitrack": bool(getattr(settings, "multitrack", False)),
                    "stream_context_seconds": float(
                        getattr(settings, "stream_context_seconds", 15.0) or 15.0
                    ),
                }
                overrides: dict[str, Any] = {}
                # job-level override for ASR model isn't currently a first-class field; keep mode-based selection.
                eff = resolve_effective_settings(mode=mode, base=base, overrides=overrides)
                model_name = str(eff.asr_model)
                # Apply mode-driven diarizer (not persisted; only affects this run)
                eff_diar = str(eff.diarizer)
                if eff_diar == "off":
                    rt2 = dict(rt2) if isinstance(rt2, dict) else {}
                    rt2["diarizer"] = "off"
                    self.store.update(job_id, runtime=rt2)
            except Exception:
                model_name = "medium"
            device = _select_device(job.device)
            srt_out = work_dir / f"{stem}.srt"
            # Persist a stable copy in Output/<stem>/ for inspection / playback.
            srt_public = base_dir / f"{stem}.srt"
            self.store.update(job_id, progress=0.30, message=f"Transcribing (Whisper {model_name})")
            self.store.append_log(
                job_id, f"[{now_utc()}] transcribe model={model_name} device={device}"
            )
            try:
                with time_hist(pipeline_transcribe_seconds) as elapsed:
                    t_wh0 = time.perf_counter()
                    srt_meta = srt_out.with_suffix(".json")
                    if is_pass2_outer and not (
                        srt_out.exists() and srt_meta.exists() and stage_is_done(ckpt, "transcribe")
                    ):
                        # Pass B must not run ASR; fail-safe: skip pass B.
                        self.store.append_log(
                            job_id,
                            f"[{now_utc()}] pass2 skipped: missing transcribe checkpoint",
                        )
                        self.store.update(
                            job_id,
                            state=JobState.DONE,
                            progress=1.0,
                            message="Done (pass2 skipped: missing transcribe checkpoint)",
                        )
                        _auto_match_speakers_best_effort()
                        self.store.append_log(job_id, f"[{now_utc()}] passB_complete")
                        return
                    if srt_out.exists() and srt_meta.exists() and stage_is_done(ckpt, "transcribe"):
                        self.store.append_log(job_id, f"[{now_utc()}] transcribe (checkpoint hit)")
                        if is_pass2_outer:
                            _note_pass2_skip("transcribe", "checkpoint_hit")
                    else:
                        # Import path: if a source SRT/transcript was provided at submit time, skip ASR.
                        try:
                            curj = self.store.get(job_id)
                            rt_imp = dict((curj.runtime or {}) if curj else runtime)
                            imp = (
                                rt_imp.get("imports")
                                if isinstance(rt_imp.get("imports"), dict)
                                else {}
                            )
                            src_p = str((imp or {}).get("src_srt_path") or "").strip()
                            js_p = str((imp or {}).get("transcript_json_path") or "").strip()
                        except Exception:
                            src_p = ""
                            js_p = ""
                        if src_p or js_p:
                            from dubbing_pipeline.utils.io import atomic_copy, read_json, write_json

                            # Prefer explicit src.srt when present.
                            src_path = Path(src_p) if src_p else None
                            if src_path is None and js_p:
                                # Try to derive source SRT from transcript JSON segments.
                                try:
                                    tj = read_json(Path(js_p), default={})
                                    segs = tj.get("segments") if isinstance(tj, dict) else None
                                    if isinstance(segs, list):
                                        cues = []
                                        for seg in segs:
                                            if not isinstance(seg, dict):
                                                continue
                                            st = float(seg.get("start") or 0.0)
                                            en = float(seg.get("end") or 0.0)
                                            txt = str(
                                                seg.get("source_text")
                                                or seg.get("src_text")
                                                or seg.get("text")
                                                or ""
                                            )
                                            if en <= st or not txt.strip():
                                                continue
                                            cues.append({"start": st, "end": en, "text": txt})
                                        if cues:
                                            _write_srt(cues, srt_out)
                                except Exception:
                                    pass
                            if src_path is not None and src_path.exists():
                                atomic_copy(src_path, srt_out)
                            # Persist public copy unless privacy disallows transcript storage.
                            if not bool(runtime.get("no_store_transcript") or False):
                                with suppress(Exception):
                                    atomic_copy(srt_out, srt_public)
                            # Write a minimal meta file expected by downstream logic.
                            with suppress(Exception):
                                write_json(srt_meta, {"imported": True, "source": (src_p or js_p)})
                            with suppress(Exception):
                                write_ckpt(
                                    job_id,
                                    "transcribe",
                                    {"srt_out": srt_out, "srt_meta": srt_meta},
                                    {"work_dir": str(work_dir)},
                                    ckpt_path=ckpt_path,
                                )
                                ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                            # Record skip reason
                            try:
                                curj2 = self.store.get(job_id)
                                rt2 = dict((curj2.runtime or {}) if curj2 else runtime)
                                rt2.setdefault("skipped_stages", [])
                                if isinstance(rt2["skipped_stages"], list):
                                    rt2["skipped_stages"].append(
                                        {"stage": "transcribe", "reason": "imported_transcript"}
                                    )
                                self.store.update(job_id, runtime=rt2)
                            except Exception:
                                pass
                            self.store.append_log(
                                job_id, f"[{now_utc()}] transcribe skipped (import)"
                            )
                        else:
                            _fail_if_forbidden_stage("transcribe")
                            if sched is None:
                                run_with_timeout(
                                    "transcribe",
                                    timeout_s=limits.timeout_whisper_s,
                                    fn=transcribe,
                                    kwargs={
                                        "audio_path": wav,
                                        "srt_out": srt_out,
                                        "device": device,
                                        "model_name": model_name,
                                        "task": "transcribe",
                                        "src_lang": job.src_lang,
                                        "tgt_lang": job.tgt_lang,
                                        "job_id": job_id,
                                        "audio_hash": audio_hash,
                                        "word_timestamps": bool(
                                            get_settings().whisper_word_timestamps
                                        ),
                                    },
                                    cancel_check=_cancel_check_sync,
                                    cancel_exc=JobCanceled(),
                                )
                            else:
                                with sched.phase("transcribe"):
                                    run_with_timeout(
                                        "transcribe",
                                        timeout_s=limits.timeout_whisper_s,
                                        fn=transcribe,
                                        kwargs={
                                            "audio_path": wav,
                                            "srt_out": srt_out,
                                            "device": device,
                                            "model_name": model_name,
                                            "task": "transcribe",
                                            "src_lang": job.src_lang,
                                            "tgt_lang": job.tgt_lang,
                                            "job_id": job_id,
                                            "audio_hash": audio_hash,
                                            "word_timestamps": bool(
                                                get_settings().whisper_word_timestamps
                                            ),
                                        },
                                        cancel_check=_cancel_check_sync,
                                        cancel_exc=JobCanceled(),
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
                from dubbing_pipeline.utils.io import atomic_copy

                if not bool(runtime.get("no_store_transcript") or False):
                    atomic_copy(srt_out, srt_public)
                    if srt_out.with_suffix(".json").exists():
                        atomic_copy(srt_out.with_suffix(".json"), srt_public.with_suffix(".json"))
            except Exception:
                pass

            # Feature E: formatted source subtitles under Output/<job>/subs/src.(srt|vtt) (best-effort).
            try:
                from dubbing_pipeline.subs.formatting import write_formatted_subs_variant

                if bool(runtime.get("no_store_transcript") or False):
                    raise RuntimeError("privacy_no_store_transcript")
                proj_name = ""
                try:
                    curj = self.store.get(job_id)
                    rt2 = dict((curj.runtime or {}) if curj else runtime)
                    proj_name = str(rt2.get("project_name") or "").strip()
                except Exception:
                    proj_name = ""
                src_blocks = []
                for c in _parse_srt_to_cues(srt_public if srt_public.exists() else srt_out):
                    if isinstance(c, dict):
                        src_blocks.append(
                            {
                                "start": float(c.get("start", 0.0)),
                                "end": float(c.get("end", 0.0)),
                                "text": str(c.get("text") or ""),
                            }
                        )
                write_formatted_subs_variant(
                    job_dir=base_dir, variant="src", blocks=src_blocks, project=(proj_name or None)
                )
            except Exception:
                pass
            # Prefer rich segment metadata (avg_logprob) when available.
            cues: list[dict] = []
            try:
                from dubbing_pipeline.utils.io import read_json

                meta = read_json(srt_out.with_suffix(".json"), default={})
                segs_detail = meta.get("segments_detail", []) if isinstance(meta, dict) else []
                cues = segs_detail if isinstance(segs_detail, list) else []
            except Exception:
                cues = _parse_srt_to_cues(srt_out)
            self.store.update(job_id, progress=0.60, message=f"Transcribed ({len(cues)} segments)")
            await self._check_canceled(job_id)
            # Stage manifest (resume-safe metadata; best-effort)
            try:
                from dubbing_pipeline.jobs.manifests import file_fingerprint, write_stage_manifest

                proj_name = ""
                proj_hash = ""
                try:
                    curj = self.store.get(job_id)
                    rt2 = dict((curj.runtime or {}) if curj else runtime)
                    proj_name = str(rt2.get("project_name") or "").strip()
                    proj_hash = str(rt2.get("project_profile_hash") or "").strip()
                except Exception:
                    pass

                write_stage_manifest(
                    job_dir=base_dir,
                    stage="transcribe",
                    inputs={"audio_wav": file_fingerprint(Path(str(wav)))},
                    params={
                        "model": str(model_name),
                        "device": str(device),
                        "task": "transcribe",
                        "src_lang": str(job.src_lang),
                        "tgt_lang": str(job.tgt_lang),
                        "word_timestamps": bool(get_settings().whisper_word_timestamps),
                        "project": proj_name,
                        "project_profile_hash": proj_hash,
                    },
                    outputs={
                        "srt": str(
                            srt_public.resolve() if srt_public.exists() else srt_out.resolve()
                        ),
                        "meta": str(
                            srt_public.with_suffix(".json").resolve()
                            if srt_public.with_suffix(".json").exists()
                            else srt_out.with_suffix(".json").resolve()
                        ),
                    },
                )
            except Exception:
                pass

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

            # Feature D: apply per-job speaker overrides to segment speaker IDs (best-effort).
            try:
                curj = self.store.get(job_id)
                rt2 = dict((curj.runtime or {}) if curj else runtime)
                from dubbing_pipeline.review.overrides import apply_speaker_overrides_to_segments

                sp = rt2.get("speaker_overrides", {})
                if not isinstance(sp, dict):
                    # fall back to overrides file if present
                    from dubbing_pipeline.review.overrides import load_overrides

                    ov = load_overrides(base_dir)
                    sp = ov.get("speaker_overrides", {}) if isinstance(ov, dict) else {}
                segments_for_mt, changed = apply_speaker_overrides_to_segments(segments_for_mt, sp)
                if changed:
                    self.store.append_log(
                        job_id, f"[{now_utc()}] speaker_overrides applied segments={changed}"
                    )
            except Exception:
                pass
            no_store_tx = bool(runtime.get("no_store_transcript") or False)
            translated_json = (
                (work_dir / "translated.json") if no_store_tx else (base_dir / "translated.json")
            )
            translated_srt = (
                (work_dir / f"{stem}.translated.srt")
                if no_store_tx
                else (base_dir / f"{stem}.translated.srt")
            )

            do_translate = job.src_lang.lower() != job.tgt_lang.lower()
            subs_srt_path: Path | None = srt_out if no_store_tx else srt_public
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
                    from dubbing_pipeline.utils.io import read_json, write_json

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
                            proj = (
                                rt2.get("project") if isinstance(rt2.get("project"), dict) else {}
                            )
                            proj_name = str(
                                proj.get("name") or rt2.get("project_name") or ""
                            ).strip()
                            sg_path = str(rt2.get("style_guide_path") or "").strip()
                            if proj_name or sg_path:
                                from dubbing_pipeline.text.style_guide import (
                                    apply_style_guide_to_segments,
                                    load_style_guide,
                                    resolve_style_guide_path,
                                )

                                eff_path = (
                                    Path(sg_path).resolve()
                                    if sg_path
                                    else resolve_style_guide_path(
                                        project=proj_name, style_guide_path=None
                                    )
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
                            from dubbing_pipeline.text.pg_filter import apply_pg_filter_to_segments

                            analysis_dir = work_dir / "analysis"
                            analysis_dir.mkdir(parents=True, exist_ok=True)
                            report_p = analysis_dir / "pg_filter_report.json"
                            out_segments, _ = apply_pg_filter_to_segments(
                                out_segments,
                                pg=eff_pg,
                                pg_policy_path=(
                                    Path(eff_pg_policy).resolve() if eff_pg_policy else None
                                ),
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
                        out_srt = base_dir / f"{stem}.translated.edited.srt"
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

            # Import target subtitles/transcripts: skip MT and use imported target as subs_srt_path.
            try:
                curj = self.store.get(job_id)
                rt_imp = dict((curj.runtime or {}) if curj else runtime)
                imp = rt_imp.get("imports") if isinstance(rt_imp.get("imports"), dict) else {}
                tgt_p = str((imp or {}).get("tgt_srt_path") or "").strip()
                js_p = str((imp or {}).get("transcript_json_path") or "").strip()
            except Exception:
                tgt_p = ""
                js_p = ""
            if tgt_p and Path(tgt_p).exists():
                try:
                    from dubbing_pipeline.utils.io import atomic_copy, write_json

                    atomic_copy(Path(tgt_p), translated_srt)
                    subs_srt_path = translated_srt
                    # Minimal translated.json from SRT cues
                    cues_tgt = _parse_srt_to_cues(translated_srt)
                    segs = [
                        {
                            "start": float(c["start"]),
                            "end": float(c["end"]),
                            "speaker": "SPEAKER_01",
                            "text": str(c.get("text") or ""),
                        }
                        for c in cues_tgt
                        if isinstance(c, dict)
                    ]
                    write_json(
                        translated_json,
                        {"src_lang": job.src_lang, "tgt_lang": job.tgt_lang, "segments": segs},
                    )
                    do_translate = False
                    try:
                        rt2 = dict(rt_imp)
                        rt2.setdefault("skipped_stages", [])
                        if isinstance(rt2["skipped_stages"], list):
                            rt2["skipped_stages"].append(
                                {"stage": "translate", "reason": "imported_target_srt"}
                            )
                        self.store.update(job_id, runtime=rt2)
                    except Exception:
                        pass
                    self.store.append_log(
                        job_id, f"[{now_utc()}] translate skipped (import target srt)"
                    )
                except Exception as ex:
                    self.store.append_log(
                        job_id, f"[{now_utc()}] import target srt failed: {ex} (continuing)"
                    )
            elif js_p and Path(js_p).exists():
                # Best-effort: if transcript JSON contains target text, use it.
                try:
                    from dubbing_pipeline.utils.io import read_json, write_json

                    tj = read_json(Path(js_p), default={})
                    segs_in = tj.get("segments") if isinstance(tj, dict) else None
                    if isinstance(segs_in, list):
                        cues = []
                        segs = []
                        for seg in segs_in:
                            if not isinstance(seg, dict):
                                continue
                            st = float(seg.get("start") or 0.0)
                            en = float(seg.get("end") or 0.0)
                            txt = str(
                                seg.get("tgt_text")
                                or seg.get("target_text")
                                or seg.get("text")
                                or ""
                            )
                            if en <= st:
                                continue
                            cues.append({"start": st, "end": en, "text": txt})
                            segs.append(
                                {"start": st, "end": en, "speaker": "SPEAKER_01", "text": txt}
                            )
                        if cues:
                            _write_srt(cues, translated_srt)
                            write_json(
                                translated_json,
                                {
                                    "src_lang": job.src_lang,
                                    "tgt_lang": job.tgt_lang,
                                    "segments": segs,
                                },
                            )
                            subs_srt_path = translated_srt
                            do_translate = False
                            try:
                                rt2 = dict(rt_imp)
                                rt2.setdefault("skipped_stages", [])
                                if isinstance(rt2["skipped_stages"], list):
                                    rt2["skipped_stages"].append(
                                        {"stage": "translate", "reason": "imported_transcript_json"}
                                    )
                                self.store.update(job_id, runtime=rt2)
                            except Exception:
                                pass
                            self.store.append_log(
                                job_id, f"[{now_utc()}] translate skipped (import transcript json)"
                            )
                except Exception:
                    pass

            # Checkpoint-aware skip: reuse existing translation artifacts (for pass2 reruns).
            if (
                do_translate
                and translated_json.exists()
                and translated_srt.exists()
                and stage_is_done(ckpt, "translate")
            ):
                self.store.append_log(job_id, f"[{now_utc()}] translate (checkpoint hit)")
                subs_srt_path = translated_srt
                do_translate = False
                if is_pass2_outer:
                    _note_pass2_skip("translate", "checkpoint_hit")

            if is_pass2_outer and do_translate:
                # Pass B must not run MT; fail-safe: skip pass B.
                self.store.append_log(
                    job_id,
                    f"[{now_utc()}] pass2 skipped: missing translate checkpoint",
                )
                self.store.update(
                    job_id,
                    state=JobState.DONE,
                    progress=1.0,
                    message="Done (pass2 skipped: missing translate checkpoint)",
                )
                _auto_match_speakers_best_effort()
                self.store.append_log(job_id, f"[{now_utc()}] passB_complete")
                return
            if is_pass2_outer and not do_translate:
                _note_pass2_skip("translate", "not_needed_or_already_done")

            if do_translate:
                self.store.update(job_id, progress=0.62, message="Translating subtitles")
                self.store.append_log(
                    job_id, f"[{now_utc()}] translate src={job.src_lang} tgt={job.tgt_lang}"
                )
                try:
                    from dubbing_pipeline.utils.io import write_json

                    cfg = TranslationConfig(
                        mt_engine=str(settings.mt_engine),
                        mt_lowconf_thresh=float(settings.mt_lowconf_thresh),
                        glossary_path=settings.glossary_path,
                        style_path=settings.style_path,
                        show_id=(str(settings.show_id) if settings.show_id else stem),
                        whisper_model=model_name,
                        audio_path=str(wav),
                        device=device,
                    )
                    _fail_if_forbidden_stage("translate")
                    translated_segments = run_with_timeout(
                        "translate",
                        timeout_s=limits.timeout_translate_s,
                        fn=translate_segments,
                        args=(segments_for_mt,),
                        kwargs={"src_lang": job.src_lang, "tgt_lang": job.tgt_lang, "cfg": cfg},
                        cancel_check=_cancel_check_sync,
                        cancel_exc=JobCanceled(),
                    )
                    # Tier-Next E: optional project style guide (best-effort; OFF by default).
                    try:
                        curj = self.store.get(job_id)
                        rt2 = dict((curj.runtime or {}) if curj else runtime)
                        proj = rt2.get("project") if isinstance(rt2.get("project"), dict) else {}
                        proj_name = str(proj.get("name") or rt2.get("project_name") or "").strip()
                        sg_path = str(rt2.get("style_guide_path") or "").strip()
                        if proj_name or sg_path:
                            from dubbing_pipeline.text.style_guide import (
                                apply_style_guide_to_segments,
                                load_style_guide,
                                resolve_style_guide_path,
                            )

                            eff_path = (
                                Path(sg_path).resolve()
                                if sg_path
                                else resolve_style_guide_path(
                                    project=proj_name, style_guide_path=None
                                )
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
                                    from dubbing_pipeline.utils.io import atomic_copy

                                    atomic_copy(
                                        out_jsonl, base_analysis_dir / "style_guide_applied.jsonl"
                                    )
                    except Exception:
                        self.store.append_log(
                            job_id, f"[{now_utc()}] style_guide failed; continuing"
                        )

                    # Snapshot for subtitle variants (literal translation, before PG + timing-fit).
                    translated_segments_literal = [
                        dict(s) for s in translated_segments if isinstance(s, dict)
                    ]
                    translated_segments_pg = None

                    # Tier-Next C: per-job PG mode (opt-in; OFF by default), before timing-fit/TTS/subs.
                    try:
                        curj = self.store.get(job_id)
                        rt2 = dict((curj.runtime or {}) if curj else runtime)
                        eff_pg = str(rt2.get("pg") or "off").strip().lower()
                        eff_pg_policy = str(rt2.get("pg_policy_path") or "").strip()
                        if eff_pg != "off":
                            from dubbing_pipeline.text.pg_filter import apply_pg_filter_to_segments

                            analysis_dir = work_dir / "analysis"
                            analysis_dir.mkdir(parents=True, exist_ok=True)
                            base_analysis_dir = base_dir / "analysis"
                            base_analysis_dir.mkdir(parents=True, exist_ok=True)
                            report_p = analysis_dir / "pg_filter_report.json"
                            translated_segments, _ = apply_pg_filter_to_segments(
                                translated_segments,
                                pg=eff_pg,
                                pg_policy_path=(
                                    Path(eff_pg_policy).resolve() if eff_pg_policy else None
                                ),
                                report_path=report_p,
                                job_id=str(job_id),
                            )
                            with suppress(Exception):
                                from dubbing_pipeline.utils.io import atomic_copy

                                atomic_copy(report_p, base_analysis_dir / "pg_filter_report.json")
                            translated_segments_pg = [
                                dict(s) for s in translated_segments if isinstance(s, dict)
                            ]
                    except Exception:
                        self.store.append_log(job_id, f"[{now_utc()}] pg_filter failed; continuing")
                    # Optional timing-aware translation fit (Tier-1 B).
                    if bool(getattr(settings, "timing_fit", False)):
                        try:
                            from dubbing_pipeline.timing.rewrite_provider import (
                                append_rewrite_jsonl,
                                fit_with_rewrite_provider,
                            )

                            wps = float(getattr(settings, "timing_wps", 2.7))
                            tol = float(getattr(settings, "timing_tolerance", 0.10))
                            analysis_dir = work_dir / "analysis"
                            analysis_dir.mkdir(parents=True, exist_ok=True)
                            rewrite_jsonl = analysis_dir / "rewrite_provider.jsonl"
                            for seg in translated_segments:
                                try:
                                    tgt_s = max(0.0, float(seg["end"]) - float(seg["start"]))
                                    pre = str(seg.get("text") or "")
                                    req_terms: list[str] = []
                                    ga = seg.get("glossary_applied")
                                    if isinstance(ga, list):
                                        for it in ga:
                                            if isinstance(it, dict):
                                                t = str(it.get("tgt") or "").strip()
                                                if t:
                                                    req_terms.append(t)

                                    fitted, stats, attempt = fit_with_rewrite_provider(
                                        provider_name=str(
                                            getattr(settings, "rewrite_provider", "heuristic")
                                        ).lower(),
                                        endpoint=(
                                            str(
                                                getattr(settings, "rewrite_endpoint", "") or ""
                                            ).strip()
                                            or None
                                        ),
                                        model_path=getattr(settings, "rewrite_model", None),
                                        strict=bool(getattr(settings, "rewrite_strict", True)),
                                        text=pre,
                                        target_seconds=tgt_s,
                                        tolerance=tol,
                                        wps=wps,
                                        constraints={"required_terms": req_terms},
                                        context={
                                            "context_hint": "",
                                            "speaker": str(seg.get("speaker") or ""),
                                        },
                                    )
                                    seg["text_pre_fit"] = pre
                                    seg["text"] = fitted
                                    seg["timing_fit"] = stats.to_dict()
                                    append_rewrite_jsonl(
                                        rewrite_jsonl,
                                        {
                                            "segment_id": int(seg.get("segment_id") or 0),
                                            "start": float(seg.get("start", 0.0)),
                                            "end": float(seg.get("end", 0.0)),
                                            **attempt.to_dict(),
                                        },
                                    )
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

                    # Feature E: formatted subtitle variants under Output/<job>/subs/
                    try:
                        from dubbing_pipeline.subs.formatting import write_formatted_subs_variant

                        if bool(runtime.get("no_store_transcript") or False):
                            raise RuntimeError("privacy_no_store_transcript")
                        proj_name = ""
                        try:
                            curj = self.store.get(job_id)
                            rt2 = dict((curj.runtime or {}) if curj else runtime)
                            proj_name = str(rt2.get("project_name") or "").strip()
                        except Exception:
                            proj_name = ""

                        write_formatted_subs_variant(
                            job_dir=base_dir,
                            variant="tgt_literal",
                            blocks=[
                                {
                                    "start": float(s.get("start", 0.0)),
                                    "end": float(s.get("end", 0.0)),
                                    "text": str(s.get("text") or ""),
                                }
                                for s in translated_segments_literal
                                if isinstance(s, dict)
                            ],
                            project=(proj_name or None),
                        )
                        if translated_segments_pg is not None:
                            write_formatted_subs_variant(
                                job_dir=base_dir,
                                variant="tgt_pg",
                                blocks=[
                                    {
                                        "start": float(s.get("start", 0.0)),
                                        "end": float(s.get("end", 0.0)),
                                        "text": str(s.get("text") or ""),
                                    }
                                    for s in translated_segments_pg
                                    if isinstance(s, dict)
                                ],
                                project=(proj_name or None),
                            )
                        if bool(getattr(settings, "timing_fit", False)):
                            write_formatted_subs_variant(
                                job_dir=base_dir,
                                variant="tgt_fit",
                                blocks=[
                                    {
                                        "start": float(s.get("start", 0.0)),
                                        "end": float(s.get("end", 0.0)),
                                        "text": str(s.get("text") or ""),
                                    }
                                    for s in translated_segments
                                    if isinstance(s, dict)
                                ],
                                project=(proj_name or None),
                            )
                    except Exception:
                        pass
                    self.store.update(job_id, progress=0.75, message="Translation done")
                except Exception as ex:
                    self.store.append_log(job_id, f"[{now_utc()}] translate failed: {ex}")
                    self.store.update(
                        job_id, progress=0.75, message="Translation failed (using original text)"
                    )
            else:
                self.store.update(job_id, progress=0.75, message="Translation skipped")

            # Checkpoint translation artifacts so pass-2 reruns can skip MT.
            with suppress(Exception):
                arts: dict[str, Path] = {}
                if translated_json.exists():
                    arts["translated_json"] = translated_json
                if translated_srt.exists():
                    arts["translated_srt"] = translated_srt
                if arts:
                    write_ckpt(
                        job_id,
                        "translate",
                        arts,
                        {"work_dir": str(work_dir)},
                        ckpt_path=ckpt_path,
                    )
                    ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt

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
                    # Persistent character voice mapping: speaker_id -> character_slug (stored in DB).
                    speaker_character_map: dict[str, str] = {}
                    try:
                        curj = self.store.get(job_id)
                        rt = dict((curj.runtime or {}) if curj else runtime)
                        items = rt.get("voice_map", [])
                        if isinstance(items, list) and items:
                            voice_map_json = work_dir / "voice_map.json"
                            from dubbing_pipeline.utils.io import write_json

                            write_json(voice_map_json, {"items": items})

                        # Best-effort: harvest manual mappings from voice_map items if provided.
                        # Expected fields (non-breaking): speaker_id (or legacy character_id), character_slug, label.
                        series_slug = str(getattr(job, "series_slug", "") or rt.get("series_slug") or "").strip()
                        if series_slug:
                            for it in items if isinstance(items, list) else []:
                                if not isinstance(it, dict):
                                    continue
                                speaker_id = str(it.get("speaker_id") or it.get("character_id") or "").strip()
                                cslug = str(it.get("character_slug") or "").strip()
                                if speaker_id and cslug:
                                    with suppress(Exception):
                                        self.store.upsert_speaker_mapping(
                                            job_id=str(job_id),
                                            speaker_id=speaker_id,
                                            character_slug=cslug,
                                            confidence=1.0,
                                            locked=True,
                                            created_by=str(getattr(job, "owner_id", "") or ""),
                                        )
                                    with suppress(Exception):
                                        self.store.upsert_character(
                                            series_slug=series_slug,
                                            character_slug=cslug,
                                            display_name=str(it.get("label") or "").strip(),
                                            ref_path="",
                                            created_by=str(getattr(job, "owner_id", "") or ""),
                                        )

                            # Load DB mappings for this job (manual/auto).
                            with suppress(Exception):
                                for rec in self.store.list_speaker_mappings(str(job_id)):
                                    sid = str(rec.get("speaker_id") or "").strip()
                                    cslug = str(rec.get("character_slug") or "").strip()
                                    if sid and cslug:
                                        speaker_character_map[sid] = cslug
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
                        # Two-pass voice cloning controls (resolved once per run).
                        is_pass2 = bool(str(two_pass_phase or "") == "pass2")
                        if is_pass2:
                            voice_mode_eff = "clone"
                            voice_ref_dir_eff = (base_dir / "analysis" / "voice_refs").resolve()
                            no_clone_eff = False
                        elif bool(two_pass_enabled):
                            # pass1: explicitly prevent cloning
                            voice_mode_eff = "preset"
                            voice_ref_dir_eff = None
                            no_clone_eff = True
                        else:
                            voice_mode_eff = str(settings.voice_mode)
                            voice_ref_dir_eff = settings.voice_ref_dir
                            if not voice_ref_dir_eff:
                                refs_dir = (base_dir / "analysis" / "voice_refs").resolve()
                                if refs_dir.exists():
                                    voice_ref_dir_eff = refs_dir
                            no_clone_eff = False
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
                                rt.get("director_strength")
                                or getattr(settings, "director_strength", 0.5)
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
                            voice_mode=str(voice_mode_eff),
                            no_clone=bool(no_clone_eff),
                            two_pass_enabled=bool(two_pass_enabled),
                            two_pass_phase=str(two_pass_phase or ""),
                            series_slug=str(getattr(job, "series_slug", "") or ""),
                            speaker_character_map=speaker_character_map,
                            voice_ref_dir=voice_ref_dir_eff,
                            voice_store_dir=settings.voice_store_dir,
                            tts_provider=str(settings.tts_provider),
                            # expressiveness (best-effort)
                            emotion_mode=str(settings.emotion_mode),
                            expressive=str(getattr(settings, "expressive", "off")),
                            expressive_strength=float(
                                getattr(settings, "expressive_strength", 0.5)
                            ),
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
                    is_pass2_outer = bool(str(two_pass_phase or "") == "pass2")
                    if (
                        tts_wav.exists()
                        and tts_manifest.exists()
                        and stage_is_done(ckpt, "tts")
                        and not (
                            isinstance(resynth, dict)
                            and str(resynth.get("type") or "") == "approved"
                        )
                        and not is_pass2_outer
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
                        # Force rerun on pass2.
                        if is_pass2_outer:
                            with suppress(Exception):
                                tts_wav.unlink(missing_ok=True)
                            with suppress(Exception):
                                tts_manifest.unlink(missing_ok=True)
                        if sched is None:
                            run_with_timeout(
                                "tts",
                                timeout_s=limits.timeout_tts_s,
                                fn=_tts_phase,
                                cancel_check=_cancel_check_sync,
                                cancel_exc=JobCanceled(),
                            )
                        else:
                            with sched.phase("tts"):
                                run_with_timeout(
                                    "tts",
                                    timeout_s=limits.timeout_tts_s,
                                    fn=_tts_phase,
                                    cancel_check=_cancel_check_sync,
                                    cancel_exc=JobCanceled(),
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
                    from dubbing_pipeline.stages.tts import _write_silence_wav  # type: ignore

                    # best-effort duration from diarization-timed segments
                    dur = max((float(s["end"]) for s in segments_for_mt), default=0.0)
                    _write_silence_wav(tts_wav, duration_s=dur)

            # Persist TTS manifest outside ephemeral work dir (best-effort).
            with suppress(Exception):
                from dubbing_pipeline.utils.io import atomic_copy

                analysis_dir = (base_dir / "analysis").resolve()
                analysis_dir.mkdir(parents=True, exist_ok=True)
                if (work_dir / "tts_manifest.json").exists():
                    atomic_copy(work_dir / "tts_manifest.json", analysis_dir / "tts_manifest.json")

            self.store.update(job_id, progress=0.95, message="TTS done")

            # Tier-2B canonicalization: if "resynth approved" was requested, persist the
            # generated clips into review/state.json as locked segments.
            if isinstance(resynth, dict) and str(resynth.get("type") or "") == "approved":
                try:
                    from dubbing_pipeline.review.ops import lock_from_tts_manifest

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
                    self.store.append_log(
                        job_id, f"[{now_utc()}] review lock-from-resynth failed: {ex}"
                    )
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
                    is_pass2_outer = bool(str(two_pass_phase or "") == "pass2")
                    if is_pass2_outer:
                        # Force rerun: invalidate checkpoint artifacts by removing existing outputs (best-effort).
                        with suppress(Exception):
                            final_mkv.unlink(missing_ok=True)
                        with suppress(Exception):
                            final_mp4.unlink(missing_ok=True)
                        with suppress(Exception):
                            out_mkv.unlink(missing_ok=True)
                        with suppress(Exception):
                            out_mp4.unlink(missing_ok=True)
                        with suppress(Exception):
                            (base_dir / "audio" / "final_mix.wav").unlink(missing_ok=True)
                        with suppress(Exception):
                            shutil.rmtree(base_dir / "mobile", ignore_errors=True)

                    if (stage_is_done(ckpt, "mix") or stage_is_done(ckpt, "mux")) and not is_pass2_outer:
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
                            profile=str(
                                (self.store.get(job_id).runtime or {}).get(
                                    "mix_profile", str(settings.mix_profile)
                                )
                                if self.store.get(job_id)
                                else str(settings.mix_profile)
                            ),
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
                            from dubbing_pipeline.audio.mix import MixParams, mix_dubbed_audio
                            from dubbing_pipeline.stages.export import (
                                export_hls,
                                export_m4a,
                                export_mkv,
                                export_mkv_multitrack,
                                export_mp4,
                            )
                            from dubbing_pipeline.utils.io import atomic_copy

                            bg = background_wav or Path(str(wav))
                            # If separation is on, background stem likely removes vocals. For detected music
                            # regions, preserve original audio (singing) by switching bed source.
                            if (
                                background_wav is not None
                                and music_regions_path_work is not None
                                and music_regions_path_work.exists()
                            ):
                                try:
                                    from dubbing_pipeline.audio.music_detect import (
                                        build_music_preserving_bed,
                                    )
                                    from dubbing_pipeline.utils.io import read_json

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
                                        lufs_target=float(
                                            (
                                                (self.store.get(job_id).runtime or {}).get(
                                                    "lufs_target"
                                                )
                                            )
                                            if self.store.get(job_id)
                                            and isinstance(self.store.get(job_id).runtime, dict)
                                            and "lufs_target"
                                            in (self.store.get(job_id).runtime or {})
                                            else getattr(settings, "lufs_target", -16.0)
                                        ),
                                        ducking=bool(getattr(settings, "ducking", True)),
                                        ducking_strength=float(
                                            (
                                                (self.store.get(job_id).runtime or {}).get(
                                                    "ducking_strength"
                                                )
                                            )
                                            if self.store.get(job_id)
                                            and isinstance(self.store.get(job_id).runtime, dict)
                                            and "ducking_strength"
                                            in (self.store.get(job_id).runtime or {})
                                            else getattr(settings, "ducking_strength", 1.0)
                                        ),
                                        limiter=bool(
                                            ((self.store.get(job_id).runtime or {}).get("limiter"))
                                            if self.store.get(job_id)
                                            and isinstance(self.store.get(job_id).runtime, dict)
                                            and "limiter" in (self.store.get(job_id).runtime or {})
                                            else getattr(settings, "limiter", True)
                                        ),
                                    ),
                                )
                                with suppress(Exception):
                                    atomic_copy(final_mix_wav, base_audio_dir / "final_mix.wav")

                                outs2: dict[str, Path] = {}
                                emit = set(cfg_mix.emit or ())
                                # Multi-track output (opt-in): write track artifacts + mux multi-audio MKV (preferred).
                                if bool(getattr(settings, "multitrack", False)):
                                    try:
                                        from dubbing_pipeline.audio.tracks import build_multitrack_artifacts

                                        tracks = build_multitrack_artifacts(
                                            job_dir=base_dir,
                                            original_wav=Path(str(wav)),
                                            dubbed_wav=final_mix_wav,
                                            dialogue_wav=tts_wav,
                                            background_wav=(
                                                bg if background_wav is not None else None
                                            ),
                                        )
                                        if (
                                            str(getattr(settings, "container", "mkv")).lower()
                                            == "mkv"
                                            and "mkv" in emit
                                        ):
                                            outs2["mkv"] = export_mkv_multitrack(
                                                video_in=video_in,
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
                                        elif (
                                            str(getattr(settings, "container", "mkv")).lower()
                                            == "mp4"
                                        ):
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
                                        self.store.append_log(
                                            job_id,
                                            f"[{now_utc()}] multitrack failed; continuing ({ex})",
                                        )

                                if "mkv" in emit and "mkv" not in outs2:
                                    outs2["mkv"] = export_mkv(
                                        video_in,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}.dub.mkv",
                                    )
                                if "mp4" in emit:
                                    outs2["mp4"] = export_mp4(
                                        video_in,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}.dub.mp4",
                                    )
                                if "fmp4" in emit:
                                    outs2["fmp4"] = export_mp4(
                                        video_in,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}.dub.frag.mp4",
                                        fragmented=True,
                                    )
                                if "hls" in emit:
                                    outs2["hls"] = export_hls(
                                        video_in,
                                        final_mix_wav,
                                        subs_srt_path,
                                        work_dir / f"{video_path.stem}_hls",
                                    )
                                return outs2

                            if sched is None:
                                outs = run_with_timeout(
                                    "mix",
                                    timeout_s=limits.timeout_mix_s,
                                    fn=_enhanced_phase,
                                    cancel_check=_cancel_check_sync,
                                    cancel_exc=JobCanceled(),
                                )
                            else:
                                with sched.phase("mux"):
                                    outs = run_with_timeout(
                                        "mix",
                                        timeout_s=limits.timeout_mix_s,
                                        fn=_enhanced_phase,
                                        cancel_check=_cancel_check_sync,
                                        cancel_exc=JobCanceled(),
                                    )
                        else:
                            if sched is None:
                                outs = run_with_timeout(
                                    "mix",
                                    timeout_s=limits.timeout_mix_s,
                                    fn=mix,
                                    kwargs={
                                        "video_in": video_in,
                                        "tts_wav": tts_wav,
                                        "srt": subs_srt_path,
                                        "out_dir": work_dir,
                                        "cfg": cfg_mix,
                                    },
                                    cancel_check=_cancel_check_sync,
                                    cancel_exc=JobCanceled(),
                                )
                            else:
                                with sched.phase("mux"):
                                    outs = run_with_timeout(
                                        "mix",
                                        timeout_s=limits.timeout_mix_s,
                                        fn=mix,
                                        kwargs={
                                            "video_in": video_in,
                                            "tts_wav": tts_wav,
                                            "srt": subs_srt_path,
                                            "out_dir": work_dir,
                                            "cfg": cfg_mix,
                                        },
                                        cancel_check=_cancel_check_sync,
                                        cancel_exc=JobCanceled(),
                                    )
                        out_mkv = outs.get("mkv", out_mkv)
                        out_mp4 = outs.get("mp4", out_mp4)
                        if bool(getattr(settings, "multitrack", False)):
                            try:
                                from dubbing_pipeline.audio.tracks import build_multitrack_artifacts
                                from dubbing_pipeline.stages.export import export_m4a, export_mkv_multitrack

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
                                    if (
                                        str(getattr(settings, "container", "mkv")).lower() == "mkv"
                                        and out_mkv
                                    ):
                                        out_mkv = export_mkv_multitrack(
                                            video_in=video_in,
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
                                            out_path=Path(out_mkv),
                                        )
                                    elif (
                                        str(getattr(settings, "container", "mkv")).lower() == "mp4"
                                    ):
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
                                self.store.append_log(
                                    job_id, f"[{now_utc()}] multitrack failed; continuing ({ex})"
                                )
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
                        run_with_timeout(
                            "mux",
                            timeout_s=limits.timeout_mux_s,
                            fn=mkv_export.mux,
                            kwargs={
                                "src_video": video_in,
                                "dub_wav": tts_wav,
                                "srt_path": subs_srt_path,
                                "out_mkv": out_mkv,
                                "job_id": job_id,
                            },
                            cancel_check=_cancel_check_sync,
                            cancel_exc=JobCanceled(),
                        )
                    else:
                        with sched.phase("mux"):
                            run_with_timeout(
                                "mux",
                                timeout_s=limits.timeout_mux_s,
                                fn=mkv_export.mux,
                                kwargs={
                                    "src_video": video_in,
                                    "dub_wav": tts_wav,
                                    "srt_path": subs_srt_path,
                                    "out_mkv": out_mkv,
                                    "job_id": job_id,
                                },
                                cancel_check=_cancel_check_sync,
                                cancel_exc=JobCanceled(),
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

            # Two-pass voice cloning: after pass1 mux, enqueue pass2 and stop (avoid re-running export/lipsync twice).
            if bool(two_pass_enabled) and str(two_pass_phase or "") != "pass2":
                try:
                    curj = self.store.get(job_id)
                    rt2 = dict((curj.runtime or {}) if curj else runtime)
                except Exception:
                    rt2 = dict(runtime or {})
                tp = rt2.get("two_pass") if isinstance(rt2.get("two_pass"), dict) else {}
                tp = dict(tp) if isinstance(tp, dict) else {}
                tp["phase"] = "pass2"
                tp["requested_at"] = time.time()
                tp["requested_by"] = "auto"
                rt2["two_pass"] = tp
                rt2["voice_clone_two_pass"] = True
                self.store.update(
                    job_id,
                    runtime=rt2,
                    state=JobState.QUEUED,
                    progress=0.96,
                    message="Queued (pass 2)",
                )
                self.store.append_log(job_id, f"[{now_utc()}] passA_complete")
                with suppress(Exception):
                    curj2 = self.store.get(job_id)
                    rt3 = dict((curj2.runtime or {}) if curj2 else rt2)
                    tp3 = rt3.get("two_pass") if isinstance(rt3.get("two_pass"), dict) else {}
                    tp3 = dict(tp3 or {})
                    mk = tp3.get("markers")
                    if not isinstance(mk, list):
                        mk = []
                    mk.append("passA_complete")
                    tp3["markers"] = mk
                    rt3["two_pass"] = tp3
                    self.store.update(job_id, runtime=rt3)
                self.store.append_log(job_id, f"[{now_utc()}] two_pass: queued pass2")
                # Submit via Redis backend or local scheduler.
                try:
                    qb = getattr(self, "queue_backend", None)
                    if qb is not None:
                        await qb.submit_job(
                            job_id=str(job_id),
                            user_id=str(getattr(job, "owner_id", "") or "") or None,
                            mode=str(job.mode),
                            device=str(job.device),
                            priority=120,
                            meta={"two_pass": "pass2", "user_role": str(getattr(rt2.get("user_role"), "value", "") or "")},
                        )
                    else:
                        sched2 = Scheduler.instance_optional()
                        if sched2 is not None:
                            from dubbing_pipeline.runtime.scheduler import JobRecord

                            sched2.submit(
                                JobRecord(
                                    job_id=job_id,
                                    mode=job.mode,
                                    device_pref=job.device,
                                    created_at=time.time(),
                                    priority=120,
                                )
                            )
                        else:
                            await self._q.put(job_id)
                except Exception:
                    with suppress(Exception):
                        await self._q.put(job_id)
                return

            # Mobile-friendly playback outputs (H.264/AAC MP4 + optional HLS).
            try:
                if is_pass2_outer:
                    _note_pass2_skip("mobile_outputs", "pass2_skip")
                    raise _Pass2Skip()
                if bool(getattr(settings, "mobile_outputs", True)):
                    from dubbing_pipeline.stages.export import export_mobile_hls, export_mobile_mp4

                    mobile_dir = (base_dir / "mobile").resolve()
                    mobile_dir.mkdir(parents=True, exist_ok=True)

                    # Prefer enhanced final mix when present, else TTS track.
                    dubbed_wav = (
                        (base_dir / "audio" / "final_mix.wav")
                        if (base_dir / "audio" / "final_mix.wav").exists()
                        else tts_wav
                    )
                    # Dubbed mobile MP4 (default)
                    run_with_timeout(
                        "export_mobile_mp4_dubbed",
                        timeout_s=limits.timeout_export_s,
                        fn=export_mobile_mp4,
                        kwargs={
                            "video_in": video_in,
                            "audio_wav": dubbed_wav if dubbed_wav.exists() else None,
                            "out_path": mobile_dir / "mobile.mp4",
                        },
                        cancel_check=_cancel_check_sync,
                        cancel_exc=JobCanceled(),
                    )
                    # Original mobile MP4 (user-selectable in UI)
                    run_with_timeout(
                        "export_mobile_mp4_original",
                        timeout_s=limits.timeout_export_s,
                        fn=export_mobile_mp4,
                        kwargs={
                            "video_in": video_in,
                            "audio_wav": None,
                            "out_path": mobile_dir / "original.mp4",
                        },
                        cancel_check=_cancel_check_sync,
                        cancel_exc=JobCanceled(),
                    )

                    if bool(getattr(settings, "mobile_hls", False)) and dubbed_wav.exists():
                        run_with_timeout(
                            "export_mobile_hls",
                            timeout_s=limits.timeout_export_s,
                            fn=export_mobile_hls,
                            kwargs={
                                "video_in": video_in,
                                "dub_wav": dubbed_wav,
                                "out_dir": mobile_dir / "hls",
                            },
                            cancel_check=_cancel_check_sync,
                            cancel_exc=JobCanceled(),
                        )
            except _Pass2Skip:
                pass
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] mobile outputs skipped: {ex}")

            # Tier-3A: optional lip-sync plugin (default off).
            try:
                if is_pass2_outer:
                    _note_pass2_skip("lipsync", "pass2_skip")
                mode = (
                    "off"
                    if is_pass2_outer
                    else str(getattr(settings, "lipsync", "off") or "off").strip().lower()
                )
                if mode != "off":
                    from dubbing_pipeline.plugins.lipsync.base import LipSyncRequest
                    from dubbing_pipeline.plugins.lipsync.registry import resolve_lipsync_plugin
                    from dubbing_pipeline.plugins.lipsync.wav2lip_plugin import _parse_bbox

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
                            scene_limited=bool(getattr(settings, "lipsync_scene_limited", False)),
                            sample_every_s=float(
                                getattr(settings, "lipsync_sample_every_s", 0.5) or 0.5
                            ),
                            min_face_ratio=float(
                                getattr(settings, "lipsync_min_face_ratio", 0.60) or 0.60
                            ),
                            min_range_s=float(getattr(settings, "lipsync_min_range_s", 2.0) or 2.0),
                            merge_gap_s=float(getattr(settings, "lipsync_merge_gap_s", 0.6) or 0.6),
                            max_frames=int(getattr(settings, "lipsync_max_frames", 600) or 600),
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
                    from dubbing_pipeline.qa.scoring import score_job

                    score_job(base_dir, enabled=True, write_outputs=True)
                    self.store.append_log(job_id, f"[{now_utc()}] qa: ok")
            except Exception:
                self.store.append_log(job_id, f"[{now_utc()}] qa failed; continuing")

            # Feature L: cross-episode drift snapshots + reports (offline, deterministic; best-effort).
            try:
                from dubbing_pipeline.reports.drift import write_drift_reports, write_drift_snapshot

                snap = write_drift_snapshot(
                    job_dir=base_dir,
                    video_path=video_path,
                    voice_memory_dir=Path(
                        getattr(settings, "voice_memory_dir", Path.cwd() / "data" / "voice_memory")
                    ).resolve(),
                    glossary_path=str(getattr(settings, "glossary_path", "") or ""),
                )
                write_drift_reports(job_dir=base_dir, snapshot_path=snap, compare_last_n=5)
                self.store.append_log(job_id, f"[{now_utc()}] drift_report: ok")
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] drift_report skipped: {ex}")

            # Feature B: per-job retention policy (default full => keep everything)
            try:
                curj = self.store.get(job_id)
                rt2 = dict((curj.runtime or {}) if curj else runtime)
                policy = (
                    str(rt2.get("cache_policy") or getattr(settings, "cache_policy", "full"))
                    .strip()
                    .lower()
                )
                retention_days = int(
                    rt2.get("retention_days") or getattr(settings, "retention_days", 0) or 0
                )
                dry_run = bool(rt2.get("retention_dry_run") or False)
                if policy in {"balanced", "minimal"} or retention_days > 0:
                    from dubbing_pipeline.storage.retention import apply_retention

                    rep = apply_retention(
                        base_dir,
                        policy,
                        retention_days=retention_days,
                        dry_run=dry_run,
                    )
                    self.store.append_log(
                        job_id,
                        f"[{now_utc()}] retention: policy={policy} dry_run={dry_run} deleted={len(rep.get('deleted', []))}",
                    )
            except Exception as ex:
                self.store.append_log(job_id, f"[{now_utc()}] retention failed; continuing: {ex}")

            self.store.update(
                job_id,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(final_mkv if final_mkv.exists() else out_mkv),
                output_srt=str(subs_srt_path) if subs_srt_path else "",
                work_dir=str(base_dir),
            )
            _auto_match_speakers_best_effort()
            if is_pass2_outer:
                self.store.append_log(job_id, f"[{now_utc()}] passB_complete")
                with suppress(Exception):
                    curj2 = self.store.get(job_id)
                    rt2 = dict((curj2.runtime or {}) if curj2 else runtime)
                    tp2 = rt2.get("two_pass") if isinstance(rt2.get("two_pass"), dict) else {}
                    tp2 = dict(tp2 or {})
                    mk = tp2.get("markers")
                    if not isinstance(mk, list):
                        mk = []
                    mk.append("passB_complete")
                    tp2["markers"] = mk
                    rt2["two_pass"] = tp2
                    self.store.update(job_id, runtime=rt2)
            # Best-effort library mirror + manifest (must never affect job success).
            with suppress(Exception):
                self._write_library_artifacts_best_effort(job_id=job_id, base_dir=base_dir)
            # Optional: job-finish notification (best-effort, no impact on pipeline success).
            with suppress(Exception):
                await self._notify_job_finished(job_id, state="DONE")
            # Clear resynth flag after completion.
            with suppress(Exception):
                curj = self.store.get(job_id)
                rt2 = dict((curj.runtime or {}) if curj else runtime)
                if "resynth" in rt2:
                    rt2.pop("resynth", None)
                # Clear two-pass request marker after successful completion.
                tp = rt2.get("two_pass") if isinstance(rt2.get("two_pass"), dict) else None
                if isinstance(tp, dict):
                    tp2 = dict(tp)
                    tp2.pop("request", None)
                    # Mark completion if we just finished pass2.
                    if str(tp2.get("phase") or "").strip().lower() == "pass2":
                        tp2["done_at"] = time.time()
                        tp2["phase"] = "done"
                    rt2["two_pass"] = tp2
                    self.store.update(job_id, runtime=rt2)
            jobs_finished.labels(state="DONE").inc()
            self.store.append_log(job_id, f"[{now_utc()}] done in {time.perf_counter()-t0:.2f}s")
            with suppress(Exception):
                audit.emit(
                    "job.finished",
                    user_id=str(job.owner_id or "") or None,
                    job_id=str(job_id),
                    meta={"state": "DONE"},
                )
            # Cleanup temp workdir (keep logs + checkpoint + final outputs in Output/<stem>/).
            with suppress(Exception):
                shutil.rmtree(work_dir, ignore_errors=True)
        except JobCanceled:
            self.store.append_log(job_id, f"[{now_utc()}] canceled")
            self.store.update(job_id, state=JobState.CANCELED, message="Canceled", error=None)
            jobs_finished.labels(state="CANCELED").inc()
            with suppress(Exception):
                audit.emit(
                    "job.canceled",
                    user_id=str(job.owner_id or "") or None,
                    job_id=str(job_id),
                    meta={"state": "CANCELED"},
                )
            # optional cleanup of in-progress outputs: leave as-is (ignored by state)
        except Exception as ex:
            self.store.append_log(job_id, f"[{now_utc()}] failed: {ex}")
            self.store.update(job_id, state=JobState.FAILED, message="Failed", error=str(ex))
            # Best-effort library mirror + manifest on failure as well.
            with suppress(Exception):
                self._write_library_artifacts_best_effort(job_id=job_id, base_dir=base_dir)
            # Optional: job-finish notification (best-effort).
            with suppress(Exception):
                await self._notify_job_finished(job_id, state="FAILED")
            jobs_finished.labels(state="FAILED").inc()
            pipeline_job_failed_total.inc()
            with suppress(Exception):
                audit.emit(
                    "job.failed",
                    user_id=str(job.owner_id or "") or None,
                    job_id=str(job_id),
                    meta={"state": "FAILED", "error": str(ex)},
                )
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
            # Best-effort cleanup of decrypted input (if any).
            with suppress(Exception):
                if decrypted_video is not None:
                    decrypted_video.unlink(missing_ok=True)

    def _write_library_artifacts_best_effort(self, *, job_id: str, base_dir: Path) -> None:
        """
        Best-effort creation of the grouped Library/ mirror and its manifest.json.
        Must never throw.
        """
        job = self.store.get(job_id)
        if job is None:
            return

        # Determine output candidates from the canonical Output dir.
        stem = Path(job.video_path).stem if job.video_path else base_dir.name
        master = None
        with suppress(Exception):
            p = Path(str(job.output_mkv or "")).resolve()
            if p.exists():
                master = p
        if master is None:
            for cand in [
                base_dir / f"{stem}.dub.mkv",
                base_dir / "dub.mkv",
                *list(base_dir.glob("*.dub.mkv")),
            ]:
                if cand.exists():
                    master = cand.resolve()
                    break

        mobile = None
        with suppress(Exception):
            p = (base_dir / "mobile" / "mobile.mp4").resolve()
            if p.exists():
                mobile = p

        hls_index = None
        with suppress(Exception):
            p = (base_dir / "mobile" / "hls" / "index.m3u8").resolve()
            if p.exists():
                hls_index = p
            else:
                p2 = (base_dir / "mobile" / "hls" / "master.m3u8").resolve()
                if p2.exists():
                    hls_index = p2

        logs_dir = (base_dir / "logs").resolve()
        qa_dir = (base_dir / "qa").resolve()

        # Create library dir; fall back to writing manifest under Output/ if it fails.
        try:
            from dubbing_pipeline.library.paths import ensure_library_dir, mirror_outputs_best_effort
            from dubbing_pipeline.library.manifest import write_manifest

            lib_dir = ensure_library_dir(job)
            if lib_dir is None:
                raise RuntimeError("library_dir_unavailable")

            mirror_outputs_best_effort(
                job=job,
                library_dir=lib_dir,
                master=master,
                mobile=mobile,
                hls_index=hls_index,
                output_dir=base_dir,
            )
            write_manifest(
                job=job,
                outputs={
                    "library_dir": str(lib_dir),
                    "master": str(master) if master else None,
                    "mobile": str(mobile) if mobile else None,
                    "hls_index": str(hls_index) if hls_index else None,
                    "logs_dir": str(logs_dir) if logs_dir.exists() else None,
                    "qa_dir": str(qa_dir) if qa_dir.exists() else None,
                },
                extra={"output_dir": str(base_dir)},
            )
            return
        except Exception as ex:
            with suppress(Exception):
                self.store.append_log(job_id, f"[{now_utc()}] library mirror failed: {ex}")

        # Fallback: write manifest under the canonical Output job dir (best-effort).
        try:
            from dubbing_pipeline.library.manifest import write_manifest

            out_manifest_dir = base_dir
            out_manifest_dir.mkdir(parents=True, exist_ok=True)
            write_manifest(
                job=job,
                outputs={
                    "library_dir": str(out_manifest_dir),
                    "master": str(master) if master else None,
                    "mobile": str(mobile) if mobile else None,
                    "hls_index": str(hls_index) if hls_index else None,
                    "logs_dir": str(logs_dir) if logs_dir.exists() else None,
                    "qa_dir": str(qa_dir) if qa_dir.exists() else None,
                },
                extra={"fallback": True, "output_dir": str(base_dir)},
            )
        except Exception:
            # Final fallback: no manifest.
            return

    async def _notify_job_finished(self, job_id: str, *, state: str) -> None:
        """
        Best-effort private notification hook (ntfy).
        Must never throw or affect job outcome.
        """
        settings = get_settings()
        if not bool(getattr(settings, "ntfy_enabled", False)):
            return

        job = self.store.get(job_id)
        if job is None:
            return

        # Privacy mode: if configured or if retention is minimal (data minimization), redact filenames.
        rt = dict(job.runtime or {})
        privacy_mode = str(rt.get("privacy_mode") or "").strip().lower()
        if not privacy_mode:
            try:
                policy = (
                    str(rt.get("cache_policy") or getattr(settings, "cache_policy", "full"))
                    .strip()
                    .lower()
                )
                privacy_mode = "minimal" if policy == "minimal" else ""
            except Exception:
                privacy_mode = ""
        privacy_on = privacy_mode not in {"", "0", "false", "off", "none"}

        try:
            from pathlib import Path as _Path

            filename = _Path(str(job.video_path or "")).name
        except Exception:
            filename = ""

        title = "Dubbing job finished" if privacy_on else (filename or "Dubbing job finished")
        msg = f"Status: {state}\nJob: {job_id}"

        # Click URL (optional): requires PUBLIC_BASE_URL to be configured.
        base = str(getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
        click = f"{base}/ui/jobs/{job_id}" if base else None

        tags = ["dubbing-pipeline", str(state).lower()]
        prio = 4 if str(state).upper() == "FAILED" else 3

        # Run sync notifier in a thread to avoid blocking the async worker.
        import asyncio as _asyncio

        def _send() -> bool:
            from dubbing_pipeline.notify.ntfy import notify as _notify

            return _notify(
                event=f"job.{str(state).lower()}",
                title=title,
                message=msg,
                url=click,
                tags=tags,
                priority=prio,
                user_id=str(job.owner_id or "") or None,
                job_id=str(job_id),
            )

        await _asyncio.to_thread(_send)
