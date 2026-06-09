import os
import json
import warnings
from pathlib import Path
import numpy as np
import geopandas as gpd
import xarray as xr
import rioxarray
import rasterio

import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from matplotlib.patches import Patch
from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER

warnings.filterwarnings("ignore")

# =========================================================================
# 1. Base Configuration & Path Deadlock (Dynamic Environment Adaptive)
# =========================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_CWD = Path.cwd()

# Define the target subdirectory folder container name distributed via Zenodo
DATA_FOLDER_NAME = "component_01_Ecuador_mainland_DESC_2026Jan"

# Portable backtracking array matrix to handle repository distribution structures
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
    # Default fallback execution anchor matching local runtime structures
    DATA_BASE_DIR = ACTIVE_CWD / "S1-GRiTS_Zenodo_Release_v1" / DATA_FOLDER_NAME

# Bind input tracking vectors dynamically using the verified base path anchor
INPUT_VRT = DATA_BASE_DIR / "vrt" / "17MMU-18NVF-2026-01-DESCENDING-4326.vrt"
INPUT_JSON_PARAMS = DATA_BASE_DIR / "vrt" / "vrt_stretch_params_Ecuador.json"

# 🌟 Fixed: Define potential shapefile locations as a clean portable list
# Priority 1: Check the 'boundary' folder sitting directly next to this script (Your current setup)
# Priority 2: Check the 'boundary' folder inside the Zenodo standard structure
POTENTIAL_SHP_PATHS = [
    SCRIPT_DIR / "boundary" / "Ecuador_0_without_islands.shp",
    DATA_BASE_DIR / "boundary" / "Ecuador_0_without_islands.shp",
    DATA_BASE_DIR / "figure" / "boundary" / "Ecuador_0_without_islands.shp"
]

INPUT_SHP = None
for p in POTENTIAL_SHP_PATHS:
    if p.exists():
        INPUT_SHP = p
        break

if INPUT_SHP is None:
    # Strict fallback fallback targeting the local script-relative directory
    INPUT_SHP = SCRIPT_DIR / "boundary" / "Ecuador_0_without_islands.shp"

OUTPUT_DIR_PNG = DATA_BASE_DIR / "figure"

# Convert to standard string paths for geospatial engine compatibility
INPUT_VRT = str(INPUT_VRT)
INPUT_JSON_PARAMS = str(INPUT_JSON_PARAMS)
INPUT_SHP = str(INPUT_SHP)
OUTPUT_DIR_PNG = str(OUTPUT_DIR_PNG)

# =========================================================================
GAMMA = 3.0
DPI = 300
MOSAIC_FIGSIZE = (16, 14)
GRID_INTERVAL = 1.0  # 1.0 degree spacing for macro-scale map overview

RGB_BANDS = (0, 1, 4)  # R=VV_dB, G=VH_dB, B=VV_glcm_CONTR
BAND_NAMES = ("VV_dB", "VH_dB", "VV_glcm_CONTR")

vrt_stem = Path(INPUT_VRT).stem
OUTPUT_PNG_MOSAIC = str(Path(OUTPUT_DIR_PNG) / f"{vrt_stem}_mosaic.png")

# Global initialization of typography (Arial family, 28pt, regular)
font_size = 28
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

TILE_VRT_PERCENTILES = stretch_config["vrt_percentiles"]

# =========================================================================
# 3. Core Mosaic False-Color Composite Rendering Pipeline
# =========================================================================
def create_s1_false_color_composite(
    vrt_path, boundary_shp, output_path, downsample=10, rgb_bands=(0, 1, 2),
    band_names=("VV_dB", "VH_dB", "Ratio"), vrt_percentiles=None, gamma=1.0, grid_interval=1.0, grid_color="#555555",
    figsize=(16, 14), dpi=300, show_grid=True
):
    """
    Generates a title-free publication-ready false-color composite map from a Sentinel-1 VRT mosaic
    with omnidirectional coordinates, high-zorder gridlines, and a locked foreground legend layer.
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
    print(f"Loading Sentinel-1 VRT: {Path(vrt_path).name}")

    # 1. Load VRT with lazy loading
    ds = rioxarray.open_rasterio(vrt_path, chunks='auto', masked=True)
    print(f"   Dataset shape: {ds.shape}")
    print(f"   CRS: {ds.rio.crs}")

    if str(ds.rio.crs) != "EPSG:4326":
        print(f"   Reprojecting grid coordinates from {ds.rio.crs} to EPSG:4326...")
        ds = ds.rio.reproject("EPSG:4326", resampling=rasterio.enums.Resampling.bilinear)

    # 2. Downsample for memory and performance efficiency
    if downsample > 1:
        print(f"   Downsampling dataset by a factor of {downsample}...")
        ds_downsampled = ds.coarsen(x=downsample, y=downsample, boundary='trim').mean()
    else:
        ds_downsampled = ds

    # 3. Load regional boundary shapefile
    print(f"Loading boundary: {Path(boundary_shp).name}")
    if not Path(boundary_shp).exists():
        raise FileNotFoundError(f"Required boundary file could not be parsed: {boundary_shp}")
    gdf = gpd.read_file(boundary_shp)
    if str(gdf.crs) != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")

    # 4. Clip to regional boundary vector layer
    print("   Clipping mosaic data layers to target vector boundary...")
    try:
        ds_clipped = ds_downsampled.rio.clip(gdf.geometry, ds_downsampled.rio.crs, drop=True, all_touched=False)
        print(f"   Clipped shape: {ds_clipped.shape}")
    except Exception as e:
        print(f"ERROR - Clipping failed: {e}")
        ds.close()
        return None, None

    # 5. Extract RGB channels matrix
    idx_r, idx_g, idx_b = rgb_bands
    name_r, name_g, name_b = band_names

    xmin, xmax = float(ds_clipped.x.min()), float(ds_clipped.x.max())
    ymin, ymax = float(ds_clipped.y.min()), float(ds_clipped.y.max())
    res_x = abs(float(ds_clipped.rio.resolution()[0]))
    res_y = abs(float(ds_clipped.rio.resolution()[1]))

    r_data = ds_clipped.isel(band=idx_r).values
    g_data = ds_clipped.isel(band=idx_g).values
    b_data = ds_clipped.isel(band=idx_b).values

    # Explicitly release GDAL unmanaged memory handles
    ds_clipped = None
    ds_downsampled = None
    ds.close()
    ds = None

    # 6. Generate precise alpha valid data mask channel
    valid_mask = (~np.isnan(r_data)) & (~np.isnan(g_data)) & (~np.isnan(b_data))
    alpha = valid_mask.astype(np.float32)

    # 7. Normalize layers using locked cross-tile JSON percentile structures
    print(f"   Normalizing layer arrays using global locked JSON boundaries (gamma={gamma})...")
    r_norm = _norm_percentile(r_data, vrt_percentiles["r"][0], vrt_percentiles["r"][1], gamma)
    g_norm = _norm_percentile(g_data, vrt_percentiles["g"][0], vrt_percentiles["g"][1], gamma)
    b_norm = _norm_percentile(b_data, vrt_percentiles["b"][0], vrt_percentiles["b"][1], gamma)

    r_data = g_data = b_data = None
    r_norm = np.nan_to_num(r_norm, 0)
    g_norm = np.nan_to_num(g_norm, 0)
    b_norm = np.nan_to_num(b_norm, 0)

    # 8. Stack into final RGBA display matrix
    rgba = np.dstack((r_norm, g_norm, b_norm, alpha))
    r_norm = g_norm = b_norm = alpha = None

    # 9. Execution of advanced geospatial plotting
    print("Initializing figure canvas architecture...")
    plot_crs = ccrs.PlateCarree()

    fig = plt.figure(figsize=figsize, facecolor="white")
    ax = plt.axes(projection=plot_crs)

    # Enforce clear spatial extents including a neat margin padding buffer
    margin = 0.2
    ax.set_extent([xmin - margin, xmax + margin, ymin - margin, ymax + margin], crs=plot_crs)

    # Add standard reference background basemaps beneath the raster layer overlay
    ax.add_feature(cfeature.OCEAN, facecolor='#cde8f5', zorder=1)
    ax.add_feature(cfeature.LAND, facecolor='#ffffff', zorder=2)
    ax.add_feature(cfeature.COASTLINE, linewidth=1.5, edgecolor='#555555', alpha=0.8, zorder=3)

    # Map raster spacing matrix
    extent = [xmin - res_x/2, xmax + res_x/2, ymin - res_y/2, ymax + res_y/2]

    # Overlay false-color mosaic composite arrays
    ax.imshow(rgba, extent=extent, transform=plot_crs, origin='upper', interpolation='nearest', zorder=10)

    # Overlay vector boundary neatline framework above the imagery
    ax.add_geometries(gdf.geometry, crs=plot_crs, facecolor='none', edgecolor='black', linewidth=2.0, zorder=20)

    # Bounding frame line configs
    ax.spines['geo'].set_linewidth(3.5)
    ax.spines['geo'].set_color('black')

    if show_grid:
        # Create a silent gridline foundation to isolate conflicts from the Cartopy label engine
        gl = ax.gridlines(crs=plot_crs, draw_labels=False, linewidth=0.0, alpha=0.0)
        gl.xlines = gl.ylines = False

        # Generate tick tracking arrays based on geographic interval limits
        x_ticks = _compute_grid(xmin, xmax, grid_interval)
        y_ticks = _compute_grid(ymin, ymax, grid_interval)
        
        x_ticks = [x for x in x_ticks if (xmin - margin) <= x <= (xmax + margin)]
        y_ticks = [y for y in y_ticks if (ymin - margin) <= y <= (ymax + margin)]

        # Bind native matplotlib multi-axis control frames
        ax.set_xticks(x_ticks, crs=plot_crs)
        ax.set_yticks(y_ticks, crs=plot_crs)
        
        lon_labels = [f"{abs(x):.1f}°W" for x in x_ticks]
        lat_labels = [f"{abs(y):.1f}°S" for y in y_ticks]
        
        ax.set_xticklabels(lon_labels, fontsize=font_size, color="black")
        ax.set_yticklabels(lat_labels, fontsize=font_size, color="black")

        # Activate dual mirrored tick parameters for top+bottom and left+right corporate layouts
        ax.xaxis.set_tick_params(top=True, bottom=True, labeltop=True, labelbottom=True, 
                                 direction='out', length=12, width=2.5, pad=12)
        ax.yaxis.set_tick_params(left=True, right=True, labelleft=True, labelright=True,
                                 direction='out', length=12, width=2.5, pad=12)

        # Plot high-zorder (zorder=100) dashed gray grid lines directly
        for x in x_ticks:
            ax.plot([x, x], [ymin - margin, ymax + margin], transform=plot_crs,
                    color=grid_color, linewidth=1.5, linestyle="--", alpha=0.85, zorder=100)
        for y in y_ticks:
            ax.plot([xmin - margin, xmax + margin], [y, y], transform=plot_crs,
                    color=grid_color, linewidth=1.5, linestyle="--", alpha=0.85, zorder=100)

        # Enforce 90-degree vertical rotation properties cleanly on both Y-axis text lines
        fig.canvas.draw()
        for tick in ax.yaxis.get_major_ticks():
            tick.label1.set_rotation(90)
            tick.label1.set_verticalalignment('center')
            tick.label1.set_horizontalalignment('right')
            tick.label2.set_rotation(90)
            tick.label2.set_verticalalignment('center')
            tick.label2.set_horizontalalignment('left')

    # Insert structured legend block with white facecolor and a solid gray border
    legend_elements = [
        Patch(facecolor='#d62728', alpha=0.8, edgecolor='none', label=f'R: {name_r}'),
        Patch(facecolor='#2ca02c', alpha=0.8, edgecolor='none', label=f'G: {name_g}'),
        Patch(facecolor='#1f77b4', alpha=0.8, edgecolor='none', label=f'B: {name_b}')
    ]
    
    leg = ax.legend(
        handles=legend_elements, loc='lower right', bbox_to_anchor=(0.98, 0.04),
        framealpha=1.0, edgecolor='#888888', facecolor='white', prop={'size': font_size * 0.9}
    )
    
    # Force recursive zorder sorting parameters to keep box above manual gridline segments
    if leg:
        leg.set_zorder(200)
    
    # Tight subplots adjusting padding now that title block footprints are completely removed
    fig.subplots_adjust(left=0.15, right=0.85, bottom=0.08, top=0.95) 

    # Self-explaining output name construction
    out_path = Path(output_path)
    band_tag = "_" + "".join(str(b) for b in rgb_bands)   
    out_path = out_path.with_name(f"{out_path.stem}{band_tag}_RGB_{'_'.join(band_names)}{out_path.suffix}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Exporting publication mosaic image to: {out_path}")
    plt.savefig(out_path, dpi=dpi, facecolor='white', bbox_inches='tight', pad_inches=0.05)
    plt.close(fig)

    print("=" * 60)
    print("SUCCESS: Regional mosaic figure saved!")
    print("=" * 60)
    return fig, ax

# =========================================================================
# 4. Pipeline Execution
# =========================================================================
if __name__ == "__main__":
    create_s1_false_color_composite(
        vrt_path=INPUT_VRT,
        boundary_shp=INPUT_SHP,
        output_path=OUTPUT_PNG_MOSAIC,
        downsample=10,  
        rgb_bands=RGB_BANDS,
        band_names=BAND_NAMES,
        vrt_percentiles=TILE_VRT_PERCENTILES,  
        gamma=GAMMA,
        grid_interval=GRID_INTERVAL,
        dpi=DPI,
        figsize=MOSAIC_FIGSIZE,
    )