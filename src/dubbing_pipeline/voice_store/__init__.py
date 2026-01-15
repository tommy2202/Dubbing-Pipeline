"""
Persistent, series-scoped voice store (opt-in).

Canonical layout (root is settings.voice_store_dir):

voice_store/
  <series_slug>/
    characters/
      <character_slug>/
        ref.wav
        refs/
          <job_id>_<ts>.wav
        meta.json
    index.json
"""

