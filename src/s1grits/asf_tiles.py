from datetime import datetime
from warnings import warn

import asf_search as asf
import geopandas as gpd
import pandas as pd
from pandera.pandas import check_input
from rasterio.crs import CRS
from shapely.geometry import shape

from s1grits.mgrs_burst_data import get_burst_ids_in_mgrs_tiles, get_lut_by_mgrs_tile_ids
from s1grits.tabular_models import reorder_columns, rtc_s1_resp_schema, rtc_s1_schema

SECONDS_PER_DAY: int = 86400          # Seconds in one day
S1_REPEAT_CYCLE_DAYS: int = 6        # Sentinel-1 exact repeat cycle in days


def convert_asf_url_to_cumulus(url: str) -> str:
    """
    Convert an ASF datapool URL to the equivalent Cumulus earthdatacloud URL.

    Handles both RTC time-series and RTC-STATIC product URLs.

    RTC URL format:    https://datapool.asf.alaska.edu/RTC/OPERA-S1/<filename>
    RTC-STATIC format: https://datapool.asf.alaska.edu/RTC-STATIC/OPERA-S1/<filename>
    Cumulus format:    https://cumulus.asf.earthdatacloud.nasa.gov/OPERA/OPERA_L2_RTC-S1/<granule>/<filename>

    Args:
        url: Source URL (ASF datapool RTC, ASF datapool RTC-STATIC, or Cumulus format)

    Returns:
        Cumulus URL if input is an ASF datapool URL, otherwise returns input unchanged.
    """
    cumulus_base = 'https://cumulus.asf.earthdatacloud.nasa.gov/OPERA/OPERA_L2_RTC-S1/'
    asf_rtc_base = 'https://datapool.asf.alaska.edu/RTC/OPERA-S1/'
    asf_rtc_static_base = 'https://datapool.asf.alaska.edu/RTC-STATIC/OPERA-S1/'

    # Already a Cumulus URL — return as-is
    if url.startswith(cumulus_base):
        return url

    # Determine which ASF datapool base this URL belongs to
    if url.startswith(asf_rtc_base):
        asf_base = asf_rtc_base
    elif url.startswith(asf_rtc_static_base):
        asf_base = asf_rtc_static_base
    else:
        # Not a recognised ASF URL — return unchanged (no warning for non-TIF files)
        if url.endswith('.tif'):
            warn(f'URL {url} is not a recognised ASF datapool or Cumulus URL.')
        return url

    filename = url[len(asf_base):]  # strip base, keep filename only
    # OPERA granule name is the filename stem up to (but not including) the layer suffix.
    # RTC filename:        OPERA_L2_RTC-S1_T018-038646-IW2_20230501T120000Z_..._VV.tif
    # RTC-STATIC filename: OPERA_L2_RTC-S1-STATIC_T018-038646-IW2_20140403_S1A_30_v1.0_mask.tif
    # The granule name is the product directory; we derive it by stripping the layer suffix
    # (everything after the last underscore before a known suffix, or just using the sceneName pattern).
    # Simplest approach: granule name = filename without the trailing _<layer>.tif part.
    # The OPERA granule dir has the same name as the product minus the layer suffix.
    # For RTC-STATIC: OPERA_L2_RTC-S1-STATIC_T018-038646-IW2_20140403_S1A_30_v1.0
    # For RTC:        OPERA_L2_RTC-S1_T018-038646-IW2_20230501T120000Z_20230501T120010Z_S1A_30_v1.0
    # Strip known layer suffixes to get the granule name
    _known_layer_suffixes = (
        '_VV.tif', '_VH.tif', '_HH.tif', '_HV.tif',
        '_dem.tif', '_mask.tif', '_incidence_angle.tif',
        '_local_incidence_angle.tif', '_number_of_looks.tif',
        '_rtc_anf_gamma0_to_beta0.tif', '_rtc_anf_gamma0_to_sigma0.tif',
    )
    granule_name = None
    for suffix in _known_layer_suffixes:
        if filename.endswith(suffix):
            granule_name = filename[:-len(suffix)]
            break
    if granule_name is None:
        # Fallback: strip last underscore-delimited segment (handles unknown suffixes)
        parts = filename.rsplit('_', 1)
        granule_name = parts[0] if len(parts) == 2 else filename.replace('.tif', '')

    new_url = f'{cumulus_base}{granule_name}/{filename}'
    return new_url


def format_polarization(pol: list | str) -> str:
    """
    Normalize a polarization value to a canonical string like 'VV+VH' or 'HH+HV'.

    Args:
        pol: Polarization as a list (e.g. ['VV', 'VH']) or string (e.g. 'VV+VH').

    Returns:
        Canonical polarization string.

    Raises:
        TypeError: If pol is neither a list nor a string.
    """
    if isinstance(pol, list):
        if ('VV' in pol) and len(pol) == 2:
            return 'VV+VH'
        elif ('HH' in pol) and len(pol) == 2:
            return 'HH+HV'
        else:
            return '+'.join(pol)
    elif isinstance(pol, str):
        return pol
    else:
        raise TypeError(f'Invalid polarization: {pol}.')


def extract_pass_id(acq_dt: pd.Timestamp) -> int:
    """
    Compute a pass ID as the number of 6-day repeat cycles since 2014-01-01 UTC.

    Args:
        acq_dt: Acquisition datetime (timezone-aware).

    Returns:
        Integer pass ID (0-indexed from the Sentinel-1 reference epoch).
    """
    reference_date = pd.Timestamp('2014-01-01', tz='UTC')
    return int((acq_dt - reference_date).total_seconds() / SECONDS_PER_DAY / S1_REPEAT_CYCLE_DAYS)


def append_pass_data(df_rtc: gpd.GeoDataFrame, mgrs_tile_ids: list[str]) -> gpd.GeoDataFrame:
    """Format the RTC S1 metadata for easier lookups."""
    # Extract the LUT acquisition info
    # Burst IDs will have multiple rows if they lie in multiple MGRS tiles and those tiles are specified
    rtc_columns = df_rtc.columns.tolist()
    if not all([col in rtc_columns for col in ['jpl_burst_id', 'pass_id', 'acq_dt', 'track_number']]):
        raise ValueError('Cannot append pass data without jpl_burst_id, pass_id, acq_dt, and track_number columns.')
   
    df_lut = get_lut_by_mgrs_tile_ids(mgrs_tile_ids)
    
    df_rtc = pd.merge(
        df_rtc,
        df_lut[['jpl_burst_id', 'mgrs_tile_id', 'acq_group_id_within_mgrs_tile', 'orbit_pass']],
        on='jpl_burst_id',
        how='inner',
    )
    # Creates a date string 'YYYY-MM-DD' for the earliest acquisition date for a pass of the mgrs tile
    df_rtc['acq_date_for_mgrs_pass'] = (
        df_rtc.groupby(['mgrs_tile_id', 'acq_group_id_within_mgrs_tile', 'pass_id'])['acq_dt']
        .transform('min')
        .dt.floor('D')
        .dt.strftime('%Y-%m-%d')
    )

    # Creates track_token that associates joins the track number with '_' within a pass of the mgrs tile
    def get_track_token(track_numbers: list[int]) -> str:
        unique_track_numbers = track_numbers.unique().tolist()
        return '_'.join(map(str, sorted(unique_track_numbers)))

    df_rtc['track_token'] = df_rtc.groupby(['mgrs_tile_id', 'acq_group_id_within_mgrs_tile'])['track_number'].transform(
        get_track_token
    )

    df_rtc = df_rtc.sort_values(by=['jpl_burst_id', 'acq_dt']).reset_index(drop=True)

    return df_rtc


def _chunk_list(items: list, n_chunks: int) -> list[list]:
    """Split *items* into up to *n_chunks* contiguous, roughly-equal chunks."""
    n_chunks = max(1, min(int(n_chunks), len(items)))
    if n_chunks <= 1:
        return [items]
    size = (len(items) + n_chunks - 1) // n_chunks
    return [items[i:i + size] for i in range(0, len(items), size)]


def _geo_search_bursts(
    burst_ids: list[str],
    start_acq_dt_obj,
    stop_acq_dt_obj,
    query_workers: int,
) -> list:
    """Run asf.geo_search over *burst_ids*, optionally across a thread pool.

    Splitting the burst-ID list into chunks and issuing one geo_search per
    chunk returns the same union of products as a single geo_search over the
    whole list (bursts are independent), so the concatenated response feeds
    the identical downstream dedup/sort and yields a bit-identical result set.
    Parallelism only overlaps the network round-trips.  ``query_workers <= 1``
    keeps the original single call.
    """
    if query_workers <= 1 or len(burst_ids) <= 1:
        return list(asf.geo_search(
            operaBurstID=burst_ids,
            processingLevel='RTC',
            start=start_acq_dt_obj,
            end=stop_acq_dt_obj,
        ))
    chunks = _chunk_list(burst_ids, query_workers)
    from concurrent.futures import ThreadPoolExecutor

    def _one(chunk):
        return list(asf.geo_search(
            operaBurstID=chunk,
            processingLevel='RTC',
            start=start_acq_dt_obj,
            end=stop_acq_dt_obj,
        ))

    resp: list = []
    with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
        for part in ex.map(_one, chunks):   # ex.map preserves chunk order
            resp.extend(part)
    return resp


def get_rtc_s1_ts_metadata_by_burst_ids(
    burst_ids: str | list[str],
    start_acq_dt: str | datetime | None | pd.Timestamp = None,
    stop_acq_dt: str | datetime | None | pd.Timestamp = None,
    polarizations: str | None = None,
    include_single_polarization: bool = False,
    query_workers: int = 1,
) -> gpd.GeoDataFrame:
    """Wrap/format the ASF search API for RTC-S1 metadata search. All searches go through this function.

    Requires search data to be dual polarized data of the same type (if not specified, will get all search results
    of the available type).

    If dual polarized data is mixed (that is there are HH+HV and VV+VH), will raise an error.

    ``query_workers`` > 1 splits the burst-ID list into that many chunks and
    issues the CMR searches concurrently.  The merged result is identical to the
    serial single-call result (bursts are independent; the downstream dedup and
    ``sort_values(['jpl_burst_id', 'acq_dt'])`` are deterministic).
    """
    if isinstance(burst_ids, str):
        burst_ids = [burst_ids]

    if (polarizations is not None) and (polarizations not in ['HH+HV', 'VV+VH']):
        raise ValueError(f'Invalid polarization: {polarizations}. Must be one of: HH+HV, VV+VH, None.')

    # Convert all date inputs to datetime objects using pandas for flexibility
    start_acq_dt_obj = None
    stop_acq_dt_obj = None

    if start_acq_dt is not None:
        start_acq_dt_obj = pd.to_datetime(start_acq_dt, utc=True).to_pydatetime()

    if stop_acq_dt is not None:
        stop_acq_dt_obj = pd.to_datetime(stop_acq_dt, utc=True).to_pydatetime()

    # Make sure JPL syntax is transformed to asf syntax
    burst_ids = [burst_id.upper().replace('-', '_') for burst_id in burst_ids]
    resp = _geo_search_bursts(burst_ids, start_acq_dt_obj, stop_acq_dt_obj, query_workers)
    if not resp:
        warn('No results - please check burst id and availability.', category=UserWarning)
        return gpd.GeoDataFrame(columns=rtc_s1_resp_schema.columns.keys())

    properties = [r.properties for r in resp]
    geometry = [shape(r.geojson()['geometry']) for r in resp]
    properties_f = [
        {
            'opera_id': p['sceneName'],
            'acq_dt': pd.to_datetime(p['startTime']),
            'track_number': p['pathNumber'],
            'polarizations': format_polarization(p['polarization']),
            'all_urls': [p['url']] + p['additionalUrls'],
        }
        for p in properties
    ]

    df_rtc = gpd.GeoDataFrame(properties_f, geometry=geometry, crs=CRS.from_epsg(4326))
    # Extract the burst_id from the opera_id
    df_rtc['jpl_burst_id'] = df_rtc['opera_id'].map(lambda bid: bid.split('_')[3])

    # pass_id is the integer number of 6 day periods since 2014-01-01
    df_rtc['pass_id'] = df_rtc.acq_dt.map(extract_pass_id)

    # Remove duplicates from time series
    df_rtc['dedup_id'] = df_rtc.opera_id.map(lambda id_: '_'.join(id_.split('_')[:5]))
    df_rtc = df_rtc.drop_duplicates(subset=['dedup_id']).reset_index(drop=True)
    df_rtc = df_rtc.drop(columns=['dedup_id'])

    # polarizations - ensure dual polarization
    # asf metadata can be ['HH', 'HV'] or 'HH+HV' - reformat to the latter
    df_rtc['polarizations'] = df_rtc['polarizations'].map(format_polarization)
    if polarizations is not None:
        ind_pol = df_rtc['polarizations'] == polarizations
    elif not include_single_polarization:
        ind_pol = df_rtc['polarizations'].isin(['HH+HV', 'VV+VH'])
    else:
        ind_pol = df_rtc['polarizations'].isin(['HH+HV', 'VV+VH', 'HH', 'HV', 'VV', 'VH'])
    if not ind_pol.any():
        warn(f'No valid dual polarization images found for {burst_ids}.')
    # First get all the dual-polarizations images
    df_rtc = df_rtc[ind_pol].reset_index(drop=True)

    def get_url_by_polarization(prod_urls: list[str], polarization_token: str) -> list[str]:
        if polarization_token == 'copol':
            polarizations_allowed = ['VV', 'HH']
        elif polarization_token == 'crosspol':
            polarizations_allowed = ['HV', 'VH']
        else:
            raise ValueError(f'Invalid polarization token: {polarization_token}. Must be one of: copol, crosspol.')
        possible_urls = [url for pol in polarizations_allowed for url in prod_urls if f'_{pol}.tif' == url[-7:]]
        if len(possible_urls) == 0:
            raise ValueError(f'No {polarizations_allowed} urls found')
        if len(possible_urls) > 1:
            raise ValueError(f'Multiple {polarization_token} urls found: {", ".join(possible_urls)}')
        return possible_urls[0]

    url_copol = df_rtc.all_urls.map(lambda urls_for_prod: get_url_by_polarization(urls_for_prod, 'copol'))
    url_crosspol = df_rtc.all_urls.map(lambda urls_for_prod: get_url_by_polarization(urls_for_prod, 'crosspol'))

    df_rtc['url_copol'] = url_copol
    df_rtc['url_crosspol'] = url_crosspol
    df_rtc['url_copol'] = df_rtc['url_copol'].map(convert_asf_url_to_cumulus)
    df_rtc['url_crosspol'] = df_rtc['url_crosspol'].map(convert_asf_url_to_cumulus)
    df_rtc = df_rtc.drop(columns=['all_urls'])

    # Ensure the data is sorted by jpl_burst_id and acq_dt
    df_rtc = df_rtc.sort_values(by=['jpl_burst_id', 'acq_dt'], ascending=True).reset_index(drop=True)

    rtc_s1_resp_schema.validate(df_rtc)
    df_rtc = reorder_columns(df_rtc, rtc_s1_resp_schema)

    return df_rtc


# =============================================================================
# Static Layer URL Derivation and RTC-STATIC Product Query
# =============================================================================

STATIC_LAYER_SUFFIXES: dict[str, str] = {
    'dem':     '_dem.tif',
    'inc_map': '_inc_map.tif',
    'ls_map':  '_ls_map.tif',
}

# File suffix patterns used to identify static layer files within an RTC-STATIC product.
# RTC-STATIC products do NOT contain a DEM file; dem is excluded.
# IMPORTANT: '_local_incidence_angle.tif' must be listed BEFORE '_incidence_angle.tif'
# because the shorter suffix is a substring of the longer one — endswith() would match
# both, and we want the more specific (local) variant to take priority.
# Layover/shadow mask uses '_mask.tif'.
_STATIC_LAYER_FILE_PATTERNS: dict[str, tuple[str, ...]] = {
    'local_inc_angle': ('_local_incidence_angle.tif',),
    'inc_angle':       ('_incidence_angle.tif',),
    'ls_map':          ('_mask.tif',),
    'number_of_looks': ('_number_of_looks.tif',),
    'rtc_anf_beta0':   ('_rtc_anf_gamma0_to_beta0.tif',),
    'rtc_anf_sigma0':  ('_rtc_anf_gamma0_to_sigma0.tif',),
}


def _extract_static_layer_urls(all_urls: list[str]) -> dict[str, str | None]:
    """
    Extract static layer URLs from an RTC-STATIC product's URL list.

    Scans the list of URLs for each known static layer file pattern and
    returns the first match per layer. Returns None for any layer not found.

    Args:
        all_urls: List of all URLs for one RTC-STATIC granule (url + additionalUrls).

    Returns:
        dict mapping layer name to URL or None, e.g.
        {'dem': 'https://...dem.tif', 'inc_map': None, 'ls_map': 'https://...mask.tif'}
    """
    result: dict[str, str | None] = {}
    for layer, patterns in _STATIC_LAYER_FILE_PATTERNS.items():
        matched = None
        for url in all_urls:
            for pat in patterns:
                if url.lower().endswith(pat):
                    matched = url
                    break
            if matched:
                break
        result[layer] = matched
    return result


def query_rtc_static_metadata_by_burst_ids(
    burst_ids: list[str],
    batch_size: int = 50,
    max_results: int = 5000,
    max_query_retries: int = 5,
    query_retry_base_delay: float = 2.0,
) -> 'pd.DataFrame':
    """
    Query ASF for OPERA RTC-STATIC products by burst IDs.

    RTC-STATIC products are not time-series; they are static ancillary layers
    (DEM, incidence angle map, layover/shadow mask) associated with each burst.
    They must be queried directly with processingLevel='RTC-STATIC' — they are
    NOT accessible by deriving URLs from RTC time-series products.

    Args:
        burst_ids: List of JPL/ASF burst IDs (e.g. ['T018_038645_IW2', ...]).
            Both underscore and hyphen formats are accepted and normalised.
        batch_size: Number of burst IDs per ASF query (default 50).
        max_results: Maximum results per batch query.
        max_query_retries: Max retries per batch on transient failures (default 5).
        query_retry_base_delay: Base delay in seconds before first retry,
            doubled each attempt with jitter (default 2.0).

    Returns:
        pd.DataFrame with columns:
            jpl_burst_id, opera_id,
            url_local_inc_angle, url_inc_angle, url_ls_map,
            url_number_of_looks, url_rtc_anf_beta0, url_rtc_anf_sigma0,
            flight_direction, path_number
        One row per RTC-STATIC granule. Rows missing all layer URLs are dropped.
    """
    import time as _time
    import random as _random
    import logging

    # Normalise burst IDs to ASF format (uppercase, underscores)
    normalised = [bid.upper().replace('-', '_') for bid in burst_ids]

    all_records: list[dict] = []

    for i in range(0, len(normalised), batch_size):
        batch = normalised[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(normalised) + batch_size - 1) // batch_size
        logging.debug("[Static] Querying RTC-STATIC batch %d/%d: %d burst IDs",
                      batch_num, total_batches, len(batch))

        results = None
        last_error = None
        for attempt in range(1, max_query_retries + 1):
            try:
                results = asf.search(
                    operaBurstID=batch,
                    processingLevel='RTC-STATIC',
                    maxResults=max_results,
                )
                break  # success
            except Exception as e:
                last_error = e
                if attempt < max_query_retries:
                    delay = query_retry_base_delay * (2 ** (attempt - 1))
                    jitter = _random.uniform(0, delay * 0.5)
                    wait = delay + jitter
                    logging.warning(
                        "[Static] RTC-STATIC batch %d/%d attempt %d/%d failed: %s. "
                        "Retrying in %.1fs...",
                        batch_num, total_batches, attempt, max_query_retries,
                        e, wait,
                    )
                    _time.sleep(wait)
                else:
                    logging.warning(
                        "[Static] RTC-STATIC batch %d/%d failed after %d attempts: %s",
                        batch_num, total_batches, max_query_retries, e,
                    )

        if results is None:
            results = []

        for r in results:
            p = r.properties
            # Use ASF datapool URLs directly — RTC-STATIC products are NOT on Cumulus.
            # Do NOT convert to Cumulus format; the ASF datapool URLs are the correct endpoints.
            all_urls: list[str] = ([p.get('url', '')] + list(p.get('additionalUrls') or []))
            all_urls = [u for u in all_urls if u]

            layer_urls = _extract_static_layer_urls(all_urls)

            # Skip granules with no recognised static files at all
            if not any(layer_urls.values()):
                logging.debug("[Static] Skipping granule with no static layer URLs: %s",
                              p.get('sceneName', ''))
                continue

            all_records.append({
                'jpl_burst_id':        p.get('operaBurstID', '').upper().replace('-', '_'),
                'opera_id':            p.get('sceneName', ''),
                'acq_dt':              p.get('startTime'),
                'url_local_inc_angle': layer_urls.get('local_inc_angle'),
                'url_inc_angle':       layer_urls.get('inc_angle'),
                'url_ls_map':          layer_urls.get('ls_map'),
                'url_number_of_looks': layer_urls.get('number_of_looks'),
                'url_rtc_anf_beta0':   layer_urls.get('rtc_anf_beta0'),
                'url_rtc_anf_sigma0':  layer_urls.get('rtc_anf_sigma0'),
                'flight_direction':    p.get('flightDirection', ''),
                'path_number':         p.get('pathNumber'),
            })

        _time.sleep(0.2)

    if not all_records:
        logging.warning(
            "[Static] No RTC-STATIC products found for %d burst IDs. "
            "Ensure asf_search >= 6.7.3 and that RTC-STATIC data exists for this region.",
            len(burst_ids),
        )
        import pandas as _pd
        return _pd.DataFrame(columns=[
            'jpl_burst_id', 'opera_id', 'acq_dt',
            'url_dem', 'url_inc_map', 'url_ls_map',
            'flight_direction', 'path_number',
        ])

    import pandas as _pd
    df = _pd.DataFrame(all_records)
    # Burst sensing time (UTC) — used to set the static item's temporal range.
    df['acq_dt'] = _pd.to_datetime(df['acq_dt'], errors='coerce', utc=True)
    # One RTC-STATIC granule per burst — keep unique burst IDs (latest opera_id if duplicates)
    df = df.sort_values('opera_id').drop_duplicates(subset=['jpl_burst_id'], keep='last')
    df = df.reset_index(drop=True)
    logging.info("[Static] Found %d RTC-STATIC granules for %d burst IDs",
                 len(df), len(burst_ids))
    return df


def derive_static_urls(url_copol: str) -> dict[str, str]:
    """
    Derive static layer URLs from a copol (VV/HH) URL by suffix substitution.

    .. deprecated::
        OPERA RTC-S1 time-series products do NOT include static ancillary files
        (_dem.tif, _inc_map.tif, _ls_map.tif). These files only exist in
        dedicated RTC-STATIC products. Use query_rtc_static_metadata_by_burst_ids()
        instead. This function is retained for backward compatibility only.

    Args:
        url_copol: Cumulus URL ending in '_VV.tif' or '_HH.tif'.

    Returns:
        dict mapping layer name to derived URL (may return 404 if files do not exist).

    Raises:
        ValueError: If url_copol does not end with a recognised copol suffix.
    """
    for copol_suffix in ('_VV.tif', '_HH.tif'):
        if url_copol.endswith(copol_suffix):
            base = url_copol[:-len(copol_suffix)]
            return {layer: base + suffix for layer, suffix in STATIC_LAYER_SUFFIXES.items()}
    raise ValueError(f"Cannot derive static URLs from copol URL: {url_copol!r}")


def get_rtc_s1_metadata_from_acq_group(
    mgrs_tile_ids: list[str],
    track_numbers: list[int],
    n_images_per_burst: int = 1,
    start_acq_dt: datetime | str | None = None,
    stop_acq_dt: datetime | str | None = None,
    max_variation_seconds: float | None = None,
    polarizations: str | None = None,
) -> gpd.GeoDataFrame:
    """
    Meant for acquiring a pre-image or post-image set from MGRS tiles for a given S1 pass.

    Obtains the most recent burst image set within a date range.

    For acquiring a post-image set, we provide the keyword argument max_variation_seconds to ensure the latest
    acquisition of are within the latest acquisition time from the most recent burst image. If this is not provided,
    you will get the latest burst image product for each burst within the allowable date range. This could yield imagery
    collected on different dates for the burst_ids provided.

    For acquiring a pre-image set, we use n_images_per_burst > 1. We get the latest n_images_per_burst images for each
    burst and there can be different number of images per burst for all the burst supplied and/or the image
    time series can be composed of images from different dates.

    Note we take care of the equator edge cases in LUT of the MGRS/burst_ids, so only need to provide 1 valid track
    number in pass.

    Parameters
    ----------
    mgrs_tile_ids: list[str]
    track_numbers: list[int]
    start_acq_dt: datetime | str
    stop_acq_dt : datetime
    max_variation_seconds : float, optional
    n_images_per_burst : int, optional

    Returns
    -------
    gpd.GeoDataFrame
    """
    if len(track_numbers) > 2:
        raise ValueError('Cannot handle more than 2 track numbers.')
    if (len(track_numbers) == 2) and (abs(track_numbers[0] - track_numbers[1]) > 1):
        raise ValueError('Two track numbers that are not consecutive were provided.')
    burst_ids = get_burst_ids_in_mgrs_tiles(mgrs_tile_ids, track_numbers=track_numbers)
    if not burst_ids:
        mgrs_tiles_str = ','.join(mgrs_tile_ids)
        track_numbers_str = ','.join(map(str, track_numbers))
        raise ValueError(
            f'No burst ids found for the provided MGRS tile {mgrs_tiles_str} and track numbers {track_numbers_str}.'
        )

    if (n_images_per_burst == 1) and (max_variation_seconds is None):
        warn(
            'No maximum variation in acq dts provided although n_images_per_burst is 1. '
            'This could yield imagery collected on '
            'different dates for the burst_ids provided.',
            category=UserWarning,
        )
    df_rtc = get_rtc_s1_ts_metadata_by_burst_ids(
        burst_ids,
        start_acq_dt=start_acq_dt,
        stop_acq_dt=stop_acq_dt,
        polarizations=polarizations,
    )
    # Assumes that each group is ordered by date (earliest first and most recent last)
    columns = df_rtc.columns
    df_rtc = df_rtc.groupby('jpl_burst_id').tail(n_images_per_burst).reset_index(drop=False)
    df_rtc = df_rtc[columns]
    if max_variation_seconds is not None:
        if (n_images_per_burst is None) or (n_images_per_burst > 1):
            raise ValueError('Cannot apply maximum variation in acq dts when n_images_per_burst > 1 or None.')
        max_dt = df_rtc['acq_dt'].max()
        ind = df_rtc['acq_dt'] > max_dt - pd.Timedelta(seconds=max_variation_seconds)
        df_rtc = df_rtc[ind].reset_index(drop=True)

    if not df_rtc.empty:
        df_rtc = append_pass_data(df_rtc, mgrs_tile_ids)
        rtc_s1_schema.validate(df_rtc)
    df_rtc = reorder_columns(df_rtc, rtc_s1_schema)

    return df_rtc


def get_rtc_s1_ts_metadata_from_mgrs_tiles(
    mgrs_tile_ids: list[str],
    track_numbers: list[int] | None = None,
    start_acq_dt: str | datetime | None = None,
    stop_acq_dt: str | datetime | None = None,
    polarizations: str | None = None,
    query_workers: int = 1,
) -> gpd.GeoDataFrame:
    """Get the RTC S1 time series for a given MGRS tile and track number.

    ``query_workers`` > 1 parallelizes the CMR burst searches (identical
    result set; see get_rtc_s1_ts_metadata_by_burst_ids).
    """
    if isinstance(start_acq_dt, str):
        start_acq_dt = datetime.strptime(start_acq_dt, '%Y-%m-%d')
    if isinstance(stop_acq_dt, str):
        stop_acq_dt = datetime.strptime(stop_acq_dt, '%Y-%m-%d')

    burst_ids = get_burst_ids_in_mgrs_tiles(mgrs_tile_ids, track_numbers=track_numbers)
    df_rtc_ts = get_rtc_s1_ts_metadata_by_burst_ids(
        burst_ids, start_acq_dt=start_acq_dt, stop_acq_dt=stop_acq_dt,
        polarizations=polarizations, query_workers=query_workers,
    )
    if df_rtc_ts.empty:
        mgrs_tiles_str = ','.join(mgrs_tile_ids)
        msg = f'No RTC S1 metadata found for  MGRS tile {mgrs_tiles_str}.'
        if track_numbers is not None:
            track_number_token = '_'.join(map(str, track_numbers))
            msg += f' Track numbers provided: {track_number_token}.'
        warn(msg)
        return gpd.GeoDataFrame(columns=rtc_s1_schema.columns.keys())

    df_rtc_ts = append_pass_data(df_rtc_ts, mgrs_tile_ids)
    rtc_s1_schema.validate(df_rtc_ts)
    df_rtc_ts = reorder_columns(df_rtc_ts, rtc_s1_schema)

    return df_rtc_ts


@check_input(rtc_s1_schema, 0)
def agg_rtc_metadata_by_burst_id(df_rtc_ts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    df_agg = (
        df_rtc_ts.groupby('jpl_burst_id')
        .agg(count=('jpl_burst_id', 'size'), earliest_acq_date=('acq_dt', 'min'), latest_acq_date=('acq_dt', 'max'))
        .reset_index()
    )

    return df_agg