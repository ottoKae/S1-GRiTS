"""Built-in user-facing configuration templates.

Templates live in Python source so they are available from both a source
checkout and the universal wheel. The CLI and web console deliberately share
the same value; changing defaults in one entry point must not silently create
a different processing contract in the other.
"""

DEFAULT_SCENES_CONFIG = """\
workflow: "scenes"

roi:
  manual_mgrs_tiles:
    - "17MPU"
  flight_direction: "ASCENDING"
  polarization: "VV+VH"

time:
  full: 2026            # Full archive up to this year.
  # Alternative: years: [2026] and months: [1]

output:
  base_dir: "./output"
  existing_store: "resume"
  existing_month: "skip"
  formats: {cog: true, preview: true}

processing:
  target_resolution: 30.0
  resampling_method: auto
  tile_clip: true
  monthly:
    enabled: true
    only: true
    composite_method: "nanmedian"
    generate_cog: true
    generate_preview: true
    blockwise_threads: 2

memory:
  max_memory_gb: "auto"
  batch_strategy: "auto"
  max_download_workers: 8
  download_prefetch: true

parallel:
  enabled: true
  max_workers: 2
"""


__all__ = ["DEFAULT_SCENES_CONFIG"]
