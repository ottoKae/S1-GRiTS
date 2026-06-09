import os
import json
import string
import warnings
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
import xarray as xr
import rioxarray
from rasterio.enums import Resampling  
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.ticker as ticker
import matplotlib.colors as mcolors
import matplotlib.ticker as mticker
import cartopy.crs as ccrs
from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER

warnings.filterwarnings("ignore")

# =========================================================================
# 0. Environment-Adaptive Path Anchoring System (GitHub & Zenodo Safe)
# =========================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ACTIVE_CWD = Path.cwd()

# Standardized folder name redistributed via Zenodo frameworks
DATA_FOLDER_NAME = "component_05_Ecuador_17MPU_pairs_pixel_level_2017_2025"

# Portable tracking array matrices
POTENTIAL_PATHS = [
    SCRIPT_DIR / DATA_FOLDER_NAME,              
    PROJECT_ROOT / DATA_FOLDER_NAME,            
    ACTIVE_CWD / DATA_FOLDER_NAME,              
    SCRIPT_DIR.parents[1] / DATA_FOLDER_NAME    
]

DATA_BASE_DIR = None
for p in POTENTIAL_PATHS:
    if p.exists():
        DATA_BASE_DIR = p
        break

if DATA_BASE_DIR is None:
    raise FileNotFoundError(
        f"Target data container directory '{DATA_FOLDER_NAME}' could not be resolved automatically. "
        "Please place the unzipped dataset folder within the local script runtime repository framework."
    )

# Establish precise input locations bound to the validated repository directory
PATH_INTER = DATA_BASE_DIR / "STATS_17MPU_cross_orbit_02d.zarr"
STATIC_PATH_ASC = DATA_BASE_DIR / "S1-GRiTS_17MPU_ASC_static"
STATIC_PATH_DESC = DATA_BASE_DIR / "S1-GRiTS_17MPU_DESC_static"

# Output configuration architecture targeting manuscript figure directories
OUTPUT_DIR = DATA_BASE_DIR / "results_figure"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================================
# 1. Processing Parameters & Data Loading Pipeline
# =========================================================================
HEX_NUM = 100  
HEX_NUM_ANF = 1200
pol = "vv"  # Locked to co-polarization VV signal analytics
TARGET_CRS = "EPSG:32717"

if not PATH_INTER.exists():
    PATH_INTER = DATA_BASE_DIR / "stats_17MPU_orbit_consistency.zarr"
    if not PATH_INTER.exists():
        raise FileNotFoundError(f"Core orbit consistency matrix Zarr store unreachable at: {PATH_INTER}")

ds_inter = xr.open_zarr(str(PATH_INTER))

def get_aligned_layer_nearest(path, filename, reference):
    target_path = Path(path) / filename
    if not target_path.exists():
        raise FileNotFoundError(f"Static reference matrix layer missing: {target_path}")
    da = rioxarray.open_rasterio(str(target_path))
    return da.rio.reproject_match(reference, resampling=Resampling.nearest).squeeze().values

print("Loading consistency control matrix baseline dataset...")
ref_da = ds_inter['t_score_vv'].rio.write_crs(TARGET_CRS)

print("Synchronizing and extracting LIA and ANF structural geo-layers...")
asc_lia = get_aligned_layer_nearest(STATIC_PATH_ASC, "17MPU_ASCENDING_static_local_inc_angle.tif", ref_da)
desc_lia = get_aligned_layer_nearest(STATIC_PATH_DESC, "17MPU_DESCENDING_static_local_inc_angle.tif", ref_da)

asc_anf_beta = get_aligned_layer_nearest(STATIC_PATH_ASC, "17MPU_ASCENDING_static_rtc_anf_beta0.tif", ref_da)
desc_anf_beta = get_aligned_layer_nearest(STATIC_PATH_DESC, "17MPU_DESCENDING_static_rtc_anf_beta0.tif", ref_da)

# Extract spatial bounding boxes from projection coordinates
utm_bounds = ref_da.rio.bounds()  

# Execute precise coordinate transformation to yield exact WGS84 bounding envelopes
utm_crs_h = ccrs.UTM(zone=17, southern_hemisphere=True)
geo_crs_h = ccrs.PlateCarree()
lon_lat_pts = geo_crs_h.transform_points(utm_crs_h, 
                                         np.array([utm_bounds[0], utm_bounds[2]]), 
                                         np.array([utm_bounds[1], utm_bounds[3]]))

dynamic_extent_geo = [lon_lat_pts[0, 0], lon_lat_pts[1, 0], lon_lat_pts[0, 1], lon_lat_pts[1, 1]]
OUTPUT_FILENAME = OUTPUT_DIR / f"c5_f7_cross_orbit_spatial_heatmap_LIA_ANF_{pol}.jpg"

# =========================================================================
# 2. Color Mapping & Typography Configurations
# =========================================================================
col_metrics = ["bias", "t_score", "std"]

metric_configs = {
    "bias": {
        "cmap": plt.get_cmap("RdYlBu_r"),         
        "norm": mcolors.Normalize(vmin=0, vmax=6), 
        "ticks": [0, 2, 4, 6],
        "label": "Absolute Bias (dB)"
    },
    "t_score": {
        "cmap": plt.get_cmap("GnBu"),  
        "norm": mcolors.Normalize(vmin=0, vmax=20),  
        "ticks": [0, 5, 10, 15, 20],
        "label": "T-score"
    },
    "std": {
        "cmap": plt.get_cmap("RdYlBu"),   
        "norm": mcolors.Normalize(vmin=0, vmax=10),  
        "ticks": [0, 2, 4, 6, 8, 10],
        "label": "Standard Deviation (dB)"
    }
}

for cfg in metric_configs.values():
    cfg["cmap"].set_bad(color="white")

font_size = 10
plt.rcParams.update({
    'font.size': font_size,              
    'axes.labelsize': font_size,         
    'xtick.labelsize': font_size,        
    'ytick.labelsize': font_size,        
    'font.family': 'sans-serif',
    'font.sans-serif': ['Arial', 'Liberation Sans', 'DejaVu Sans'],
    'xtick.direction': 'out',            
    'ytick.direction': 'out'
})

fig = plt.figure(figsize=(8.27, 9.2), dpi=300)
gs = gridspec.GridSpec(3, 3, figure=fig, 
                       wspace=0.02, hspace=0.005, 
                       left=0.12, right=0.98, bottom=0.15, top=0.95)

row3_axes_handles = []
cbar_reference_instances = {}

map_x_ticks = [-79.8, -79.4]
map_y_ticks = [-1.6, -1.2]

# =========================================================================
# 3. Core 3x3 Analytical Matrix Plotting Loop
# =========================================================================
abc_list = string.ascii_lowercase
plot_idx = 0

for r_idx in range(3):
    for c_idx, metric in enumerate(col_metrics):
        
        var_name = f"{metric}_{pol}"
        cfg = metric_configs[metric]
        y_raw = np.abs(ds_inter[var_name].values)
        
        # -----------------------------------------------------------------
        # Row 0: Geospatial Panoramic Mapping Viewports
        # -----------------------------------------------------------------
        if r_idx == 0:
            ax = fig.add_subplot(gs[r_idx, c_idx], projection=ccrs.PlateCarree())
            ax.set_aspect('equal', adjustable='box')
            
            im_map = ax.imshow(y_raw, cmap=cfg["cmap"], norm=cfg["norm"],
                               extent=dynamic_extent_geo, origin='upper', transform=ccrs.PlateCarree())
            
            ax.set_extent(dynamic_extent_geo, crs=ccrs.PlateCarree())
            
            gl = ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=True,
                              linewidth=0.5, color='grey', alpha=1.0, linestyle='--')
            gl.xlocator = mticker.FixedLocator(map_x_ticks)
            gl.ylocator = mticker.FixedLocator(map_y_ticks)
            
            gl.top_labels = True
            gl.bottom_labels = False
            gl.right_labels = False
            gl.left_labels = True if c_idx == 0 else False  
            
            gl.xformatter = LONGITUDE_FORMATTER
            gl.yformatter = LATITUDE_FORMATTER
            gl.padding = 3  
            
            gl.xlabel_style = {'size': font_size, 'color': '#333333'}
            gl.ylabel_style = {'size': font_size, 'color': '#333333', 'rotation': 90, 'va': 'center', 'ha': 'right'}
            ax.tick_params(direction='out', length=0, width=0, pad=0, labelsize=0)
            
        # -----------------------------------------------------------------
        # Row 1: Cross-Orbit Local Incidence Angle (LIA) Heatmaps
        # -----------------------------------------------------------------
        elif r_idx == 1:
            ax = fig.add_subplot(gs[r_idx, c_idx])
            ax.set_aspect('equal', adjustable='box')
            
            mask_lia = (np.isfinite(y_raw) & np.isfinite(asc_lia) & np.isfinite(desc_lia))
            df_lia = pd.DataFrame({
                'X': asc_lia[mask_lia], 'Y': desc_lia[mask_lia], 'Z': y_raw[mask_lia]
            }).sample(n=min(mask_lia.sum(), 180000), random_state=42)
            
            hb_lia = ax.hexbin(df_lia['X'], df_lia['Y'], C=df_lia['Z'], 
                               reduce_C_function=np.median, gridsize=HEX_NUM, norm=cfg["norm"], 
                               cmap=cfg["cmap"], mincnt=5, edgecolors='none')
            
            ax.grid(True, linestyle=':', alpha=0.3, color='gray')
            ax.plot([0, 80], [0, 80], color='#444444', ls='--', lw=0.6, alpha=0.5)
            
            ax.set_xlim(0, 80)
            ax.set_ylim(0, 80)
            ax.tick_params(direction='out', length=2, width=0.5, pad=3, labelsize=10)
            
            lia_ticks = [10, 30, 50, 70]
            ax.yaxis.set_major_locator(ticker.FixedLocator(lia_ticks))
            ax.xaxis.set_major_locator(ticker.FixedLocator(lia_ticks))
            
            if c_idx == 0:
                ax.set_yticklabels([f"{t}" for t in lia_ticks])
            else:
                ax.set_yticklabels([])
                
            ax.set_xticklabels([f"{t}" for t in lia_ticks])

        # -----------------------------------------------------------------
        # Row 2: Area Normalization Factor (ANF) Heatmaps
        # -----------------------------------------------------------------
        else:
            ax = fig.add_subplot(gs[r_idx, c_idx])
            ax.set_aspect('equal', adjustable='box')
            row3_axes_handles.append(ax)
            
            mask_anf = (np.isfinite(y_raw) & 
                        np.isfinite(asc_anf_beta) & (asc_anf_beta > 0.0) & 
                        np.isfinite(desc_anf_beta) & (desc_anf_beta > 0.0))
            
            df_anf = pd.DataFrame({
                'X': asc_anf_beta[mask_anf], 'Y': desc_anf_beta[mask_anf], 'Z': y_raw[mask_anf]
            }).sample(n=min(mask_anf.sum(), 180000), random_state=42)
            
            hb_anf = ax.hexbin(df_anf['X'], df_anf['Y'], C=df_anf['Z'], 
                               reduce_C_function=np.median, gridsize=HEX_NUM_ANF, norm=cfg["norm"], 
                               cmap=cfg["cmap"], mincnt=5, edgecolors='none')
            
            cbar_reference_instances[metric] = hb_anf  
            ax.grid(True, linestyle=':', alpha=0.3, color='gray')
            
            ax.axhline(y=1.0, color='#333333', ls='--', lw=0.8, alpha=0.8, zorder=28)
            ax.axvline(x=1.0, color='#333333', ls='--', lw=0.8, alpha=0.8, zorder=28)
            
            anf_limit_min, anf_limit_max = 0, 6
            ax.plot([anf_limit_min, anf_limit_max], [anf_limit_min, anf_limit_max], color='#555555', ls='--', lw=0.7, alpha=0.6, zorder=29)
            
            ax.set_xlim(anf_limit_min, anf_limit_max)
            ax.set_ylim(anf_limit_min, anf_limit_max)
            ax.tick_params(direction='out', length=2, width=0.5, pad=3, labelsize=10)
            
            anf_ticks = [1, 3, 5]
            ax.yaxis.set_major_locator(ticker.FixedLocator(anf_ticks))
            ax.xaxis.set_major_locator(ticker.FixedLocator(anf_ticks))
            
            if c_idx == 0:
                ax.set_yticklabels([f"{t}" for t in anf_ticks])
            else:
                ax.set_yticklabels([])
                
            ax.set_xticklabels([f"{t}" for t in anf_ticks])

            fig.canvas.draw()
            pos = ax.get_position()
            ax.set_position([pos.x0, pos.y0 - 0.015, pos.width, pos.height])

        ax.text(0.95, 0.95, f'({abc_list[plot_idx]})', 
                transform=ax.transAxes, fontweight='bold', fontsize=font_size,
                va='top', ha='right', zorder=35,
                bbox=dict(facecolor='white', alpha=0.8, edgecolor='none', pad=1.2))
        
        plot_idx += 1

# =========================================================================
# 4. Colorbar Alignment & Layout Processing
# =========================================================================
fig.canvas.draw()  

pos_col1 = row3_axes_handles[0].get_position()
pos_col2 = row3_axes_handles[1].get_position()
pos_col3 = row3_axes_handles[2].get_position()

cbar_height = 0.014     
cbar_y_offset = 0.04    

cbar_layouts = [
    {"metric": "bias",    "x0": pos_col1.x0, "width": pos_col1.x1 - pos_col1.x0},
    {"metric": "t_score", "x0": pos_col2.x0, "width": pos_col2.x1 - pos_col2.x0},
    {"metric": "std",     "x0": pos_col3.x0, "width": pos_col3.x1 - pos_col3.x0}
]

for layout in cbar_layouts:
    m = layout["metric"]
    cfg = metric_configs[m]
    
    cb_ax = fig.add_axes([layout["x0"], pos_col1.y0 - cbar_y_offset, layout["width"], cbar_height])
    cb = fig.colorbar(cbar_reference_instances[m], cax=cb_ax, orientation='horizontal', extend='max')
    
    cb.set_ticks(cfg["ticks"])
    cb.ax.set_xticklabels([f"{t}" for t in cfg["ticks"]], fontsize=font_size-1)
    
    cb.ax.tick_params(direction='out', length=2, pad=1.5)
    cb.set_label(cfg["label"], fontsize=font_size-1, fontweight='normal', labelpad=3)

# =========================================================================
# 5. Figure Export Initialization
# =========================================================================
print(f"Exporting publication composite figure matrix to: {OUTPUT_FILENAME}")
plt.savefig(str(OUTPUT_FILENAME), dpi=600, bbox_inches='tight', pad_inches=0.01)
plt.close(fig)
print("Pipeline process completed successfully.")