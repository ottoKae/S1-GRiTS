# ==============================================================================
#  asf_io.py
#  Download & Session Management for S1-GRiTS.
#
#  Refactored from original asf_io.py for Cython compatibility.
#  Changes vs. original:
#    - Removed threading.local; each call to _get_session() creates a fresh
#      per-call Session so parallel threads do not share connection state
#    - Extracted _download_with_retry to top-level (no closure / nonlocal)
#    - despeckle_2d imported from asf_array_processing (not defined here)
#    - All function-body imports moved to module top-level
#    - 404 retry counter uses module-level dict (not function-object attribute)
# ==============================================================================

import concurrent.futures
import gc
import io
import logging
import random
import re
import threading
import time

import numpy as np
import pandas as pd
import requests
import rasterio

from tqdm.auto import tqdm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rasterio.io import MemoryFile

from s1grits.asf_array_processing import despeckle_2d
from s1grits.runtime_limits import rasterio_env_kwargs

# =========================================================
#  Module-Level Constants
# =========================================================
NODATA_SENTINEL: float = -9999.0          # Standard nodata fill value for all rasters
DB_SCALE_FACTOR: float = 10.0             # Multiplier in dB conversion (10 * log10)
METERS_PER_DEGREE_LAT: float = 111320.0  # Approximate metres per degree of latitude
UTM_NORTH_BASE: int = 32600              # EPSG base for UTM North zones (32600 + zone)
UTM_SOUTH_BASE: int = 32700              # EPSG base for UTM South zones (32700 + zone)
MAX_BACKOFF_EXPONENT: int = 10           # Cap on exponential backoff: 2**10 = 1024 s
DEFAULT_RETRY_TIMEOUT_S: float = 600.0  # Total retry budget per download (seconds)
DEFAULT_CHUNK_SIZE: int = 1024           # Default zarr chunk size (pixels)
COG_BLOCK_SIZE: int = 256               # COG internal tile block size (pixels)
DEFAULT_RES_M: float = 30.0             # Default output resolution (metres)
PREVIEW_RES_M: float = 300.0            # Default preview resolution (metres)
DOWNLOAD_CHUNK_BYTES: int = 1024 * 1024  # HTTP download streaming chunk size (1 MiB)

# Per-URL 404 retry counter (module-level; avoids function-attribute state that
# Cython cannot optimise and that is not thread-safe).
_NOT_FOUND_RETRIES: dict = {}

# Thread-local storage for per-thread Session reuse.
# Each worker thread creates its own Session on first use and reuses it for all
# subsequent downloads, avoiding the overhead of constructing a new Session +
# HTTPAdapter + Retry object on every call.
_thread_local: threading.local = threading.local()

# Process-wide persistent download pool. Sessions live in thread-local storage,
# so a pool that is torn down between batches discards every warmed session and
# re-pays the TLS handshake + Earthdata URS auth redirect chain on the next
# batch — a fixed ramp that dominates small (monthly) batches. Keeping ONE pool
# alive for the process lifetime lets the per-thread sessions (and their
# keep-alive connections) survive across batches. The pool is idle between
# batches; it is resized (drain + recreate) only when a different worker count
# is requested, which never happens mid-download.
_download_executor: concurrent.futures.ThreadPoolExecutor | None = None
_download_executor_workers: int = 0
_download_executor_lock = threading.Lock()


def _get_download_executor(max_workers: int) -> concurrent.futures.ThreadPoolExecutor:
    """Return the persistent download pool, (re)creating it on size change.

    Thread-safe. The previous pool (if any) is drained with ``wait=True``
    before replacement; callers only request a resize between batches, when
    the pool is idle, so the drain is instantaneous in practice.
    """
    global _download_executor, _download_executor_workers
    max_workers = max(1, int(max_workers))
    with _download_executor_lock:
        if (_download_executor is None
                or _download_executor_workers != max_workers):
            if _download_executor is not None:
                _download_executor.shutdown(wait=True)
            _download_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="s1grits-dl"
            )
            _download_executor_workers = max_workers
        return _download_executor

# =========================================================
#  Part 0: MGRS to UTM Conversion Helper
# =========================================================

def _mgrs_to_utm_epsg(mgrs_tile_id: str) -> str:
    """
    Extract UTM EPSG code from MGRS tile ID.

    MGRS tiles are based on UTM zones. Each MGRS tile ID encodes the UTM zone
    and hemisphere information in its first characters.

    Args:
        mgrs_tile_id: MGRS tile identifier (e.g., '17MPV', '50RMQ', '17KLV')

    Returns:
        EPSG code string for the corresponding UTM zone (e.g., 'EPSG:32617')
    """
    match = re.match(r'^(\d{1,2})([C-HJ-NP-X])', mgrs_tile_id.upper())
    if not match:
        raise ValueError(f"Invalid MGRS tile ID format: {mgrs_tile_id}")

    zone_num = int(match.group(1))
    lat_band = match.group(2)

    if not (1 <= zone_num <= 60):
        raise ValueError(f"Invalid UTM zone number: {zone_num} (must be 1-60)")

    # Latitude bands C-M: Southern hemisphere (EPSG:327xx)
    # Latitude bands N-X: Northern hemisphere (EPSG:326xx)
    if lat_band < 'N':
        epsg_code = UTM_SOUTH_BASE + zone_num
    else:
        epsg_code = UTM_NORTH_BASE + zone_num

    return f"EPSG:{epsg_code}"


# =========================================================
#  Polarization Band Naming Helper
# =========================================================

def get_band_names(polarization: str) -> tuple:
    """
    Get band names based on polarization mode.

    Args:
        polarization: Polarization mode ("VV+VH" or "HH+HV")

    Returns:
        Tuple of (copol_name, crosspol_name, ratio_name, rvi_name)
    """
    if polarization == "VV+VH":
        return "VV_dB", "VH_dB", "Ratio", "RVI"
    elif polarization == "HH+HV":
        return "HH_dB", "HV_dB", "Ratio", "RVI"
    else:
        raise ValueError(
            f"Unsupported polarization: {polarization}. "
            f"Must be one of: 'VV+VH', 'HH+HV'"
        )


# =========================================================
#  Part 1: Network & Download Helpers
# =========================================================

def _get_session(retries=5, backoff_factor=1, pool_maxsize=8) -> requests.Session:
    """
    Return a requests Session for the calling thread, creating one on first use.

    Sessions are stored in thread-local storage so each worker thread reuses the
    same Session (and its underlying connection pool) across all downloads, rather
    than paying the construction cost of a new Session + HTTPAdapter + Retry object
    on every call.  Threads never share a Session, so there is no connection-state
    race between them.

    Note: pool_maxsize, backoff_factor, and retries are only applied when the
    Session is first created for a thread.  Subsequent calls from the same thread
    return the cached Session unchanged.
    """
    s = getattr(_thread_local, 'session', None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=retries,
            read=retries,
            connect=retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return s


def _download_to_bytes(
    url: str,
    timeout: tuple = (30, 120),
    retry_timeout_seconds: float = 600.0,
    max_backoff: float = 60.0,
) -> bytes:
    """
    Download file to bytes, retrying on transient errors until
    retry_timeout_seconds have elapsed (default 10 minutes).

    404/410 raises FileNotFoundError immediately (not retryable).
    All other errors are retried with exponential back-off + jitter.
    """
    fname = url.split("/")[-1]
    deadline = time.monotonic() + retry_timeout_seconds
    attempt = 0

    # Opt-in cross-tile burst cache: return cached bytes without any HTTP when
    # this URL was already downloaded (this run or a prior one). Inert unless
    # burst_cache.configure() was called, so the default path is unchanged.
    from s1grits import burst_cache
    if burst_cache.is_enabled():
        _cached = burst_cache.get(url)
        if _cached is not None:
            return _cached

    while True:
        attempt += 1
        try:
            s = _get_session()
            with s.get(url, stream=True, timeout=timeout) as resp:
                if resp.status_code in (404, 410):
                    raise FileNotFoundError(
                        f"Source file not found (HTTP {resp.status_code}): {fname}"
                    )
                resp.raise_for_status()
                buf = io.BytesIO()
                for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                    if chunk:
                        buf.write(chunk)
                _data = buf.getvalue()
                if burst_cache.is_enabled():
                    burst_cache.put(url, _data)
                return _data

        except FileNotFoundError:
            # Retry up to 2 times with 30 s gap for transient ASF re-archiving.
            # Counter stored in module-level dict (not on function object).
            _nf_count = _NOT_FOUND_RETRIES.get(url, 0)
            if _nf_count < 2:
                _NOT_FOUND_RETRIES[url] = _nf_count + 1
                remaining = deadline - time.monotonic()
                _wait = min(30.0, remaining)
                if _wait > 0:
                    logging.warning(
                        "[%s] 404 received (attempt %d/2). "
                        "Retrying in %.0fs (may be transient ASF re-archiving)...",
                        fname, _nf_count + 1, _wait,
                    )
                    time.sleep(_wait)
                    continue
            raise

        except Exception as e:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Download of {fname} failed after {attempt} attempts "
                    f"({retry_timeout_seconds:.0f}s budget exhausted). Last error: {e}"
                ) from e

            wait = min(max_backoff, (2 ** min(attempt, MAX_BACKOFF_EXPONENT)) + random.uniform(0, 2))
            wait = min(wait, remaining)

            logging.warning(
                "[%s] attempt %d failed: %s: %s. "
                "Retrying in %.1fs (budget remaining: %.0fs)...",
                fname, attempt, type(e).__name__, e, wait, remaining,
            )
            time.sleep(wait)


def read_one_asf(url: str, retry_timeout_seconds: float = 600.0):
    """
    Download and read a single GeoTIFF from ASF.

    Returns:
        (array, profile, None)            on success
        (None,  None,    'not_found')     on HTTP 404/410
        (None,  None,    'network_error') if retry budget exhausted
    """
    fname = url.split("/")[-1]
    try:
        # Ensures the bytes are on disk when the burst cache is enabled (put),
        # so the lazy path below can window-read the cached GeoTIFF directly.
        data = _download_to_bytes(url, retry_timeout_seconds=retry_timeout_seconds)
        with rasterio.Env(**rasterio_env_kwargs()):
            with MemoryFile(data, filename=fname) as memfile:
                with memfile.open() as ds:
                    prof = ds.profile
                    # Phase 3.2 (opt-in, memory.windowed_burst_reads): when the
                    # burst is on disk in the cache, hand back a lazy window
                    # reader instead of decoding the whole band — no full-array
                    # transient, no .npy spill copy, block reads fault in only
                    # the rows they touch. Byte-identical values.
                    from s1grits import lazy_burst
                    _lazy = lazy_burst.maybe_lazy(url, prof)
                    if _lazy is not None:
                        return _lazy, prof, None
                    arr = ds.read(1).astype(np.float32)
        # Phase 3 (opt-in, memory.batch_spill): hand back a read-only
        # file-backed memmap instead of anonymous RAM — byte-identical
        # values, reclaimable pages, windowed reads for the block path.
        from s1grits import batch_spill
        arr = batch_spill.maybe_spill(arr)
        return arr, prof, None
    except FileNotFoundError as e:
        logging.warning("NOT FOUND [%s]: %s", fname, e)
        return None, None, "not_found"
    except Exception as e:
        logging.error("DOWNLOAD FAILED [%s]: %s", fname, e)
        return None, None, "network_error"


def read_asf_rtc_image_data(urls: list, max_workers: int = 2, retry_timeout_seconds: float = 600.0):
    """
    Download and read GeoTIFFs in parallel, preserving strict order of input URLs.

    Returns:
        arrs: list of arrays (None on failure)
        profs: list of profiles (None on failure)
        error_types: list per scene: None=success, 'not_found', 'network_error'
    """
    N = len(urls)
    results = [(None, None, None)] * N

    # Persistent pool: threads (and their warmed thread-local sessions)
    # survive across batches instead of re-paying TLS + URS auth per batch.
    ex = _get_download_executor(max_workers)
    fut2idx = {ex.submit(read_one_asf, url, retry_timeout_seconds): i for i, url in enumerate(urls)}
    for fut in tqdm(concurrent.futures.as_completed(fut2idx), total=N, desc="Downloading"):
        i = fut2idx[fut]
        try:
            results[i] = fut.result()
        except Exception:
            results[i] = (None, None, "network_error")

    arrs = [r[0] for r in results]
    profs = [r[1] for r in results]
    error_types = [r[2] for r in results]

    success_count = sum(1 for a in arrs if a is not None)
    not_found_count = sum(1 for e in error_types if e == "not_found")
    network_fail_count = sum(1 for e in error_types if e == "network_error")
    logging.info(
        "Download complete: %d/%d success, %d not_found (404), %d network errors",
        success_count, N, not_found_count, network_fail_count,
    )
    return arrs, profs, error_types


def _download_with_retry(
    urls: list,
    label: str,
    dates: list,
    max_workers: int,
    retry_timeout_seconds: float,
    scene_max_retries: int,
) -> tuple:
    """
    Download a list of URLs with per-scene retry for network errors.

    Extracted from load_and_despeckle_rtc_strict for Cython compatibility
    (no closure / nonlocal needed).

    Args:
        urls: List of URLs to download.
        label: Human-readable label for log messages (e.g. 'copol').
        dates: Acquisition datetime list (same length as urls) for log messages.
        max_workers: Parallel download workers.
        retry_timeout_seconds: Per-scene retry budget in seconds.
        scene_max_retries: Maximum number of per-scene retry rounds.

    Returns:
        (arrs, profs, error_types) tuples as returned by read_asf_rtc_image_data.
    """
    arrs, profs, error_types = read_asf_rtc_image_data(
        urls, max_workers=max_workers, retry_timeout_seconds=retry_timeout_seconds
    )

    retry_indices = [i for i, e in enumerate(error_types) if e == "network_error"]

    for retry_round in range(1, scene_max_retries):
        if not retry_indices:
            break
        logging.info(
            "[%s] Scene-level retry round %d/%d: %d scenes",
            label, retry_round, scene_max_retries - 1, len(retry_indices),
        )
        time.sleep(2 ** retry_round)  # backoff: 2, 4, 8 ...
        still_failing = []
        for i in retry_indices:
            arr, prof, err = read_one_asf(urls[i], retry_timeout_seconds=retry_timeout_seconds)
            if err is None:
                arrs[i], profs[i], error_types[i] = arr, prof, None
                logging.info(
                    "[%s] Scene %d (%s) recovered on retry %d",
                    label, i, dates[i], retry_round,
                )
            elif err == "not_found":
                error_types[i] = "not_found"
                logging.warning(
                    "[%s] Scene %d (%s) is 404 on retry, skipping",
                    label, i, dates[i],
                )
            else:
                still_failing.append(i)
        retry_indices = still_failing

    return arrs, profs, error_types


def _run_paired_download_jobs(
    jobs: list,
    max_workers: int,
    retry_timeout_seconds: float,
    desc: str,
    raw_by_label: dict,
    prof_by_label: dict,
    err_by_label: dict,
) -> None:
    """Run mixed copol/crosspol download jobs in a single shared pool."""
    if not jobs:
        return

    # Persistent pool: threads (and their warmed thread-local sessions)
    # survive across batches instead of re-paying TLS + URS auth per batch.
    ex = _get_download_executor(max_workers)
    fut2job = {
        ex.submit(read_one_asf, url, retry_timeout_seconds=retry_timeout_seconds): (
            label,
            i,
            url,
            dt,
        )
        for label, i, url, dt in jobs
    }
    for fut in tqdm(
        concurrent.futures.as_completed(fut2job),
        total=len(jobs),
        desc=desc,
    ):
        label, i, _url, dt = fut2job[fut]
        try:
            arr, prof, err = fut.result()
        except Exception as exc:
            logging.error(
                "[%s] Download job failed for scene %d (%s): %s",
                label,
                i,
                dt,
                exc,
            )
            arr, prof, err = None, None, "network_error"

        raw_by_label[label][i] = arr
        prof_by_label[label][i] = prof
        err_by_label[label][i] = err


def _download_paired_with_retry(
    urls_copol: list,
    urls_crosspol: list,
    dates: list,
    max_workers: int,
    retry_timeout_seconds: float,
    scene_max_retries: int,
) -> tuple:
    """
    Download VV/VH pairs through one shared pool.

    The total number of active download tasks is bounded by ``max_workers``
    across both polarizations. This avoids the previous 2 * max_workers fan-out
    while still allowing VV and VH jobs to progress together.
    """
    if not (len(urls_copol) == len(urls_crosspol) == len(dates)):
        raise ValueError("copol, crosspol, and dates must have the same length")

    labels = ("copol", "crosspol")
    urls_by_label = {
        "copol": urls_copol,
        "crosspol": urls_crosspol,
    }
    n_items = len(dates)
    raw_by_label = {label: [None] * n_items for label in labels}
    prof_by_label = {label: [None] * n_items for label in labels}
    err_by_label = {label: [None] * n_items for label in labels}

    jobs = [
        (label, i, urls_by_label[label][i], dates[i])
        for label in labels
        for i in range(n_items)
    ]
    logging.info(
        "Downloading copol and crosspol in shared pool "
        "(%d total download workers, %d files)...",
        max_workers,
        len(jobs),
    )
    _run_paired_download_jobs(
        jobs,
        max_workers,
        retry_timeout_seconds,
        "Downloading VV/VH",
        raw_by_label,
        prof_by_label,
        err_by_label,
    )

    for retry_round in range(1, scene_max_retries):
        retry_jobs = [
            (label, i, urls_by_label[label][i], dates[i])
            for label in labels
            for i in range(n_items)
            if err_by_label[label][i] == "network_error"
        ]
        if not retry_jobs:
            break

        logging.info(
            "[paired] Scene-level retry round %d/%d: %d files",
            retry_round,
            scene_max_retries - 1,
            len(retry_jobs),
        )
        time.sleep(2 ** retry_round)
        _run_paired_download_jobs(
            retry_jobs,
            max_workers,
            retry_timeout_seconds,
            f"Retrying VV/VH {retry_round}",
            raw_by_label,
            prof_by_label,
            err_by_label,
        )

    for label in labels:
        arrs = raw_by_label[label]
        error_types = err_by_label[label]
        success_count = sum(1 for a in arrs if a is not None)
        not_found_count = sum(1 for e in error_types if e == "not_found")
        network_fail_count = sum(1 for e in error_types if e == "network_error")
        logging.info(
            "%s shared download complete: %d/%d success, %d not_found (404), "
            "%d network errors",
            label,
            success_count,
            n_items,
            not_found_count,
            network_fail_count,
        )

    return (
        raw_by_label["copol"],
        prof_by_label["copol"],
        err_by_label["copol"],
        raw_by_label["crosspol"],
        prof_by_label["crosspol"],
        err_by_label["crosspol"],
    )


def load_rtc_band_strict(
    df: pd.DataFrame,
    band: str = "copol",
    max_workers: int = 2,
    do_despeckle: bool = False,
    despeckle_method: str = "tv_bregman",
    despeckle_kwargs: dict = None,
    scene_max_retries: int = 3,
    max_failed_ratio: float = 0.0,
    retry_timeout_seconds: float = 600.0,
):
    """
    Strict downloader for a single RTC polarization band.

    Returns:
        (final_arr, valid_prof, valid_dates, valid_source_indices)

    ``valid_source_indices`` are integer positions in the input dataframe. They
    let callers run a cheap copol-only QC pass, then request crosspol only for
    the rows that survived QC while keeping arrays and metadata aligned.
    """
    logger = logging.getLogger(__name__)

    band_norm = str(band or "copol").lower()
    if band_norm in {"copol", "vv", "hh"}:
        url_col = "url_copol"
        label = "copol"
    elif band_norm in {"crosspol", "vh", "hv"}:
        url_col = "url_crosspol"
        label = "crosspol"
    else:
        raise ValueError("band must be 'copol' or 'crosspol'")

    urls = df[url_col].tolist()
    dates = df["acq_datetime"].tolist()
    n_items = len(dates)

    logger.debug("[load_rtc_band_strict] Input: %d %s scenes", n_items, label)
    logging.info("Downloading %s only...", label)
    raw_arr, prof_arr, err_arr = _download_with_retry(
        urls, label, dates, max_workers, retry_timeout_seconds, scene_max_retries
    )

    valid_arr, valid_prof, valid_dates, valid_source_indices = [], [], [], []
    not_found_indices = []
    network_fail_indices = []

    for i in range(n_items):
        arr_ok = raw_arr[i] is not None
        err = err_arr[i]
        if arr_ok:
            valid_arr.append(raw_arr[i])
            valid_prof.append(prof_arr[i])
            valid_dates.append(dates[i])
            valid_source_indices.append(i)
        elif err == "not_found":
            not_found_indices.append(i)
            logger.info(
                "[Strict/%s] Scene %d (%s) skipped: 404 not_found",
                label, i, dates[i],
            )
        else:
            network_fail_indices.append(i)
            logger.warning(
                "[Strict/%s] Scene %d (%s) FAILED after retries: err=%s",
                label, i, dates[i], err,
            )

    logging.info(
        "%s summary: %d valid, %d skipped (404), %d network failures / %d total",
        label, len(valid_dates), len(not_found_indices),
        len(network_fail_indices), n_items,
    )

    if not valid_dates:
        raise RuntimeError(
            f"No valid {label} scenes found - "
            f"{len(network_fail_indices)} network failures, "
            f"{len(not_found_indices)} not_found (404) out of {n_items} scenes."
        )

    downloadable = n_items - len(not_found_indices)
    if downloadable > 0 and max_failed_ratio >= 0.0:
        actual_ratio = len(network_fail_indices) / downloadable
        if actual_ratio > max_failed_ratio:
            failed_summary = ", ".join(
                f"{i}({dates[i]})" for i in network_fail_indices
            )
            raise RuntimeError(
                f"{label} network failure ratio {actual_ratio:.1%} exceeds "
                f"max_failed_ratio {max_failed_ratio:.1%} "
                f"({len(network_fail_indices)}/{downloadable} downloadable "
                f"scenes failed). Failed scenes: {failed_summary}. "
                f"Consider reducing max_download_workers or increasing "
                f"scene_max_retries."
            )

    if do_despeckle:
        logging.info("Applying %s despeckle to %s...", despeckle_method, label)
        dkw = despeckle_kwargs or {}
        method_key = 'tv_kwargs' if despeckle_method == 'tv_bregman' else 'nlm_kwargs'
        final_arr = [
            despeckle_2d(a, method=despeckle_method, **{method_key: dkw})
            for a in valid_arr
        ]
    else:
        final_arr = [a.astype(np.float32) for a in valid_arr]

    del raw_arr
    gc.collect()
    return final_arr, valid_prof, valid_dates, valid_source_indices


def load_and_despeckle_rtc_strict(
    df: pd.DataFrame,
    max_workers: int = 2,
    do_despeckle: bool = False,
    despeckle_method: str = "tv_bregman",
    despeckle_kwargs: dict = None,
    scene_max_retries: int = 3,
    max_failed_ratio: float = 0.0,
    retry_timeout_seconds: float = 600.0,
):
    """
    Strict Mode Downloader (VV/VH Sync) with scene-level retry and 404 exemption.

    Args:
        df: DataFrame with url_copol, url_crosspol, acq_datetime columns.
        max_workers: Total parallel download workers shared by VV/VH.
        do_despeckle: Apply spatial despeckle after download.
        despeckle_method: Despeckle algorithm — 'tv_bregman' or 'nlm'.
        despeckle_kwargs: Extra keyword arguments forwarded to despeckle_2d
            (e.g. {'reg_param': 25.0} or {'nlm_h': 25.0}).  None uses defaults.
        scene_max_retries: Max retry rounds per scene for network errors.
        max_failed_ratio: Max tolerated ratio of network-failed scenes (0.0 = zero
            tolerance). 404 scenes are always exempt and never counted as failures.
        retry_timeout_seconds: Per-scene retry budget in seconds.

    Returns:
        (final_vv, valid_vv_prof, final_vh, valid_vh_prof, valid_dates)
    """
    logger = logging.getLogger(__name__)

    urls_copol    = df["url_copol"].tolist()
    urls_crosspol = df["url_crosspol"].tolist()
    dates         = df["acq_datetime"].tolist()
    N             = len(dates)

    logger.debug("[load_and_despeckle_rtc_strict] Input: %d scenes", N)

    (
        raw_vv,
        prof_vv,
        err_vv,
        raw_vh,
        prof_vh,
        err_vh,
    ) = _download_paired_with_retry(
        urls_copol,
        urls_crosspol,
        dates,
        max_workers,
        retry_timeout_seconds,
        scene_max_retries,
    )

    # Classify scenes
    valid_vv, valid_vv_prof = [], []
    valid_vh, valid_vh_prof = [], []
    valid_dates = []
    not_found_indices   = []
    network_fail_indices = []

    for i in range(N):
        vv_ok  = raw_vv[i] is not None
        vh_ok  = raw_vh[i] is not None
        vv_err = err_vv[i]
        vh_err = err_vh[i]

        if vv_ok and vh_ok:
            valid_vv.append(raw_vv[i]);   valid_vv_prof.append(prof_vv[i])
            valid_vh.append(raw_vh[i]);   valid_vh_prof.append(prof_vh[i])
            valid_dates.append(dates[i])
        elif "not_found" in (vv_err, vh_err):
            not_found_indices.append(i)
            logger.info(
                "[Strict] Scene %d (%s) skipped: 404 not_found (vv_err=%s vh_err=%s)",
                i, dates[i], vv_err, vh_err,
            )
        else:
            network_fail_indices.append(i)
            logger.warning(
                "[Strict] Scene %d (%s) FAILED after retries: vv_err=%s vh_err=%s",
                i, dates[i], vv_err, vh_err,
            )

    logging.info(
        "Scene summary: %d valid, %d skipped (404), %d network failures / %d total",
        len(valid_dates), len(not_found_indices), len(network_fail_indices), N,
    )

    if not valid_dates:
        raise RuntimeError(
            f"No valid VV/VH pairs found — "
            f"{len(network_fail_indices)} network failures, "
            f"{len(not_found_indices)} not_found (404) out of {N} scenes."
        )

    # Strict ratio check (404 scenes excluded from denominator)
    downloadable = N - len(not_found_indices)
    if downloadable > 0 and max_failed_ratio >= 0.0:
        actual_ratio = len(network_fail_indices) / downloadable
        if actual_ratio > max_failed_ratio:
            failed_summary = ", ".join(
                f"{i}({dates[i]})" for i in network_fail_indices
            )
            raise RuntimeError(
                f"Network failure ratio {actual_ratio:.1%} exceeds max_failed_ratio "
                f"{max_failed_ratio:.1%} "
                f"({len(network_fail_indices)}/{downloadable} downloadable scenes failed). "
                f"Failed scenes: {failed_summary}. "
                f"Consider reducing max_download_workers or increasing scene_max_retries."
            )

    if do_despeckle:
        logging.info("Applying %s despeckle...", despeckle_method)
        _dkw = despeckle_kwargs or {}
        _method_key = 'tv_kwargs' if despeckle_method == 'tv_bregman' else 'nlm_kwargs'
        final_vv = [despeckle_2d(a, method=despeckle_method, **{_method_key: _dkw}) for a in valid_vv]
        final_vh = [despeckle_2d(a, method=despeckle_method, **{_method_key: _dkw}) for a in valid_vh]
    else:
        final_vv = [a.astype(np.float32) for a in valid_vv]
        final_vh = [a.astype(np.float32) for a in valid_vh]

    del raw_vv, raw_vh
    gc.collect()
    return final_vv, valid_vv_prof, final_vh, valid_vh_prof, valid_dates


# =========================================================
#  Part 5: Static Layer Download
# =========================================================

def download_static_burst_arrays(
    url_copol_list: list[str],
    layers: list[str],
    max_workers: int = 2,
    retry_timeout_seconds: float = 600.0,
) -> dict[str, tuple[list, list, list]]:
    """
    Download static layer GeoTIFFs for a list of burst copol URLs.

    Derives static layer URLs from each copol URL via suffix substitution and
    downloads them in parallel using the existing read_asf_rtc_image_data
    infrastructure (with retry, 404 handling, etc.).

    Args:
        url_copol_list: List of copol (VV/HH) URLs, one per burst.
        layers: Subset of ['dem', 'inc_map', 'ls_map'] to download.
        max_workers: Number of parallel download workers.
        retry_timeout_seconds: Per-URL retry budget in seconds.

    Returns:
        dict[layer_name, (arrs, profs, error_types)] where each tuple is
        aligned with url_copol_list. arrs[i] is None on download failure.
    """
    from s1grits.asf_tiles import derive_static_urls

    # Log the first copol URL so URL pattern issues are immediately visible.
    if url_copol_list:
        logging.info("[Static] Sample url_copol: %s", url_copol_list[0])

    results: dict[str, tuple[list, list, list]] = {}
    for layer in layers:
        urls = []
        for url_copol in url_copol_list:
            try:
                derived = derive_static_urls(url_copol)[layer]
                urls.append(derived)
                logging.debug("[Static] Derived %s URL: %s", layer, derived)
            except ValueError as e:
                # URL suffix not recognised — log clearly so the user can diagnose.
                logging.warning(
                    "[Static] Cannot derive %s URL from copol URL %r: %s",
                    layer, url_copol, e,
                )
                urls.append(None)

        # Filter out None entries: replace with empty string so read_asf_rtc_image_data
        # receives a list of the same length, but skip the actual HTTP request.
        safe_urls = [u if u is not None else '' for u in urls]

        # Warn if all URLs failed derivation
        valid_count = sum(1 for u in safe_urls if u)
        if valid_count == 0:
            logging.error(
                "[Static] All %d copol URLs failed suffix derivation for layer '%s'. "
                "Check that url_copol ends with '_VV.tif' or '_HH.tif'.",
                len(url_copol_list), layer,
            )

        arrs, profs, error_types = read_asf_rtc_image_data(
            safe_urls,
            max_workers=max_workers,
            retry_timeout_seconds=retry_timeout_seconds,
        )

        # Mark entries that had no valid URL as not_found (not network_error)
        for i, u in enumerate(safe_urls):
            if not u:
                arrs[i] = None
                profs[i] = None
                error_types[i] = 'not_found'

        results[layer] = (arrs, profs, error_types)
    return results
