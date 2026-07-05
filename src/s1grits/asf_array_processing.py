# ==============================================================================
#  asf_array_processing.py
#  In-memory array processing: interpolation, TV despeckle, GLCM texture.
#
#  This module is Cython-compilable (no nested functions, no threading.local,
#  no nonlocal mutations).  All previously-nested helpers (_quantize,
#  _metrics_one_angle) are promoted to module-level functions with explicit
#  parameter passing.
# ==============================================================================

import logging
import numpy as np
import cv2

from scipy.ndimage import distance_transform_edt
from scipy.ndimage import label as ndlabel

from skimage.restoration import denoise_tv_bregman, denoise_nl_means, estimate_sigma

# =========================================================
#  Module-Level Constants (duplicated from asf_io for independent compilation)
# =========================================================
DB_SCALE_FACTOR: float = 10.0             # Multiplier in dB conversion (10 * log10)

# =========================================================
#  Part 1: NaN Interpolation
# =========================================================

def interpolate_arr(arr: np.ndarray, method: str = 'nearest', preserve_exterior: bool = True) -> np.ndarray:
    """Interpolate NaNs, optionally preserving exterior boundaries.

    When preserve_exterior=True, NaN regions connected to the image boundary
    (tile edges, rivers that reach the boundary) are kept as NaN.  Only
    isolated interior NaN holes are filled.  This uses flood-fill labeling
    rather than erosion/dilation, which avoids incorrectly masking valid pixels
    in narrow land strips between NaN regions (e.g. river banks < 7 px wide).
    """
    mask_nan = np.isnan(arr)
    if not mask_nan.any():
        return arr

    indices = distance_transform_edt(mask_nan, return_distances=False, return_indices=True)
    arr_interp = arr[tuple(indices)]

    if preserve_exterior:
        # Label connected NaN components
        nan_labeled, _ = ndlabel(mask_nan)
        # Exterior NaN = any component that touches the image boundary.
        # Use np.unique on a concatenated boundary strip — avoids creating four
        # temporary Python set objects and is more Cython-friendly.
        boundary_vals = np.unique(np.concatenate([
            nan_labeled[0,  :].ravel(),
            nan_labeled[-1, :].ravel(),
            nan_labeled[:,  0].ravel(),
            nan_labeled[:, -1].ravel(),
        ]))
        boundary_labels = set(boundary_vals.tolist()) - {0}
        if boundary_labels:
            exterior_nan = np.isin(nan_labeled, list(boundary_labels))
        else:
            exterior_nan = np.zeros_like(mask_nan)
        # Preserve exterior NaN; fill interior NaN holes with interpolated values
        return np.where(exterior_nan, np.nan, arr_interp)

    return arr_interp


# =========================================================
#  Part 2: Despeckle (TV-Bregman + NLM)
# =========================================================

def _despeckle_tv_bregman_linear(
    X: np.ndarray,
    reg_param: float = 5.0,
    max_valid_value: float = 1.0,
    min_valid_value: float = 1e-7,
    interp_method: str = 'none',
    preserve_exterior_mask: bool = True,
    noise_floor_db: float = -23.0
) -> np.ndarray:
    """
    TV Denoising with Soft Padding (-23dB) and Interior-Only Interpolation.
    """
    if X is None:
        return None

    # 1. Clip to safe linear range
    X_c = np.clip(X, min_valid_value, max_valid_value)

    # 2. Interpolate small holes (crucial for TV stability)
    if interp_method != 'none':
        X_c = interpolate_arr(X_c, method=interp_method, preserve_exterior=preserve_exterior_mask)

    nodata_mask = np.isnan(X_c)

    # 3. Log Transform
    X_db = np.log10(X_c, where=~nodata_mask, out=np.empty_like(X_c)) * DB_SCALE_FACTOR
    X_db[nodata_mask] = np.nan

    # 4. Soft Padding (-23dB): fill remaining NaNs with noise floor for smooth TV gradients
    if nodata_mask.any():
        X_db[nodata_mask] = noise_floor_db

    # 5. TV Denoising
    weight = 1.0 / float(reg_param)
    X_db_dspkl = denoise_tv_bregman(X_db, weight=weight, isotropic=True, eps=1e-3)

    # 6. Inverse Log
    X_out = np.power(10.0, X_db_dspkl / 10.0).astype(np.float32)

    # 7. Restore original nodata mask (strict cleanup)
    X_out[nodata_mask] = np.nan

    return np.clip(X_out, min_valid_value, max_valid_value)


def _despeckle_nlm_linear(
    X: np.ndarray,
    patch_size: int = 3,
    patch_distance: int = 7,
    h: float = None,
    max_valid_value: float = 1.0,
    min_valid_value: float = 1e-7,
    interp_method: str = 'none',
    preserve_exterior_mask: bool = True,
    noise_floor_db: float = -23.0,
) -> np.ndarray:
    """
    Non-Local Means denoising in dB domain with soft NaN padding.

    NLM searches for similar patches across the image and averages them,
    preserving edges and repetitive structures (roads, forest edges) better
    than TV which penalises all gradients uniformly.

    Args:
        X: 2-D linear-domain float array.
        patch_size: Side length of the comparison patch (pixels).
            Smaller = finer detail preserved; larger = more context, slower.
            Recommended range: 3–7.
        patch_distance: Search radius for similar patches (pixels).
            Larger = more candidates found, slower.  Recommended range: 5–11.
        h: Filter strength multiplier on the adaptive sigma estimate.
            None (or 'adaptive') = 1.0 × sigma_est — a safe, data-driven default.
            Increase above 1.0 for stronger smoothing, decrease for lighter touch.
        noise_floor_db: Soft-padding value (dB) used to fill NaN pixels before
            denoising so boundary artefacts are minimised.
    """
    if X is None:
        return None

    # 1. Clip to safe linear range
    X_c = np.clip(X, min_valid_value, max_valid_value)

    # 2. Optional interior-hole interpolation
    if interp_method != 'none':
        X_c = interpolate_arr(X_c, method=interp_method, preserve_exterior=preserve_exterior_mask)

    nodata_mask = np.isnan(X_c)

    # 3. Log transform -> dB
    X_db = np.log10(X_c, where=~nodata_mask, out=np.empty_like(X_c)) * DB_SCALE_FACTOR
    X_db[nodata_mask] = np.nan

    # 4. Soft padding: fill NaN with noise floor so NLM patch search is stable
    if nodata_mask.any():
        X_db[nodata_mask] = noise_floor_db

    # 5. NLM denoising
    # h=None/'adaptive' → use sigma_est directly (neutral, data-driven default).
    # h=float           → use sigma_est * h as the cut-off distance.
    sigma_est = float(np.mean(estimate_sigma(X_db)))
    _h = h if (h is not None and str(h).lower() != 'adaptive') else None
    h_val = sigma_est if _h is None else sigma_est * float(_h)
    X_db_dspkl = denoise_nl_means(
        X_db.astype(np.float64),
        h=h_val,
        fast_mode=True,
        patch_size=int(patch_size),
        patch_distance=int(patch_distance),
    ).astype(np.float32)

    # 6. Inverse log
    X_out = np.power(10.0, X_db_dspkl / 10.0).astype(np.float32)

    # 7. Restore nodata mask
    X_out[nodata_mask] = np.nan

    return np.clip(X_out, min_valid_value, max_valid_value)


def despeckle_2d(
    img_lin,
    method: str = "tv_bregman",
    tv_kwargs: dict = None,
    nlm_kwargs: dict = None,
    max_valid_value: float = 1.0,
    min_valid_value: float = 1e-7,
    min_valid_lin: float = None,
    interp_method: str = 'none',
    preserve_exterior_mask: bool = True,
    noise_floor_db: float = -23.0,
):
    """Apply 2D despeckle to a linear-domain image array.

    Args:
        img_lin: 2-D float array in linear (not dB) domain.
        method: 'tv_bregman' or 'nlm'.
        tv_kwargs: TV-Bregman options, e.g. {'reg_param': 25.0}.
        nlm_kwargs: NLM options, e.g. {'patch_size': 3, 'patch_distance': 7, 'h': None}.
            h=None means adaptive (sigma_est from estimate_sigma); pass a float to
            override as a multiplier on sigma_est.
        max_valid_value: Upper clip threshold in linear scale.
        min_valid_value: Lower clip threshold in linear scale.
        min_valid_lin: Deprecated alias for min_valid_value.
        interp_method: NaN interpolation method passed to interpolate_arr.
        preserve_exterior_mask: Whether to preserve boundary NaN regions.
        noise_floor_db: Soft-padding value (dB) used to fill NaN during denoising.

    Returns:
        Despeckled float32 array, or None if img_lin is None.
    """
    if img_lin is None:
        return None
    if min_valid_lin is not None:
        min_valid_value = min_valid_lin

    _common = dict(
        max_valid_value=max_valid_value,
        min_valid_value=min_valid_value,
        interp_method=interp_method,
        preserve_exterior_mask=preserve_exterior_mask,
        noise_floor_db=noise_floor_db,
    )
    if method == "tv_bregman":
        return _despeckle_tv_bregman_linear(img_lin, **(tv_kwargs or {}), **_common)
    if method == "nlm":
        return _despeckle_nlm_linear(img_lin, **(nlm_kwargs or {}), **_common)
    raise ValueError(f"Unknown despeckle method: {method!r}. Choose 'tv_bregman' or 'nlm'.")


# =========================================================
#  Part 3: GLCM Texture Feature Computation
# =========================================================

# Abbreviated output band names for each GLCM metric
_GLCM_METRIC_ABBREV = {
    "contrast":    "CONTR",
    "homogeneity": "IDM",
    "entropy":     "ENT",
    "correlation": "CORR",
}


def _glcm_band_name(prefix: str, metric: str) -> str:
    """Return the output band name for a given polarization prefix and GLCM metric."""
    abbrev = _GLCM_METRIC_ABBREV.get(metric, metric.upper())
    return f"{prefix}_glcm_{abbrev}"


def _get_texture_band_names(texture_cfg: dict) -> list:
    """Return the ordered list of texture band names from config (no computation)."""
    inputs  = texture_cfg.get("inputs",  ["VV_dB", "VH_dB"])
    metrics = texture_cfg.get("metrics", ["contrast", "homogeneity", "entropy", "correlation"])
    prefix_map = {"VV_dB": "VV", "VH_dB": "VH", "HH_dB": "HH", "HV_dB": "HV"}
    names = []
    for inp in inputs:
        prefix = prefix_map.get(inp, inp.replace("_dB", ""))
        for m in metrics:
            names.append(_glcm_band_name(prefix, m))
    return names


def _glcm_quantize(db_arr: np.ndarray, db_range, levels: int, sentinel_val: int) -> np.ndarray:
    """
    Clip and quantize a float dB array to [0, levels-1] integers.
    NaN pixels are mapped to sentinel_val.

    Args:
        db_arr:      2-D float array of dB values.
        db_range:    (lo, hi) tuple defining the quantization range.
        levels:      Number of quantization levels.
        sentinel_val: Integer value assigned to NaN pixels.

    Returns:
        2-D int32 array.
    """
    lo, hi = float(db_range[0]), float(db_range[1])
    nan_mask = np.isnan(db_arr)
    # Replace NaN with 0 before cast to suppress "invalid value encountered in cast".
    # NaN pixels are overwritten with sentinel_val immediately after.
    clipped = np.clip((db_arr - lo) / (hi - lo), 0.0, 1.0) * (levels - 1)
    clipped[nan_mask] = 0.0
    q = np.floor(clipped).astype(np.int32)
    q[nan_mask] = sentinel_val
    return q


def _glcm_metrics_one_angle(
    q_arr: np.ndarray,
    angle_deg: float,
    window_size: int,
    levels: int,
    sentinel_val: int,
    distance: int,
    metrics: list,
) -> dict:
    """
    Compute GLCM metrics for one angle using a vectorized fast-GLCM approach.

    Fast paths (O(1) filter calls) are used for contrast, homogeneity, and
    correlation.  Entropy requires the O(levels^2) loop.

    Args:
        q_arr:        2-D int32 quantized array (NaN pixels = sentinel_val).
        angle_deg:    Angle in degrees for the co-occurrence shift direction.
        window_size:  Size of the square averaging window.
        levels:       Number of quantization levels.
        sentinel_val: Sentinel value for NaN pixels in q_arr.
        distance:     Pixel distance for the co-occurrence shift.
        metrics:      List of metric names to compute.

    Returns:
        dict mapping metric_name -> (H, W) float32 array.
    """
    h, w = q_arr.shape
    kernel = np.ones((window_size, window_size), dtype=np.float32)

    # Shift image by (dx, dy) for the given angle
    dx = distance * np.cos(np.deg2rad(angle_deg))
    dy = distance * np.sin(np.deg2rad(-angle_deg))
    mat = np.array([[1.0, 0.0, -dx], [0.0, 1.0, -dy]], dtype=np.float32)
    q_shifted = cv2.warpAffine(
        q_arr.astype(np.float32), mat, (w, h),
        flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REPLICATE,
    ).astype(np.int32)

    # Valid mask: pixels where neither original nor shifted is a sentinel
    valid = ((q_arr != sentinel_val) & (q_shifted != sentinel_val)).astype(np.float32)

    # N = count of valid co-occurrence pairs in each window
    N = cv2.filter2D(valid, -1, kernel, borderType=cv2.BORDER_REPLICATE).astype(np.float64)
    safe_N = np.where(N > 0, N, 1.0)

    # NaN only when the window has zero valid pairs
    nan_mask = N == 0

    need_contrast  = "contrast"    in metrics
    need_homo      = "homogeneity" in metrics
    need_ent       = "entropy"     in metrics
    need_corr      = "correlation" in metrics

    results = {}

    # --- Fast path: contrast = E[(i-j)^2] ---
    if need_contrast:
        diff_sq = ((q_arr - q_shifted).astype(np.float32) ** 2) * valid
        r = (cv2.filter2D(diff_sq, -1, kernel,
                          borderType=cv2.BORDER_REPLICATE).astype(np.float64)
             / safe_N).astype(np.float32)
        r[nan_mask] = np.nan
        results["contrast"] = r

    # --- Fast path: homogeneity = E[1/(1+(i-j)^2)] ---
    if need_homo:
        diff_sq_f = (q_arr - q_shifted).astype(np.float32) ** 2
        homo_px = valid / (1.0 + diff_sq_f)
        r = (cv2.filter2D(homo_px, -1, kernel,
                          borderType=cv2.BORDER_REPLICATE).astype(np.float64)
             / safe_N).astype(np.float32)
        r[nan_mask] = np.nan
        results["homogeneity"] = r

    # --- Fast path: correlation --- (5 filter calls)
    if need_corr:
        qi = q_arr.astype(np.float32) * valid
        qj = q_shifted.astype(np.float32) * valid
        mu_i = cv2.filter2D(qi, -1, kernel, borderType=cv2.BORDER_REPLICATE).astype(np.float64) / safe_N
        mu_j = cv2.filter2D(qj, -1, kernel, borderType=cv2.BORDER_REPLICATE).astype(np.float64) / safe_N
        E_i2 = cv2.filter2D(qi * q_arr.astype(np.float32),     -1, kernel, borderType=cv2.BORDER_REPLICATE).astype(np.float64) / safe_N
        E_j2 = cv2.filter2D(qj * q_shifted.astype(np.float32), -1, kernel, borderType=cv2.BORDER_REPLICATE).astype(np.float64) / safe_N
        E_ij = cv2.filter2D(qi * q_shifted.astype(np.float32), -1, kernel, borderType=cv2.BORDER_REPLICATE).astype(np.float64) / safe_N
        sig_i = np.sqrt(np.maximum(E_i2 - mu_i ** 2, 0.0) + 1e-10)
        sig_j = np.sqrt(np.maximum(E_j2 - mu_j ** 2, 0.0) + 1e-10)
        r = ((E_ij - mu_i * mu_j) / (sig_i * sig_j)).astype(np.float32)
        r[nan_mask] = np.nan
        results["correlation"] = r

    # --- Entropy: requires O(levels^2) loop ---
    if need_ent:
        acc_clogc = np.zeros((h, w), dtype=np.float64)
        for i in range(levels):
            row_i = (q_arr == i)
            for j in range(levels):
                mask = (row_i & (q_shifted == j)).astype(np.float32)
                cnt  = cv2.filter2D(mask, -1, kernel,
                                    borderType=cv2.BORDER_REPLICATE).astype(np.float64)
                nz = cnt > 0
                acc_clogc[nz] += cnt[nz] * np.log(cnt[nz])
        # H = log(N) - (1/N) * sum_ij cnt_ij * log(cnt_ij)
        r = (np.log(safe_N) - acc_clogc / safe_N).astype(np.float32)
        r[nan_mask] = np.nan
        results["entropy"] = r

    return results


def compute_glcm_texture_bands(
    vv_db: np.ndarray,
    vh_db: np.ndarray,
    texture_cfg: dict,
) -> tuple:
    """
    Compute GLCM texture feature bands from monthly dB composites.

    Uses a vectorized fast-GLCM approach: iterates over all (i,j) gray-level
    pairs and applies a box filter once per pair to accumulate co-occurrence
    counts across the full image simultaneously.  This is ~100-1000x faster
    than scipy.ndimage.generic_filter for typical tile sizes.

    Args:
        vv_db       : 2-D float32 array of VV backscatter in dB (NaN = nodata).
        vh_db       : 2-D float32 array of VH backscatter in dB (NaN = nodata).
        texture_cfg : Dict parsed from the YAML texture_features block.

    Returns:
        (arrays, names) where arrays is a list of float32 2-D arrays and names
        is the corresponding list of band name strings.
    """
    # --- Parse config ---
    inputs         = texture_cfg.get("inputs",         ["VV_dB", "VH_dB"])
    metrics        = texture_cfg.get("metrics",        ["contrast", "homogeneity", "entropy", "correlation"])
    window_size    = int(texture_cfg.get("window_size",    5))
    distance       = int(texture_cfg.get("distance",       1))
    angle_degrees  = texture_cfg.get("angles",         [0, 45, 90, 135])
    average_angles = bool(texture_cfg.get("average_angles", True))
    levels         = int(texture_cfg.get("levels",         32))
    vv_db_range    = texture_cfg.get("vv_db_range",    [-25.0,  5.0])
    vh_db_range    = texture_cfg.get("vh_db_range",    [-32.0, -5.0])

    # SENTINEL = levels: integer value marking NaN pixels in the quantized array
    SENTINEL   = levels
    input_map  = {"VV_dB": (vv_db, vv_db_range), "VH_dB": (vh_db, vh_db_range)}
    prefix_map = {"VV_dB": "VV", "VH_dB": "VH", "HH_dB": "HH", "HV_dB": "HV"}

    out_arrays = []
    out_names  = []

    for inp in inputs:
        if inp not in input_map:
            logging.warning("[GLCM] Unknown input '%s', skipping.", inp)
            continue
        db_arr, db_range = input_map[inp]
        prefix       = prefix_map.get(inp, inp.replace("_dB", ""))
        nan_mask_inp = np.isnan(db_arr)
        q_arr        = _glcm_quantize(db_arr, db_range, levels, SENTINEL)

        logging.debug(
            "[GLCM] Computing texture for %s (window=%d, levels=%d, angles=%s)...",
            prefix, window_size, levels, angle_degrees,
        )

        # Compute per-angle results then optionally average
        angle_results = [
            _glcm_metrics_one_angle(q_arr, a, window_size, levels, SENTINEL, distance, metrics)
            for a in angle_degrees
        ]

        for metric in metrics:
            if average_angles and len(angle_results) > 1:
                # In-place accumulation: avoids allocating a full (n_angles, H, W) stack
                # which would consume ~4x peak memory vs. sequential summation.
                acc = angle_results[0][metric].astype(np.float64)
                cnt = (~np.isnan(acc)).astype(np.float64)
                np.nan_to_num(acc, nan=0.0, copy=False)
                for ar in angle_results[1:]:
                    vals = ar[metric].astype(np.float64)
                    valid = ~np.isnan(vals)
                    acc[valid] += vals[valid]
                    cnt[valid] += 1.0
                result = np.where(cnt > 0, acc / np.maximum(cnt, 1.0), np.nan).astype(np.float32)
            else:
                result = angle_results[0][metric].copy()
            result[nan_mask_inp] = np.nan
            out_arrays.append(result)
            out_names.append(_glcm_band_name(prefix, metric))

    return out_arrays, out_names
