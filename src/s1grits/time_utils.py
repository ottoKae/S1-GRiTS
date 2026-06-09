"""
Time range parsing utility module

Provides parsing functionality for time range configurations.

Supports two config schemas:

  New schema (preferred):
    time:
      full: 2026             # Mode A: full archive, auto-detect start, process to end_year
      # -- OR --
      years: [2025, 2026]    # Mode B: specific years
      months: [1, 2]         # optional month filter for Mode B

  Legacy schema (still accepted):
    time_range:
      mode: 'full'
      full_mode:
        end_year: 2026
      # -- OR --
      mode: 'years'
      years: [2025, 2026]
      months: [1, 2]
"""

from __future__ import annotations

import calendar
from datetime import date as _date
import pandas as pd
import asf_search as asf
from warnings import warn

from s1grits.logger_config import get_logger

logger = get_logger(__name__)


def _is_future_or_current_month(year: int, month: int, allow_current: bool = False) -> bool:
    """Return True if (year, month) should be skipped due to being current or future.

    Args:
        year: The year to check.
        month: The month to check.
        allow_current: If True, the current (incomplete) month is allowed and only
                       strictly future months are blocked. Default is False (monthly
                       workflow behavior: block current month until it is complete).
    """
    today = _date.today()
    if allow_current:
        # Block only strictly future months; current month is allowed
        return (year, month) > (today.year, today.month)
    return (year, month) >= (today.year, today.month)


def parse_time_range_config(
    config: dict, wkt: str, allow_current_month: bool = False
) -> list[tuple[str, str]]:
    """
    Parse the time range from the configuration dictionary.

    Accepts both the new 'time' key schema and the legacy 'time_range' schema.

    Args:
        config: Full configuration dictionary.
        wkt: WKT string used for ASF earliest-date detection (full mode).

    Returns:
        List of (start_date, end_date) tuples (YYYY-MM-DD strings).

    Raises:
        KeyError: If neither 'time' nor 'time_range' section is present, or if
                  required sub-keys are missing.
        ValueError: If mode is invalid or months specification is out of range.

    New schema examples:
        # Full archive
        time:
          full: 2026

        # Specific years, all months
        time:
          years: [2022, 2023, 2024]

        # Specific years, selected months
        time:
          years: [2024]
          months: [10, 11, 12]

    Legacy schema examples:
        time_range:
          mode: 'full'
          full_mode:
            end_year: 2026

        time_range:
          mode: 'years'
          years: [2024]
          months: [10, 11, 12]
    """
    # ── Resolve config block (new 'time' key takes priority) ──────────────────
    if 'time' in config:
        time_config = config['time']
        _schema = 'new'
    elif 'time_range' in config:
        time_config = config['time_range']
        _schema = 'legacy'
    else:
        raise KeyError("Configuration must contain a 'time' (or legacy 'time_range') section.")

    # ── Detect mode ───────────────────────────────────────────────────────────
    if _schema == 'new':
        if 'years' in time_config:
            mode = 'years'
        elif 'full' in time_config:
            mode = 'full'
            end_year = int(time_config['full'])
        else:
            raise KeyError(
                "Under 'time:', specify either 'full: <end_year>' or 'years: [...]'."
            )
    else:
        # Legacy schema
        mode = time_config.get('mode', '')
        if mode == 'full':
            end_year = time_config['full_mode']['end_year']
        elif mode != 'years':
            raise ValueError(
                f"Invalid time_range.mode: '{mode}'. Must be 'full' or 'years'."
            )

    # ── Mode A: Full archive ──────────────────────────────────────────────────
    if mode == 'full':
        polarization = config['roi'].get('polarization', 'VV+VH')
        logger.info("Auto-detecting earliest available RTC-S1 data...")

        manual_tiles = config['roi'].get('manual_mgrs_tiles')
        if manual_tiles:
            earliest_date = detect_earliest_available_date_from_mgrs_tiles(
                manual_tiles, polarization
            )
        else:
            earliest_date = detect_earliest_available_date(wkt, polarization)

        if earliest_date is None:
            warn("Unable to detect earliest available data; defaulting to 2014-01-01.")
            earliest_date = "2014-01-01"

        # Clip end date to the last fully completed month so we never request
        # data for the current (incomplete) month or any future month.
        today = _date.today()
        last_complete_month = today.month - 1
        if last_complete_month == 0:
            last_complete_year = today.year - 1
            last_complete_month = 12
        else:
            last_complete_year = today.year
        last_day = calendar.monthrange(last_complete_year, last_complete_month)[1]
        effective_end = _date(last_complete_year, last_complete_month, last_day)
        config_end = _date(end_year, 12, 31)
        if config_end > effective_end:
            logger.warning(
                "Clipping end date from %s to %s (last fully completed month; today is %s)",
                config_end.isoformat(), effective_end.isoformat(), today.isoformat(),
            )
            end_date = effective_end.isoformat()
        else:
            end_date = f"{end_year}-12-31"

        logger.info("Time range: %s to %s", earliest_date, end_date)
        return [(earliest_date, end_date)]

    # ── Mode B: Specific years ────────────────────────────────────────────────
    years = time_config['years']
    months = time_config.get('months', None)

    if months is not None:
        if not isinstance(months, list) or not all(1 <= m <= 12 for m in months):
            raise ValueError(
                f"Invalid months specification: {months}. "
                "Must be a list of integers between 1 and 12."
            )
        months = sorted(months)

    time_ranges = []
    for year in years:
        if months is None:
            today = _date.today()
            if year > today.year:
                logger.warning(
                    "Skipping year %d: entirely in the future (today is %s)",
                    year, today.isoformat(),
                )
                continue
            time_ranges.append((f"{year}-01-01", f"{year}-12-31"))
        else:
            valid_months = []
            for m in months:
                if _is_future_or_current_month(year, m, allow_current=allow_current_month):
                    logger.warning(
                        "Skipping %d-%02d: month is current (incomplete) or future "
                        "(today is %s)",
                        year, m, _date.today().isoformat(),
                    )
                else:
                    valid_months.append(m)

            if not valid_months:
                continue  # all months for this year were skipped

            first_month = valid_months[0]
            last_month = valid_months[-1]
            last_day = calendar.monthrange(year, last_month)[1]
            time_ranges.append((
                f"{year}-{first_month:02d}-01",
                f"{year}-{last_month:02d}-{last_day:02d}",
            ))

    if months is None:
        logger.info("Processing years: %s", years)
    else:
        logger.info("Processing years: %s, months: %s", years, months)

    return time_ranges


def detect_earliest_available_date(wkt: str, polarization: str = 'VV+VH') -> str | None:
    """
    Get the earliest available RTC-S1 data date for the specified ROI through the ASF search API

    Args:
        wkt: ROI string in WKT format
        polarization: Polarization mode ('VV+VH' or 'HH+HV')

    Returns:
        Earliest available data date string (YYYY-MM-DD), returns None if query fails

    Implementation steps:
    1. Call asf.search(intersectsWith=wkt, processingLevel='RTC', maxResults=1000)
    2. Sort by time ascending, take the startTime of the first record
    3. Search in stages to handle regions with late data availability
    """
    try:
        # Search for the earliest RTC-S1 data in stages
        # Start from Sentinel-1A launch date (2014-04-03) to ensure we get the earliest data
        dates = []

        # Stage 1: Search early years (2014-2018)
        for end_year in [2017, 2018, 2020, 2023]:
            try:
                resp = asf.search(
                    intersectsWith=wkt,
                    platform=[asf.PLATFORM.SENTINEL1],
                    processingLevel='RTC',
                    start='2014-01-01',
                    end=f'{end_year}-12-31',
                    maxResults=1000,
                )

                if resp:
                    # Extract dates from this batch
                    for r in resp:
                        try:
                            start_time = pd.to_datetime(r.properties['startTime'])
                            # Check if polarization matches
                            pol = '+'.join(r.properties['polarization'])
                            if polarization in [pol, None]:
                                dates.append(start_time)
                        except Exception as _e:
                            logger.debug("Skipping search result: could not parse properties: %s", _e)
                            continue

                    # If we found data, no need to search further
                    if dates:
                        break

            except Exception as e:
                warn(f"Search failed for period up to {end_year}: {e}")
                continue

        if not dates:
            warn(f"No RTC-S1 data found for WKT: {wkt[:50]}...")
            return None

        # Find the earliest date
        earliest = min(dates)
        earliest_str = earliest.strftime("%Y-%m-%d")

        logger.info("Detected earliest available data: %s", earliest_str)
        return earliest_str

    except Exception as e:
        warn(f"Failed to detect earliest available data: {e}")
        return None


def detect_earliest_available_date_from_mgrs_tiles(
    mgrs_tile_ids: list[str],
    polarization: str = 'VV+VH',
) -> str | None:
    """
    Get the earliest available RTC-S1 data date for the specified MGRS tiles
    by querying burst-level metadata from ASF.

    More precise than WKT-based search: queries exactly the bursts that overlap
    the target tiles using the local LUT, then finds the minimum acquisition date.

    Args:
        mgrs_tile_ids: List of MGRS tile IDs (e.g. ['18MUD', '18MUE'])
        polarization: Polarization mode ('VV+VH' or 'HH+HV')

    Returns:
        Earliest available data date string (YYYY-MM-DD), or None if query fails
    """
    try:
        from s1grits.mgrs_burst_data import get_burst_ids_in_mgrs_tiles
        from s1grits.asf_tiles import get_rtc_s1_ts_metadata_by_burst_ids

        burst_ids = get_burst_ids_in_mgrs_tiles(mgrs_tile_ids)
        if not burst_ids:
            warn(f"No burst IDs found for MGRS tiles: {mgrs_tile_ids}")
            return None

        df = get_rtc_s1_ts_metadata_by_burst_ids(
            burst_ids,
            start_acq_dt='2014-01-01',
            stop_acq_dt=None,
            polarizations=polarization,
        )

        if df.empty:
            warn(f"No RTC-S1 data found for MGRS tiles: {mgrs_tile_ids}")
            return None

        earliest = pd.to_datetime(df['acq_dt'].min()).strftime('%Y-%m-%d')
        logger.info("Detected earliest available data: %s", earliest)
        return earliest

    except Exception as e:
        warn(f"Failed to detect earliest available data from MGRS tiles: {e}")
        return None


def chunk_dates_by_year(time_ranges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    Split large time spans by year

    Args:
        time_ranges: [(start, end), ...] list of time ranges

    Returns:
        List of time ranges split by year

    Example:
        Input: [('2020-01-01', '2023-12-31')]
        Output: [('2020-01-01', '2020-12-31'),
                 ('2021-01-01', '2021-12-31'),
                 ('2022-01-01', '2022-12-31'),
                 ('2023-01-01', '2023-12-31')]
    """
    chunked_ranges = []

    for start_str, end_str in time_ranges:
        start_date = pd.to_datetime(start_str)
        end_date = pd.to_datetime(end_str)

        # Generate start and end dates for each year
        current_year = start_date.year
        end_year = end_date.year

        while current_year <= end_year:
            # Determine start and end dates for the current year
            if current_year == start_date.year:
                year_start = start_date.strftime("%Y-%m-%d")
            else:
                year_start = f"{current_year}-01-01"

            if current_year == end_date.year:
                year_end = end_date.strftime("%Y-%m-%d")
            else:
                year_end = f"{current_year}-12-31"

            chunked_ranges.append((year_start, year_end))
            current_year += 1

    return chunked_ranges
