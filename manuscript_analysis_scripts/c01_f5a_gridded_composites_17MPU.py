import os
import json
import warnings
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
import xarray as xr
import rioxarray
import rasterio

import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import cartopy.crs as ccrs
from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER

warnings.filterwarnings("ignore")

# =========================================================================
# 1. Base Configuration & Path Deadlock (Dynamic Environment Adaptive)
# =========================================================================
# Resolve baseline script location tracking variables
SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_CWD = Path.cwd()

# Define the standard subdirectory container name
DATA_FOLDER_NAME = "component_01_Ecuador_mainland_DESC_2026Jan"

# Portable tracking array matrix to handle repository distribution locations
POTENTIAL_ROOTS = [
    SCRIPT_DIR / "S1-GRiTS_Zenodo_Release_v1" / DATA_FOLDER_NAME,
    SCRIPT_DIR.parent / "S1-GRiTS_Zenodo_Release_v1" / DATA_FOLDER_NAME,
    ACTIVE_CWD / "S1-GRiTS_Zenodo_Release_v1" / DATA_FOLDER_NAME,
    SCRIPT_DIR.parents[2] / "S1-GRiTS_Zenodo_Release_v1" / DATA_FOLDER_NAME
]

DATA_BASE_DIR = None
for p in POTENTIAL_ROOTS:
    if p.exists():
        DATA_BASE_DIR = p
        break

if DATA_BASE_DIR is None:
    # Safe fallback default matching current active working directory structures
    DATA_BASE_DIR = ACTIVE_CWD / "S1-GRiTS_Zenodo_Release_v1" / DATA_FOLDER_NAME

# Bind structured input paths dynamically to the verified directory anchor
INPUT_COG = DATA_BASE_DIR / "data" / "17MPU_S1_Monthly_DESCENDING_2026-01.tif"
INPUT_JSON_PARAMS = DATA_BASE_DIR / "vrt" / "vrt_stretch_params_Ecuador.json"
OUTPUT_DIR_PNG = DATA_BASE_DIR / "figure"

# Convert to standard string paths for geospatial engine compatibility
INPUT_COG = str(INPUT_COG)
INPUT_JSON_PARAMS = str(INPUT_JSON_PARAMS)
OUTPUT_DIR_PNG = str(OUTPUT_DIR_PNG)

# =========================================================================
HEX_NUM = 100  
HEX_NUM_ANF = 1200
pol = "vv"  
GAMMA = 3.0
DPI = 600
TILE_FIGSIZE = (16, 18)  # Optimized aspect ratio for title-free layout

RGB_BANDS = (0, 1, 4)  # R=VV_dB, G=VH_dB, B=VV_glcm_CONTR
BAND_NAMES = ("VV_dB", "VH_dB", "VV_glcm_CONTR")

cog_stem = Path(INPUT_COG).stem
OUTPUT_PNG_TILE = str(Path(OUTPUT_DIR_PNG) / f"{cog_stem}.png")

# Global initialization of typography (Arial family, 36pt, regular)
font_size = 36
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

# =========================================================================
# 2. Fast Loading of Global Stretch Parameters
# =========================================================================
print("Loading global stretch parameters from JSON configuration...")
if not Path(INPUT_JSON_PARAMS).exists():
    raise FileNotFoundError(f"Target stretch JSON file not found: {INPUT_JSON_PARAMS}")

with open(INPUT_JSON_PARAMS, "r", encoding="utf-8") as f:
    stretch_config = json.load(f)

# Extract cross-tile locked percentile thresholds mapping dictionary
TILE_VRT_PERCENTILES = stretch_config["vrt_percentiles"]

print("Global stretch percentiles successfully injected:")
print(f"   R ({BAND_NAMES[0]}): {TILE_VRT_PERCENTILES['r']}")
print(f"   G ({BAND_NAMES[1]}): {TILE_VRT_PERCENTILES['g']}")
print(f"   B ({BAND_NAMES[2]}): {TILE_VRT_PERCENTILES['b']}")

# =========================================================================
# 3. Core False-Color Composite Rendering Engine
# =========================================================================
def visualize_tile_cog_bands(
    cog_path, output_path, rgb_bands=(0, 1, 2), band_names=("VV_dB", "VH_dB", "Ratio"),
    month_str="2026-01", direction="DESCENDING", vrt_percentiles=None,
    gamma=1.0, grid_interval=0.2, grid_color="#555555", figsize=(16, 18), dpi=600, show_grid=True
):
    """
    Generates a title-free publication-ready false-color composite map from a single COG tile
    using locked global VRT radiometric stretching criteria with omnidirectional text labels.
    """
    def _norm_percentile(arr, lo, hi, gam):
        arr = arr.astype(np.float32, copy=False)
        if hi <= lo: return np.zeros_like(arr)
        out = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
        if gam != 1.0: out = np.power(out, gam)
        out[~np.isfinite(arr)] = np.nan
        return out

    def _compute_grid(vmin, vmax, step):
        start = np.floor(vmin / step) * step
        end   = np.ceil(vmax  / step) * step
        return np.round(np.arange(start, end + step * 0.5, step), 10)

    print("=" * 60)
    print(f"Processing COG Tile: {Path(cog_path).name}")

    ds = rioxarray.open_rasterio(cog_path, masked=True)
    if str(ds.rio.crs) != "EPSG:4326":
        print("   Reprojecting grid coordinates to EPSG:4326...")
        ds = ds.rio.reproject("EPSG:4326", resampling=rasterio.enums.Resampling.nearest)

    idx_r, idx_g, idx_b = rgb_bands
    name_r, name_g, name_b = band_names

    bounds = ds.rio.bounds()
    minx, maxx, miny, maxy = bounds[0], bounds[2], bounds[1], bounds[3]
    extent_img = [minx, maxx, miny, maxy]

    r_data = ds.isel(band=idx_r).values.astype(np.float32)
    g_data = ds.isel(band=idx_g).values.astype(np.float32)
    b_data = ds.isel(band=idx_b).values.astype(np.float32)
    ds.close()

    valid_mask = np.isfinite(r_data) & np.isfinite(g_data) & np.isfinite(b_data)
    alpha = valid_mask.astype(np.float32)

    print(f"   Normalizing layer arrays using global parameters (gamma={gamma})...")
    r_norm = _norm_percentile(r_data, vrt_percentiles["r"][0], vrt_percentiles["r"][1], gamma)
    g_norm = _norm_percentile(g_data, vrt_percentiles["g"][0], vrt_percentiles["g"][1], gamma)
    b_norm = _norm_percentile(b_data, vrt_percentiles["b"][0], vrt_percentiles["b"][1], gamma)

    rgba = np.dstack((
        np.nan_to_num(r_norm, nan=0.0),
        np.nan_to_num(g_norm, nan=0.0),
        np.nan_to_num(b_norm, nan=0.0),
        alpha
    ))
    r_data = g_data = b_data = r_norm = g_norm = b_norm = alpha = None

    print("Initializing figure canvas...")
    plot_crs = ccrs.PlateCarree()

    fig = plt.figure(figsize=figsize, facecolor="white")
    ax = plt.axes(projection=plot_crs)
    ax.set_extent(extent_img, crs=plot_crs)
    
    # Render false-color composite raster layer (zorder=10)
    ax.imshow(rgba, extent=extent_img, transform=plot_crs, origin="upper",
              interpolation="nearest", zorder=10)
    ax.set_aspect("equal")

    # Bounding neatline frame configurations
    ax.spines['geo'].set_linewidth(3.5)
    ax.spines['geo'].set_color('black')

    if show_grid:
        # Create a silent gridline foundation to isolate conflicts from the Cartopy label engine
        gl = ax.gridlines(crs=plot_crs, draw_labels=False, linewidth=0.0, alpha=0.0)
        gl.xlines = gl.ylines = False

        # Compute step intervals for geographic grids
        x_ticks = _compute_grid(minx, maxx, grid_interval)
        y_ticks = _compute_grid(miny, maxy, grid_interval)
        
        # Clip coordinates to strictly match the valid bounds of the image extent
        x_ticks = [x for x in x_ticks if minx - 1e-9 <= x <= maxx + 1e-9]
        y_ticks = [y for y in y_ticks if miny - 1e-9 <= y <= maxy + 1e-9]

        # Enable native matplotlib axis ticks to lock omnidirectional frames
        ax.set_xticks(x_ticks, crs=plot_crs)
        ax.set_yticks(y_ticks, crs=plot_crs)
        
        # Format geographic notation tags
        lon_labels = [f"{abs(x):.1f}°W" for x in x_ticks]
        lat_labels = [f"{abs(y):.1f}°S" for y in y_ticks]
        
        ax.set_xticklabels(lon_labels, fontsize=font_size, color="black")
        ax.set_yticklabels(lat_labels, fontsize=font_size, color="black")

        # Activate dual mirrored tick parameters for top+bottom and left+right alignments
        ax.xaxis.set_tick_params(top=True, bottom=True, labeltop=True, labelbottom=True, 
                                 direction='out', length=12, width=2.5, pad=12)
        ax.yaxis.set_tick_params(left=True, right=True, labelleft=True, labelright=True,
                                 direction='out', length=12, width=2.5, pad=12)

        # Manually plot high-zorder (zorder=100) dashed lines to cut through image data layers safely
        for x in x_ticks:
            ax.plot([x, x], [miny, maxy], transform=plot_crs,
                    color=grid_color, linewidth=1.5, linestyle="--", alpha=0.85, zorder=100)
            
        for y in y_ticks:
            ax.plot([minx, maxx], [y, y], transform=plot_crs,
                    color=grid_color, linewidth=1.5, linestyle="--", alpha=0.85, zorder=100)

        # Loop through axis tick labels to enforce 90-degree vertical alignment constraints safely
        fig.canvas.draw()
        for tick in ax.yaxis.get_major_ticks():
            # Left side Y-axis labels adjustment
            tick.label1.set_rotation(90)
            tick.label1.set_verticalalignment('center')
            tick.label1.set_horizontalalignment('right')
            # Right side Y-axis labels adjustment
            tick.label2.set_rotation(90)
            tick.label2.set_verticalalignment('center')
            tick.label2.set_horizontalalignment('left')

    # Repair bounding mask borders to overlay grid overlaps at the extreme margins
    ax.plot([minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny],
            transform=plot_crs, color="black", linewidth=5.0, zorder=150)

    # Tighten clean subplots margins layout for omnidirectional labeling frames
    fig.subplots_adjust(left=0.15, right=0.85, bottom=0.08, top=0.92)

    # Format self-explaining publication filename conventions
    out_path = Path(output_path)
    indices_tag = "_" + "".join(str(b) for b in rgb_bands) 
    bands_suffix = f"_RGB_{'_'.join(band_names)}"
    
    out_path = out_path.with_name(f"{out_path.stem}{indices_tag}{bands_suffix}{out_path.suffix}")
    
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Exporting title-free publication image to: {out_path}")
    fig.savefig(out_path, dpi=dpi, facecolor="white", bbox_inches='tight', pad_inches=0.02)
    plt.close(fig)

    print("=" * 60)
    print("SUCCESS: Publication-ready tile figure exported safely!")
    print("=" * 60)
    return fig, ax

# =========================================================================
# 4. Pipeline Execution
# =========================================================================
if __name__ == "__main__":
    fig, ax = visualize_tile_cog_bands(
        cog_path=INPUT_COG,
        output_path=OUTPUT_PNG_TILE,
        rgb_bands=RGB_BANDS,
        band_names=BAND_NAMES,
        month_str="2026-01",
        direction="DESCENDING",
        vrt_percentiles=TILE_VRT_PERCENTILES,
        gamma=GAMMA,
        grid_interval=0.2, 
        dpi=DPI,
        figsize=TILE_FIGSIZE,
    )