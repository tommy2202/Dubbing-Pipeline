## Quality report

- **Score**: 62.3/100
- **Segments**: 4
- **Counts**: fail=5 warn=3 info=1

## Top issues

- **seg 3** [fail] `audio_clipping`: Audio appears clipped (peak too hot).  
  **Suggested**: Lower energy, enable limiter, or regenerate this segment.
- **seg 4** [fail] `low_asr_confidence`: Low ASR/MT confidence (-1.30).  
  **Suggested**: Review transcript/translation for this segment; consider manual edit + regen + lock.
- **seg 1** [fail] `segment_overlap`: Segment audio likely overlaps the next segment.  
  **Suggested**: Regenerate with pacing/shorter text; verify segment boundaries.
- **seg 2** [fail] `speaking_rate`: Speaking rate high (11.00 wps).  
  **Suggested**: Shorten translation, enable timing-fit/pacing, or regenerate this segment.
- **seg 1** [fail] `alignment_drift`: Audio duration exceeds segment window (1.60s > 1.00s).  
  **Suggested**: Enable pacing or regenerate with shorter text; if locked, unlock/regenerate then re-lock.
- **seg 2** [warn] `speaker_flip_suspicion`: Frequent speaker changes in a short window (possible diarization flip).  
  **Suggested**: Check diarization/voice map; consider locking corrected segments.
- **seg 3** [warn] `speaker_flip_suspicion`: Frequent speaker changes in a short window (possible diarization flip).  
  **Suggested**: Check diarization/voice map; consider locking corrected segments.
- **seg 4** [warn] `speaker_flip_suspicion`: Frequent speaker changes in a short window (possible diarization flip).  
  **Suggested**: Check diarization/voice map; consider locking corrected segments.
- **seg 2** [info] `music_overlap`: Segment overlaps detected music region (dialogue may be suppressed).  
  **Suggested**: If false-positive, lower music threshold or disable music-detect.

