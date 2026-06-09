import os
import json
import warnings
from pathlib import Path
import numpy as np
import pandas as pd
import xarray as xr
import rioxarray
import rasterio
from scipy.stats import spearmanr
from scipy.stats import binned_statistic_2d

warnings.filterwarnings("ignore")

# =========================================================================
# 0. Environment-Adaptive Path Anchoring System (Zenodo Distribution Ready)
# =========================================================================
# 1. Resolve the absolute parent directory of the current script execution
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ACTIVE_CWD = Path.cwd()

# Define the relative target folder name redistributed via Zenodo
ZENODO_FOLDER_NAME = "component_05_Ecuador_17MPU_pairs_pixel_level_2017_2025"

# Smart Path Backtrack: Automatically detect the dataset folder in potential relative locations
POTENTIAL_PATHS = [
    SCRIPT_DIR / ZENODO_FOLDER_NAME,              # If dataset is placed directly next to the script
    PROJECT_ROOT / ZENODO_FOLDER_NAME,            # If dataset is placed at the project root level
    ACTIVE_CWD / ZENODO_FOLDER_NAME,              # If executed from the workspace containing the data
    SCRIPT_DIR.parents[1] / ZENODO_FOLDER_NAME    # Backtrack two levels (e.g., scripts/analysis/ -> project root)
]

DATA_BASE_DIR = None
for p in POTENTIAL_PATHS:
    if p.exists():
        DATA_BASE_DIR = p
        break

if DATA_BASE_DIR is None:
    raise FileNotFoundError(
        f"Target distribution dataset directory '{ZENODO_FOLDER_NAME}' could not be located. "
        "Please ensure the unzipped folder remains intact relative to the script runtime workspace."
    )

# Establish strict structural sub-paths bound tightly to the validated directory baseline
PATH_INTER = DATA_BASE_DIR / "STATS_17MPU_cross_orbit_02d.zarr"
PATH_ASC = DATA_BASE_DIR / "STATS_17MPU_intra_orbit_12d_ASC.zarr"
PATH_DESC = DATA_BASE_DIR / "STATS_17MPU_intra_orbit_12d_DESC.zarr"

STATIC_PATH_ASC = DATA_BASE_DIR / "S1-GRiTS_17MPU_ASC_static"
STATIC_PATH_DESC = DATA_BASE_DIR / "S1-GRiTS_17MPU_DESC_static"

ASC_LIA_FILENAME = "17MPU_ASCENDING_static_local_inc_angle.tif"
DESC_LIA_FILENAME = "17MPU_DESCENDING_static_local_inc_angle.tif"

TARGET_CRS = "EPSG:32717"

# 🌟 Smart Output Directory Alignment: Export outputs into an 'outputs' folder inside the dataset dir
OUTPUT_DIR = DATA_BASE_DIR / "results_figure" / "c5_t3_orbit_geometry_comprehensive_metrics_17MPU"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================================
# 1. Statistical Thresholds and Binning Configurations
# =========================================================================
METRIC_CONFIGS = {
    "bias": {
        "thresholds": [0.1, 0.2, 0.5, 1.0],  
        "hist_bins": np.array([0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, np.inf])
    },
    "t_score": {
        "thresholds": [2.0, 5.0, 10.0, 20.0],  
        "hist_bins": np.array([0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, np.inf])
    },
    "std": {
        "thresholds": [0.2, 0.5, 1.0, 1.5],  
        "hist_bins": np.array([0.0, 0.1, 0.2, 0.4, 0.6, 1.0, 1.5, 2.0, np.inf])
    }
}

DTHETA_BINS = np.arange(0, 91, 5)
LIA_BINS = np.arange(0, 91, 2)

RANDOM_STATE = 42
MAX_CORR_N = 2_000_000

# =========================================================================
# 2. Mathematical Statistics & Processing Helpers
# =========================================================================
def ensure_spatial_metadata(da, crs):
    if "x" in da.dims and "y" in da.dims:
        da = da.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=False)
    da = da.rio.write_crs(crs, inplace=False)
    return da


def open_static_layer(static_dir, filename, reference_da):
    path = Path(static_dir) / filename
    if not path.exists():
        raise FileNotFoundError(f"Static reference layer not found: {path}")
    da = rioxarray.open_rasterio(str(path))
    da = da.rio.reproject_match(reference_da)
    return da.squeeze().values.astype(np.float32)


def safe_array(ds, var_name):
    if var_name not in ds:
        raise KeyError(f"Variable '{var_name}' not found. Available keys: {list(ds.data_vars)}")
    return ds[var_name].values.astype(np.float32)


def compute_lia_contrast(asc_lia, desc_lia, geometry_mode):
    if geometry_mode == "inter":
        return np.abs(asc_lia - desc_lia).astype(np.float32)
    if geometry_mode in ["intra_asc", "intra_desc"]:
        return np.zeros_like(asc_lia, dtype=np.float32)
    raise ValueError(f"Unsupported geometry_mode: {geometry_mode}")


def build_valid_mask(abs_val, asc_lia, desc_lia, geometry_mode):
    base = np.isfinite(abs_val) & (abs_val >= 0.0)
    if geometry_mode == "inter":
        geom_valid = (np.isfinite(asc_lia) & np.isfinite(desc_lia) & 
                      (asc_lia >= 0) & (asc_lia <= 90) & (desc_lia >= 0) & (desc_lia <= 90))
        return base & geom_valid
    if geometry_mode == "intra_asc":
        return base & (np.isfinite(asc_lia) & (asc_lia >= 0) & (asc_lia <= 90))
    if geometry_mode == "intra_desc":
        return base & (np.isfinite(desc_lia) & (desc_lia >= 0) & (desc_lia <= 90))
    raise ValueError(f"Unsupported geometry_mode: {geometry_mode}")


def summarize_global_metrics(name, metric_type, abs_val, asc_lia, desc_lia, thresholds, geometry_mode):
    valid = build_valid_mask(abs_val, asc_lia, desc_lia, geometry_mode)
    z = abs_val[valid]
    dtheta = compute_lia_contrast(asc_lia, desc_lia, geometry_mode)[valid]

    row = {
        "case": name,
        "metric_type": metric_type,
        "geometry_mode": geometry_mode,
        "N_valid": int(z.size),
        "median_value": float(np.nanmedian(z)) if z.size > 0 else np.nan,
        "mean_value": float(np.nanmean(z)) if z.size > 0 else np.nan,
        "std_value": float(np.nanstd(z)) if z.size > 0 else np.nan,
        "p05_value": float(np.nanpercentile(z, 5)) if z.size > 0 else np.nan,
        "p95_value": float(np.nanpercentile(z, 95)) if z.size > 0 else np.nan,
        "median_D_LIA": float(np.nanmedian(dtheta)) if z.size > 0 else np.nan,
        "mean_D_LIA": float(np.nanmean(dtheta)) if z.size > 0 else np.nan,
        "spearman_rho": np.nan,
        "spearman_pvalue": np.nan,
    }

    for tau in thresholds:
        row[f"P_gt_{tau}"] = np.nan

    if z.size == 0:
        return row

    if geometry_mode == "inter" and np.nanstd(dtheta) > 1e-8:
        if z.size > MAX_CORR_N:
            rng = np.random.default_rng(RANDOM_STATE)
            idx = rng.choice(z.size, size=MAX_CORR_N, replace=False)
            rho, pval = spearmanr(dtheta[idx], z[idx], nan_policy="omit")
        else:
            rho, pval = spearmanr(dtheta, z, nan_policy="omit")
        row["spearman_rho"] = float(rho) if np.isfinite(rho) else np.nan
        row["spearman_pvalue"] = float(pval) if np.isfinite(pval) else np.nan

    for tau in thresholds:
        row[f"P_gt_{tau}"] = float(np.mean(z > tau))
    return row


def summarize_histogram(name, metric_type, abs_val, asc_lia, desc_lia, geometry_mode, hist_bins):
    valid = build_valid_mask(abs_val, asc_lia, desc_lia, geometry_mode)
    z = abs_val[valid]
    if z.size == 0: 
        return pd.DataFrame()

    counts, edges = np.histogram(z, bins=hist_bins)
    fractions = counts / counts.sum() if counts.sum() > 0 else counts
    cumulative = np.cumsum(fractions)

    rows = []
    for i in range(len(counts)):
        rows.append({
            "case": name, "metric_type": metric_type, "geometry_mode": geometry_mode,
            "bin_left": float(edges[i]), "bin_right": float(edges[i + 1]) if np.isfinite(edges[i + 1]) else np.inf,
            "N": int(counts[i]), "fraction": float(fractions[i]), "cumulative_fraction": float(cumulative[i])
        })
    return pd.DataFrame(rows)


def summarize_by_dtheta_bins(name, metric_type, abs_val, asc_lia, desc_lia, dtheta_bins, thresholds, geometry_mode):
    valid = build_valid_mask(abs_val, asc_lia, desc_lia, geometry_mode)
    z = abs_val[valid]
    dtheta = compute_lia_contrast(asc_lia, desc_lia, geometry_mode)[valid]

    rows = []
    for i in range(len(dtheta_bins) - 1):
        lo, hi = dtheta_bins[i], dtheta_bins[i + 1]
        m = (dtheta >= lo) & (dtheta < hi)
        
        row = {
            "case": name, "metric_type": metric_type, "geometry_mode": "inter",
            "D_LIA_bin_left": float(lo), "D_LIA_bin_right": float(hi), "N": 0,
            "median_val": np.nan, "mean_val": np.nan, "std_val": np.nan
        }
        for tau in thresholds: 
            row[f"P_gt_{tau}"] = np.nan

        if np.any(m):
            zz = z[m]
            row.update({
                "N": int(zz.size), "median_val": float(np.nanmedian(zz)),
                "mean_val": float(np.nanmean(zz)), "std_val": float(np.nanstd(zz))
            })
            for tau in thresholds: 
                row[f"P_gt_{tau}"] = float(np.mean(zz > tau))
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_by_lia_2d_bins(name, metric_type, abs_val, asc_lia, desc_lia, lia_bins, thresholds):
    valid = build_valid_mask(abs_val, asc_lia, desc_lia, geometry_mode="inter")
    x, y, z = asc_lia[valid], desc_lia[valid], abs_val[valid]
    if z.size == 0: 
        return pd.DataFrame()

    stat_median, x_edges, y_edges, _ = binned_statistic_2d(x, y, z, statistic="median", bins=[lia_bins, lia_bins])
    stat_mean, _, _, _ = binned_statistic_2d(x, y, z, statistic="mean", bins=[lia_bins, lia_bins])
    stat_count, _, _, _ = binned_statistic_2d(x, y, z, statistic="count", bins=[lia_bins, lia_bins])

    frac_maps = {}
    for tau in thresholds:
        indicator = (z > tau).astype(np.float32)
        frac_maps[tau], _, _, _ = binned_statistic_2d(x, y, indicator, statistic="mean", bins=[lia_bins, lia_bins])

    rows = []
    for ix in range(len(x_edges) - 1):
        for iy in range(len(y_edges) - 1):
            n = stat_count[ix, iy]
            if not np.isfinite(n) or n < 1: 
                continue

            row = {
                "case": name, "metric_type": metric_type, "geometry_mode": "inter",
                "ASC_LIA_left": float(x_edges[ix]), "ASC_LIA_right": float(x_edges[ix + 1]),
                "DESC_LIA_left": float(y_edges[iy]), "DESC_LIA_right": float(y_edges[iy + 1]),
                "N": int(n), "median_val": float(stat_median[ix, iy]), "mean_val": float(stat_mean[ix, iy])
            }
            for tau in thresholds: 
                row[f"P_gt_{tau}"] = float(frac_maps[tau][ix, iy])
            rows.append(row)
    return pd.DataFrame(rows)


# =========================================================================
# 3. Main Data Pipeline Execution Block
# =========================================================================
if __name__ == "__main__":
    print(f">>> Workspace resolved. Active Base Directory locked at: {DATA_BASE_DIR}")
    print(">>> Initializing Zarr Big Data Cube Loading...")

    ds_inter = xr.open_zarr(str(PATH_INTER))
    ds_asc = xr.open_zarr(str(PATH_ASC))
    ds_desc = xr.open_zarr(str(PATH_DESC))

    ref_da = ensure_spatial_metadata(ds_inter["bias_vv"], TARGET_CRS)

    print(">>> Loading and Aligning Local Incidence Angle (LIA) Geo-Layers...")
    asc_lia_arr = open_static_layer(STATIC_PATH_ASC, ASC_LIA_FILENAME, ref_da)
    desc_lia_arr = open_static_layer(STATIC_PATH_DESC, DESC_LIA_FILENAME, ref_da)

    cases_template = [
        {"case": "inter_ASC_DESC_VV", "ds": ds_inter, "pol": "vv", "mode": "inter"},
        {"case": "inter_ASC_DESC_VH", "ds": ds_inter, "pol": "vh", "mode": "inter"},
        {"case": "intra_ASC_VV",      "ds": ds_asc,   "pol": "vv", "mode": "intra_asc"},
        {"case": "intra_ASC_VH",      "ds": ds_asc,   "pol": "vh", "mode": "intra_asc"},
        {"case": "intra_DESC_VV",     "ds": ds_desc,  "pol": "vv", "mode": "intra_desc"},
        {"case": "intra_DESC_VH",     "ds": ds_desc,  "pol": "vh", "mode": "intra_desc"},
    ]

    global_all, hist_all, dtheta_all, lia2d_all = [], [], [], []

    for metric in ["bias", "t_score", "std"]:
        print(f"\nProcessing Metric Framework Layer: [{metric.upper()}]")
        cfg = METRIC_CONFIGS[metric]

        for template in cases_template:
            var_name = f"{metric}_{template['pol']}"
            case_name = f"{template['case']}_{metric.upper()}"
            
            raw_array = safe_array(template["ds"], var_name)
            abs_val = np.abs(raw_array)

            row_global = summarize_global_metrics(
                name=case_name, metric_type=metric, abs_val=abs_val,
                asc_lia=asc_lia_arr, desc_lia=desc_lia_arr, thresholds=cfg["thresholds"], geometry_mode=template["mode"]
            )
            global_all.append(row_global)

            df_hist = summarize_histogram(
                name=case_name, metric_type=metric, abs_val=abs_val,
                asc_lia=asc_lia_arr, desc_lia=desc_lia_arr, geometry_mode=template["mode"], hist_bins=cfg["hist_bins"]
            )
            hist_all.append(df_hist)

            df_bin = summarize_by_dtheta_bins(
                name=case_name, metric_type=metric, abs_val=abs_val,
                asc_lia=asc_lia_arr, desc_lia=desc_lia_arr, dtheta_bins=DTHETA_BINS, thresholds=cfg["thresholds"], geometry_mode=template["mode"]
            )
            dtheta_all.append(df_bin)

            if template["mode"] == "inter":
                df_2d = summarize_by_lia_2d_bins(
                    name=case_name, metric_type=metric, abs_val=abs_val,
                    asc_lia=asc_lia_arr, desc_lia=desc_lia_arr, lia_bins=LIA_BINS, thresholds=cfg["thresholds"]
                )
                lia2d_all.append(df_2d)

    print("\n>>> Merging Substructures and Saving Metrics to Disk...")
    
    df_global_main = pd.DataFrame(global_all)
    df_global_main.to_csv(OUTPUT_DIR / "comprehensive_global_metrics.csv", index=False)

    pd.concat(hist_all, ignore_index=True).to_csv(OUTPUT_DIR / "comprehensive_histogram_metrics.csv", index=False)
    pd.concat(dtheta_all, ignore_index=True).to_csv(OUTPUT_DIR / "comprehensive_D_LIA_bin_metrics.csv", index=False)
    
    if lia2d_all:
        pd.concat(lia2d_all, ignore_index=True).to_csv(OUTPUT_DIR / "comprehensive_LIA_2D_bin_metrics_inter_only.csv", index=False)

    # =========================================================================
    # 4. Refining Manuscript Tables for LaTeX Export Compilation
    # =========================================================================
    print("Extracting refined matrix indices matching LaTeX manuscript layout specifications...")

    compact_rows = []
    for row in global_all:
        m = row["metric_type"]
        taus = METRIC_CONFIGS[m]["thresholds"]
        
        compact_row = {
            "case": row["case"],
            "metric": m,
            "geometry_mode": row["geometry_mode"],
            "N_valid_M": round(row["N_valid"] / 1e6, 3),
            "median_val": round(row["median_value"], 3) if np.isfinite(row["median_value"]) else np.nan,
            "mean_val": round(row["mean_value"], 3) if np.isfinite(row["mean_value"]) else np.nan,
            "std_val": round(row["std_value"], 3) if np.isfinite(row["std_value"]) else np.nan,
            "spearman_rho": round(row["spearman_rho"], 3) if np.isfinite(row["spearman_rho"]) else np.nan,
            f"P_gt_{taus[0]} (%)": round(row[f"P_gt_{taus[0]}"] * 100, 2) if np.isfinite(row[f"P_gt_{taus[0]}"]) else np.nan,
            f"P_gt_{taus[1]} (%)": round(row[f"P_gt_{taus[1]}"] * 100, 2) if np.isfinite(row[f"P_gt_{taus[1]}"]) else np.nan,
            f"P_gt_{taus[2]} (%)": round(row[f"P_gt_{taus[2]}"] * 100, 2) if np.isfinite(row[f"P_gt_{taus[2]}"]) else np.nan,
            f"P_gt_{taus[3]} (%)": round(row[f"P_gt_{taus[3]}"] * 100, 2) if np.isfinite(row[f"P_gt_{taus[3]}"]) else np.nan,
        }
        compact_rows.append(compact_row)

    df_manuscript = pd.DataFrame(compact_rows)
    df_manuscript.to_csv(OUTPUT_DIR / "manuscript_ready_rounded_metrics.csv", index=False)

    print("\n" + "="*70 + "\nLaTeX Manuscript Compilation Report Matrix Summary\n" + "="*70)
    print(df_manuscript[["case", "metric", "N_valid_M", "median_val", "spearman_rho"]].to_string(index=False))
    print(f"\nPipeline closed successfully. Outputs exported to:\n{OUTPUT_DIR}")