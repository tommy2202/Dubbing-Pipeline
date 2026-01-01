## Preset voice library

Drop short, clean WAV samples for each preset voice under:

- `voices/presets/alice/*.wav`
- `voices/presets/bob/*.wav`

Guidelines:

- Use **WAV** files (mono or stereo is OK).
- 5–30 seconds of speech per preset works well (you can provide multiple files).
- Keep background noise low and avoid music.

Then build embeddings:

```bash
python tools/build_voice_db.py
```

This generates:

- `voices/presets.json`
- `voices/embeddings/<preset>.npy`

The TTS pipeline will use these presets when cloning isn’t available and will choose the closest preset per speaker (cosine similarity).
