from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from anime_v2.jobs.models import Job, JobState, now_utc
from anime_v2.jobs.store import JobStore
from anime_v2.jobs.limits import get_limits
from anime_v2.jobs.watchdog import PhaseTimeout, run_with_timeout
from anime_v2.config import get_settings
from anime_v2.ops.metrics import (
    job_errors,
    jobs_finished,
    pipeline_job_degraded_total,
    pipeline_job_failed_total,
    pipeline_mux_seconds,
    pipeline_transcribe_seconds,
    pipeline_tts_seconds,
    tts_seconds,
    time_hist,
    whisper_seconds,
)
from anime_v2.stages import audio_extractor, mkv_export, tts
from anime_v2.stages.character_store import CharacterStore
from anime_v2.stages.diarization import DiarizeConfig, diarize as diarize_v2
from anime_v2.stages.mixing import MixConfig, mix
from anime_v2.utils.embeds import ecapa_embedding
from anime_v2.stages.transcription import transcribe
from anime_v2.stages.translation import TranslationConfig, translate_segments
from anime_v2.utils.ffmpeg_safe import extract_audio_mono_16k
from anime_v2.utils.log import logger
from anime_v2.utils.paths import output_dir_for
from anime_v2.utils.time import format_srt_timestamp
from anime_v2.utils.net import install_egress_policy
from anime_v2.runtime.scheduler import Scheduler
from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from anime_v2.utils.circuit import Circuit
from anime_v2.utils.hashio import hash_audio_from_video


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
    if not srt_path.exists():
        return []
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    blocks = [b for b in text.split("\n\n") if b.strip()]
    cues: list[dict] = []

    def parse_ts(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start_s, end_s = [p.strip() for p in lines[1].split("-->", 1)]
        start = parse_ts(start_s)
        end = parse_ts(end_s)
        cue_text = " ".join(lines[2:]).strip() if len(lines) > 2 else ""
        cues.append({"start": start, "end": end, "text": cue_text})
    return cues


def _assign_speakers(cues: list[dict], diar_segments: list[dict] | None) -> list[dict]:
    diar_segments = diar_segments or []
    out: list[dict] = []
    for c in cues:
        start = float(c["start"])
        end = float(c["end"])
        mid = (start + end) / 2.0
        speaker_id = "Speaker1"
        for seg in diar_segments:
            try:
                if float(seg["start"]) <= mid <= float(seg["end"]):
                    speaker_id = str(seg.get("speaker_id") or speaker_id)
                    break
            except Exception:
                continue
        out.append({"start": start, "end": end, "speaker_id": speaker_id, "text": str(c.get("text", "") or "")})
    return out


def _write_srt(lines: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, l in enumerate(lines, 1):
            st = format_srt_timestamp(float(l["start"]))
            en = format_srt_timestamp(float(l["end"]))
            txt = str(l.get("text", "") or "").strip()
            f.write(f"{i}\n{st} --> {en}\n{txt}\n\n")


class JobQueue:
    def __init__(self, store: JobStore, *, concurrency: int = 1, app_root: Path | None = None) -> None:
        self.store = store
        self.concurrency = max(1, int(concurrency))
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._cancel: set[str] = set()
        self._cancel_lock = asyncio.Lock()
        if app_root is not None:
            self.app_root = app_root.resolve()
        else:
            env = os.environ.get("APP_ROOT")
            if env:
                self.app_root = Path(env).resolve()
            elif Path("/app").exists():
                self.app_root = Path("/app").resolve()
            else:
                self.app_root = Path.cwd().resolve()

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

                        sched.submit(JobRecord(job_id=j.id, mode=j.mode, device_pref=j.device, created_at=time.time(), priority=100))
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
        try:
            # Wait for queued items to be processed.
            await asyncio.wait_for(self._q.join(), timeout=float(timeout_s))
        except Exception:
            pass
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
            self.store.update(job_id, state=JobState.CANCELED, progress=0.0, message="Canceled before start")
            return

        # Establish work/log paths before writing logs.
        video_path = Path(job.video_path)
        base_dir = output_dir_for(video_path, self.app_root)
        base_dir.mkdir(parents=True, exist_ok=True)
        # Temp artifacts live under Output/<stem>/work/<job_id>/...
        work_dir = (base_dir / "work" / job_id).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        log_path = base_dir / "job.log"
        ckpt_path = base_dir / ".checkpoint.json"
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or {}
        # runtime report fields persisted on the job
        runtime = dict(job.runtime or {})
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
                            kwargs={"video": video_path, "out_dir": work_dir, "wav_out": work_dir / "audio.wav", "job_id": job_id},
                        )
                    else:
                        with sched.phase("audio"):
                            wav = run_with_timeout(
                                "audio_extract",
                                timeout_s=limits.timeout_audio_s,
                                fn=audio_extractor.extract,
                                args=(),
                                kwargs={"video": video_path, "out_dir": work_dir, "wav_out": work_dir / "audio.wav", "job_id": job_id},
                            )
                    try:
                        write_ckpt(job_id, "audio", {"audio_wav": Path(str(wav))}, {"work_dir": str(work_dir)}, ckpt_path=ckpt_path)
                        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                    except Exception:
                        pass
            except PhaseTimeout as ex:
                job_errors.labels(stage="audio_timeout").inc()
                raise RuntimeError(str(ex))
            self.store.update(job_id, progress=0.10, message="Audio extracted")
            await self._check_canceled(job_id)

            # b) diarize.identify (~0.25) (optional)
            diar_json_work = work_dir / "diarization.work.json"
            diar_json_public = base_dir / "diarization.json"
            diar_segments: list[dict] = []
            speaker_embeddings: dict[str, str] = {}
            try:
                self.store.update(job_id, progress=0.12, message="Diarizing speakers")
                self.store.append_log(job_id, f"[{now_utc()}] diarize")
                cfg = DiarizeConfig(diarizer=os.environ.get("DIARIZER", "auto"))
                utts = diarize_v2(str(wav), device=_select_device(job.device), cfg=cfg)

                seg_dir = work_dir / "segments"
                seg_dir.mkdir(parents=True, exist_ok=True)
                by_label: dict[str, list[tuple[float, float, Path]]] = {}
                for i, u in enumerate(utts):
                    s = float(u["start"])
                    e = float(u["end"])
                    lab = str(u["speaker"])
                    seg_wav = seg_dir / f"{i:04d}_{lab}.wav"
                    try:
                        extract_audio_mono_16k(src=Path(str(wav)), dst=seg_wav, start_s=s, end_s=e, timeout_s=120)
                    except Exception:
                        seg_wav = Path(str(wav))
                    by_label.setdefault(lab, []).append((s, e, seg_wav))

                show = os.environ.get("SHOW_ID") or video_path.stem
                sim = float(os.environ.get("CHAR_SIM_THRESH", "0.72"))
                store_chars = CharacterStore.default()
                store_chars.load()
                thresholds = {"sim": sim}
                lab_to_char: dict[str, str] = {}
                for lab, segs in by_label.items():
                    rep_wav = sorted(segs, key=lambda t: (t[1] - t[0]), reverse=True)[0][2]
                    emb = ecapa_embedding(rep_wav, device=_select_device(job.device))
                    if emb is None:
                        lab_to_char[lab] = lab
                        continue
                    cid = store_chars.match_or_create(emb, show_id=show, thresholds=thresholds)
                    store_chars.link_speaker_wav(cid, str(rep_wav))
                    lab_to_char[lab] = cid
                store_chars.save()

                diar_segments = []
                for lab, segs in by_label.items():
                    for s, e, wav_p in segs:
                        diar_segments.append(
                            {"start": s, "end": e, "diar_label": lab, "speaker_id": lab_to_char.get(lab, lab), "wav_path": str(wav_p)}
                        )

                from anime_v2.utils.io import write_json

                # Work version includes wav_path for TTS voice selection.
                write_json(diar_json_work, {"audio_path": str(wav), "segments": diar_segments, "speaker_embeddings": speaker_embeddings})
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
                self.store.update(job_id, progress=0.25, message=f"Diarized ({len(set(s.get('speaker_id') for s in diar_segments))} speakers)")
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
            self.store.append_log(job_id, f"[{now_utc()}] transcribe model={model_name} device={device}")
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
                srt_public.write_bytes(srt_out.read_bytes())
                if srt_out.with_suffix(".json").exists():
                    srt_public.with_suffix(".json").write_bytes(srt_out.with_suffix(".json").read_bytes())
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
                [{"start": float(s["start"]), "end": float(s["end"]), "speaker": str(s.get("speaker_id") or "SPEAKER_01")} for s in diar_segments],
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
                            logprob = sum(lp * w for lp, w in zip(lp_parts, w_parts)) / tot
                    segments_for_mt.append({"start": u["start"], "end": u["end"], "speaker": u["speaker"], "text": text_src, "logprob": logprob})
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
            if do_translate:
                self.store.update(job_id, progress=0.62, message="Translating subtitles")
                self.store.append_log(job_id, f"[{now_utc()}] translate src={job.src_lang} tgt={job.tgt_lang}")
                try:
                    from anime_v2.utils.io import write_json

                    cfg = TranslationConfig(
                        mt_engine=(os.environ.get("MT_ENGINE") or "auto"),
                        mt_lowconf_thresh=float(os.environ.get("MT_LOWCONF_THRESH", "-0.45")),
                        glossary_path=os.environ.get("GLOSSARY"),
                        style_path=os.environ.get("STYLE"),
                        show_id=os.environ.get("SHOW_ID") or video_path.stem,
                        whisper_model=model_name,
                        audio_path=str(wav),
                        device=device,
                    )
                    translated_segments = translate_segments(segments_for_mt, src_lang=job.src_lang, tgt_lang=job.tgt_lang, cfg=cfg)
                    write_json(translated_json, {"src_lang": job.src_lang, "tgt_lang": job.tgt_lang, "segments": translated_segments})
                    srt_lines = [{"start": s["start"], "end": s["end"], "speaker_id": s["speaker"], "text": s["text"]} for s in translated_segments]
                    _write_srt(srt_lines, translated_srt)
                    subs_srt_path = translated_srt
                    self.store.update(job_id, progress=0.75, message="Translation done")
                except Exception as ex:
                    self.store.append_log(job_id, f"[{now_utc()}] translate failed: {ex}")
                    self.store.update(job_id, progress=0.75, message="Translation failed (using original text)")
            else:
                self.store.update(job_id, progress=0.75, message="Translation skipped")
            await self._check_canceled(job_id)

            # e) tts.synthesize aligned track (~0.95)
            tts_wav = work_dir / f"{video_path.stem}.tts.wav"

            def on_tts_progress(done: int, total: int) -> None:
                # map [0..1] => [0.76..0.95]
                frac = 0.0 if total <= 0 else float(done) / float(total)
                self.store.update(job_id, progress=0.76 + 0.19 * frac, message=f"TTS {done}/{total}")

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
                        if voice_map_json is not None:
                            os.environ["VOICE_MAP_JSON"] = str(voice_map_json)
                        return tts.run(
                            out_dir=work_dir,
                            translated_json=translated_json if translated_json.exists() else None,
                            diarization_json=diar_json_work if diar_json_work.exists() else None,
                            wav_out=tts_wav,
                            # callbacks omitted (not picklable); progress updates remain coarse for this phase
                            progress_cb=None,
                            cancel_cb=None,
                            max_stretch=float(os.environ.get("MAX_STRETCH", "0.15")),
                            job_id=job_id,
                            audio_hash=audio_hash,
                        )

                    # checkpoint-aware skip
                    tts_manifest = work_dir / "tts_manifest.json"
                    if tts_wav.exists() and tts_manifest.exists() and stage_is_done(ckpt, "tts"):
                        self.store.append_log(job_id, f"[{now_utc()}] tts (checkpoint hit)")
                    else:
                        if sched is None:
                            run_with_timeout("tts", timeout_s=limits.timeout_tts_s, fn=_tts_phase)
                        else:
                            with sched.phase("tts"):
                                run_with_timeout("tts", timeout_s=limits.timeout_tts_s, fn=_tts_phase)
                        try:
                            rt = dict((self.store.get(job_id).runtime or {}) if self.store.get(job_id) else runtime)
                            rt.setdefault("breaker_state", {})
                            rt["breaker_state"]["tts"] = Circuit.get("tts").snapshot().state
                            rt.setdefault("attempts", {})
                            rt["attempts"]["tts"] = int(rt["attempts"].get("tts", 0)) + 1
                            self.store.update(job_id, runtime=rt)
                        except Exception:
                            pass
                        try:
                            write_ckpt(job_id, "tts", {"tts_wav": tts_wav, "manifest": tts_manifest}, {"work_dir": str(work_dir)}, ckpt_path=ckpt_path)
                            ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                        except Exception:
                            pass
                    tts_seconds.observe(max(0.0, time.perf_counter() - t_tts0))
                    dt = elapsed()
                    if dt > float(settings.budget_tts_sec):
                        _mark_degraded("budget_tts_exceeded")
            except tts.TTSCanceled:
                job_errors.labels(stage="tts").inc()
                raise JobCanceled()
            except PhaseTimeout as ex:
                job_errors.labels(stage="tts_timeout").inc()
                self.store.append_log(job_id, f"[{now_utc()}] tts watchdog timeout: {ex}")
                raise RuntimeError(str(ex))
            except JobCanceled:
                job_errors.labels(stage="tts").inc()
                raise
            except Exception as ex:
                job_errors.labels(stage="tts").inc()
                self.store.append_log(job_id, f"[{now_utc()}] tts failed: {ex} (continuing with silence)")
                # Silence track is already best-effort within tts.run; ensure file exists.
                if not tts_wav.exists():
                    from anime_v2.stages.tts import _write_silence_wav  # type: ignore

                    # best-effort duration from diarization-timed segments
                    dur = max((float(s["end"]) for s in segments_for_mt), default=0.0)
                    _write_silence_wav(tts_wav, duration_s=dur)

            self.store.update(job_id, progress=0.95, message="TTS done")
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
                        cfg_mix = MixConfig(
                            profile=os.environ.get("MIX_PROFILE", "streaming"),
                            separate_vocals=bool(int(os.environ.get("SEPARATE_VOCALS", "0") or "0")),
                            emit=tuple(
                                sorted(
                                    {
                                        "mkv",
                                        "mp4",
                                        *[
                                            p.strip().lower()
                                            for p in (os.environ.get("EMIT_FORMATS") or os.environ.get("EMIT") or "mkv,mp4").split(",")
                                            if p.strip()
                                        ],
                                    }
                                )
                            ),
                        )
                        if sched is None:
                            outs = mix(video_in=video_path, tts_wav=tts_wav, srt=subs_srt_path, out_dir=work_dir, cfg=cfg_mix)
                        else:
                            with sched.phase("mux"):
                                outs = mix(video_in=video_path, tts_wav=tts_wav, srt=subs_srt_path, out_dir=work_dir, cfg=cfg_mix)
                        out_mkv = outs.get("mkv", out_mkv)
                        out_mp4 = outs.get("mp4", out_mp4)
                        try:
                            art = {"mkv": out_mkv}
                            if out_mp4 and Path(out_mp4).exists():
                                art["mp4"] = out_mp4
                            write_ckpt(job_id, "mix", art, {"work_dir": str(work_dir)}, ckpt_path=ckpt_path)
                            ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
                        except Exception:
                            pass
                except Exception as ex:
                    self.store.append_log(job_id, f"[{now_utc()}] mix failed: {ex} (falling back to mux)")
                    if sched is None:
                        mkv_export.mux(src_video=video_path, dub_wav=tts_wav, srt_path=subs_srt_path, out_mkv=out_mkv, job_id=job_id)
                    else:
                        with sched.phase("mux"):
                            mkv_export.mux(src_video=video_path, dub_wav=tts_wav, srt_path=subs_srt_path, out_mkv=out_mkv, job_id=job_id)
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
            # Update checkpoint to point at final artifacts (so a restart doesn't reference temp paths).
            try:
                art2 = {"mkv": final_mkv}
                if final_mp4.exists():
                    art2["mp4"] = final_mp4
                write_ckpt(job_id, "mix", art2, {"work_dir": str(base_dir)}, ckpt_path=ckpt_path)
                ckpt = read_ckpt(job_id, ckpt_path=ckpt_path) or ckpt
            except Exception:
                pass

            self.store.update(
                job_id,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(final_mkv if final_mkv.exists() else out_mkv),
                output_srt=str(subs_srt_path) if subs_srt_path else "",
                work_dir=str(base_dir),
            )
            jobs_finished.labels(state="DONE").inc()
            self.store.append_log(job_id, f"[{now_utc()}] done in {time.perf_counter()-t0:.2f}s")
            # Cleanup temp workdir (keep logs + checkpoint + final outputs in Output/<stem>/).
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
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
            logger.info("job %s finished state=%s in %.2fs", job_id, (self.store.get(job_id).state if self.store.get(job_id) else "unknown"), dt)
            try:
                if sched is not None:
                    sched.on_job_done(job_id)
            except Exception:
                pass

