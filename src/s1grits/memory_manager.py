"""
Memory management module

Provides system memory detection and batch strategy selection functions,
automatically choosing appropriate data batch processing strategies based
on available memory.
"""

from __future__ import annotations

import pandas as pd
from warnings import warn

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

# Memory strategy thresholds
MEM_THRESHOLD_LARGE_GB: float = 32.0    # RAM threshold for yearly batch strategy
MEM_THRESHOLD_MEDIUM_GB: float = 16.0  # RAM threshold for quarterly batch strategy
MEM_THRESHOLD_LARGE_SCENES: int = 500  # Scene count threshold for yearly strategy
MEM_THRESHOLD_MEDIUM_SCENES: int = 200 # Scene count threshold for quarterly strategy

# Fraction of a full tile that one downloaded burst-native scene occupies.
# The legacy full-tile path reprojects every scene to the full tile and stacks
# them, so its peak scales with the full tile per scene (fraction 1.0). The
# blockwise path keeps the raw burst-sized arrays plus a bounded per-block
# working set, so its peak scales with the burst footprint. Real runs show
# ~85 MB/scene downloaded vs ~354 MB/scene for a full-tile plane
# (7180x6166 float32 x2 pol), i.e. ~0.24; 0.25 is a slightly conservative model.
BLOCKWISE_SCENE_FRACTION: float = 0.25


def estimate_memory_demand_gb(
    n_scenes: int,
    tile_size: tuple[int, int] = (6930, 6162),
    *,
    blockwise: bool = False,
    safety: float = 1.5,
) -> float:
    """Estimate peak working-set memory (GB) for a batch of ``n_scenes``.

    ``blockwise=False`` models the legacy full-tile path: every scene is
    reprojected to the full tile and stacked (full tile per scene).
    ``blockwise=True`` models the blockwise smonthly path, whose peak is the
    downloaded burst-sized arrays plus a bounded block working set — roughly
    ``BLOCKWISE_SCENE_FRACTION`` of the full-tile figure per scene.
    """
    height, width = tile_size
    single_scene_mb = (height * width * 4 * 2) / (1024 ** 2)  # VV+VH, float32
    if blockwise:
        single_scene_mb *= BLOCKWISE_SCENE_FRACTION
    return (single_scene_mb * n_scenes * safety) / 1024

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    warn("psutil is not installed, automatic memory detection is unavailable. Please run: pip install psutil")


def detect_system_memory() -> float:
    """
    Detect available system memory (GB)

    Returns:
        float: Available memory in GB, returns 8.0 as a conservative estimate if psutil is unavailable
    """
    if not PSUTIL_AVAILABLE:
        warn("Using default memory estimate: 8 GB")
        return 8.0

    try:
        # Get available memory (bytes)
        available_mem_bytes = psutil.virtual_memory().available
        available_mem_gb = available_mem_bytes / (1024**3)

        logger.info("Detected available memory: %.2f GB", available_mem_gb)
        return available_mem_gb

    except Exception as e:
        warn(f"Memory detection failed: {e}, using default value 8 GB")
        return 8.0


def select_batch_strategy(
    available_memory_gb: float,
    n_scenes: int,
    tile_size: tuple[int, int] = (6930, 6162),
    *,
    blockwise: bool = False,
) -> str:
    """
    Automatically select batch strategy based on memory and data volume

    Args:
        available_memory_gb: Available memory (GB)
        n_scenes: Number of scenes
        tile_size: Dimension of a single tile (height, width), defaults to Guayas basin size
        blockwise: When True, use the blockwise-aware memory estimate (peak
            scales with burst footprint, not a full-tile stack per scene). This
            only affects the estimated-demand downgrade check; the scene/RAM
            threshold rules are unchanged.

    Returns:
        'yearly' | 'quarterly' | 'monthly'

    Rules:
    - memory >= 32 GB and n_scenes < 500: 'yearly'
    - memory >= 16 GB and n_scenes < 200: 'quarterly'
    - others: 'monthly'

    Memory estimation: see estimate_memory_demand_gb (blockwise-aware).
    """
    # Estimate total memory requirement (GB), including safety factor
    estimated_gb = estimate_memory_demand_gb(
        n_scenes, tile_size, blockwise=blockwise
    )

    logger.info(
        "Estimated memory demand: %.2f GB (based on %d scenes, %s path)",
        estimated_gb, n_scenes, "blockwise" if blockwise else "full-tile",
    )

    # Strategy selection logic
    if available_memory_gb >= MEM_THRESHOLD_LARGE_GB and n_scenes < MEM_THRESHOLD_LARGE_SCENES:
        strategy = 'yearly'
        logger.info("Selected strategy: %s (large memory mode)", strategy)
    elif available_memory_gb >= MEM_THRESHOLD_MEDIUM_GB and n_scenes < MEM_THRESHOLD_MEDIUM_SCENES:
        strategy = 'quarterly'
        logger.info("Selected strategy: %s (medium memory mode)", strategy)
    else:
        strategy = 'monthly'
        logger.info("Selected strategy: %s (memory saving mode)", strategy)

    # Extra check: if estimated memory exceeds 80% of available memory, force downgrade
    if estimated_gb > available_memory_gb * 0.8:
        if strategy == 'yearly':
            strategy = 'quarterly'
            logger.warning("Insufficient memory, downgrading strategy to: %s", strategy)
        elif strategy == 'quarterly':
            strategy = 'monthly'
            logger.warning("Insufficient memory, downgrading strategy to: %s", strategy)

    return strategy


def peak_batch_scene_counts(acq_dates) -> dict[str, int]:
    """Peak scenes held at once per candidate batch strategy.

    ``acq_dates`` is one entry per scene row (burst acquisition) — NOT unique
    dates — so counts reflect what a batch actually loads into RAM. Returns
    the maximum row count over any single year / quarter / month.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(list(acq_dates)))
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    if len(idx) == 0:
        return {'yearly': 0, 'quarterly': 0, 'monthly': 0}
    s = pd.Series(1, index=idx)
    return {
        'yearly': int(s.groupby(idx.year).sum().max()),
        'quarterly': int(s.groupby([idx.year, idx.quarter]).sum().max()),
        'monthly': int(s.groupby([idx.year, idx.month]).sum().max()),
    }


def select_batch_strategy_by_demand(
    available_memory_gb: float,
    acq_dates,
    tile_size: tuple[int, int] = (6930, 6162),
    *,
    blockwise: bool = False,
    resident_batches: int = 1,
) -> str:
    """Pick the coarsest batch strategy whose PEAK batch fits the RAM budget.

    This is the demand-aware replacement for the threshold rules in
    :func:`select_batch_strategy`, which gate on the TOTAL scene count of the
    run — a quantity that says nothing about how many scenes one batch holds,
    so any full-archive run degrades to 'monthly' regardless of RAM. Here the
    demand model is per-batch: the actual acquisition-date histogram gives
    the worst-case batch size under each strategy, and the coarsest strategy
    whose estimated peak fits 80% of ``available_memory_gb`` wins.

    ``resident_batches`` is the number of batches simultaneously resident:
    1 for the plain serial loop, 2 when download prefetch overlaps batch N+1
    with batch N's compute.
    """
    peaks = peak_batch_scene_counts(acq_dates)
    resident = max(1, int(resident_batches))
    chosen = 'monthly'
    for strategy in ('yearly', 'quarterly', 'monthly'):
        demand_gb = estimate_memory_demand_gb(
            peaks[strategy], tile_size, blockwise=blockwise
        ) * resident
        fits = demand_gb <= available_memory_gb * 0.8
        logger.info(
            "Demand-aware strategy check: %s peak=%d scenes -> %.1f GB "
            "(x%d resident, %s path) vs budget %.1f GB -> %s",
            strategy, peaks[strategy], demand_gb, resident,
            "blockwise" if blockwise else "full-tile",
            available_memory_gb * 0.8, "fits" if fits else "too large",
        )
        if fits:
            chosen = strategy
            break
    logger.info("Selected strategy: %s (demand-aware)", chosen)
    return chosen


def chunk_time_by_strategy(
    dates: list[pd.Timestamp],
    strategy: str
) -> list[list[pd.Timestamp]]:
    """
    Group dates by strategy

    Args:
        dates: List of dates (pd.Timestamp)
        strategy: Batch strategy ('yearly' | 'quarterly' | 'monthly')

    Returns:
        [[dates_batch1], [dates_batch2], ...]

    Raises:
        ValueError: If strategy is not one of 'yearly', 'quarterly', or 'monthly'.

    Example:
        dates = [2024-01-01, 2024-02-01, ..., 2024-12-01]
        strategy = 'quarterly'
        Returns: [[Q1_dates], [Q2_dates], [Q3_dates], [Q4_dates]]
    """
    if not dates:
        return []

    # Ensure dates is pd.DatetimeIndex
    dates_idx = pd.DatetimeIndex(dates).sort_values()

    batches = []

    if strategy == 'yearly':
        # Group by year
        df = pd.DataFrame({'date': dates_idx})
        df['year'] = df['date'].dt.year

        for year, group in df.groupby('year'):
            batches.append(group['date'].tolist())

    elif strategy == 'quarterly':
        # Group by quarter
        df = pd.DataFrame({'date': dates_idx})
        df['year'] = df['date'].dt.year
        df['quarter'] = df['date'].dt.quarter

        for (year, quarter), group in df.groupby(['year', 'quarter']):
            batches.append(group['date'].tolist())

    elif strategy == 'monthly':
        # Group by month
        df = pd.DataFrame({'date': dates_idx})

        # Remove timezone if present to avoid tz_localize error
        if df['date'].dt.tz is not None:
            df['year_month'] = df['date'].dt.tz_convert(None).dt.to_period('M')
        else:
            df['year_month'] = df['date'].dt.to_period('M')

        for ym, group in df.groupby('year_month'):
            batches.append(group['date'].tolist())

    else:
        raise ValueError(f"Invalid strategy: {strategy}. Must be 'yearly', 'quarterly', or 'monthly'.")

    logger.info("Divided into %d batches (strategy: %s)", len(batches), strategy)
    return batches


def get_memory_strategy_from_config(
    config: dict, n_scenes: int = 100, *, blockwise: bool = False,
    acq_dates=None, resident_batches: int = 1,
) -> str:
    """
    Get or automatically select memory strategy from configuration file

    Args:
        config: Configuration dictionary
        n_scenes: Number of scenes (for automatic selection)
        blockwise: When True, use the blockwise-aware memory estimate for the
            'auto' path (the blockwise smonthly writer never builds a full-tile
            stack, so it can sustain a coarser batch strategy at the same RAM).
        acq_dates: Optional per-scene acquisition timestamps. When provided,
            'auto' uses the demand-aware selector (peak-batch demand from the
            real date histogram) instead of the legacy total-scene-count
            thresholds, which degrade every full-archive run to 'monthly'.
        resident_batches: Batches simultaneously resident (2 with download
            prefetch enabled). Demand-aware path only.

    Returns:
        'yearly' | 'quarterly' | 'monthly'
    """
    memory_config = config.get('memory', {})
    batch_strategy = memory_config.get('batch_strategy', 'auto')

    if batch_strategy == 'auto':
        # Automatic detection
        max_memory_gb = memory_config.get('max_memory_gb', 'auto')

        if max_memory_gb == 'auto':
            available_mem = detect_system_memory()
        else:
            available_mem = float(max_memory_gb)
            logger.info("Using configured memory limit: %.1f GB", available_mem)

        if acq_dates is not None and len(acq_dates) > 0:
            strategy = select_batch_strategy_by_demand(
                available_mem, acq_dates,
                blockwise=blockwise, resident_batches=resident_batches,
            )
        else:
            strategy = select_batch_strategy(
                available_mem, n_scenes, blockwise=blockwise
            )
    else:
        # Use manually configured strategy
        strategy = batch_strategy
        logger.info("Using manually configured strategy: %s", strategy)

    return strategy
