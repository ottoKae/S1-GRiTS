[← Back to README](../README.md)

## Quick Start

Get your first S1-GRiTS output in **5 minutes**.

### Prerequisites

- **Python:** >= 3.12
- **RAM:** >= 16 GB (64 GB+ recommended for large regions)
- **OS:** Windows or Linux
- **Network:** Access to ASF (asf.alaska.edu) and AWS S3

### Step 1: Installation

> **Important:** Do not install in conda base environment. S1-GRiTS requires Python 3.12 and compiled extensions. Always create a dedicated environment first.

#### Option A: pip install from PyPI (recommended)

```bash
# 1. Create dedicated Python 3.12 environment
conda create -n s1grits python=3.12
conda activate s1grits

# 2. Install geospatial core libraries via conda-forge (optional but recommended on Windows)
conda install -c conda-forge rasterio geopandas rioxarray pyproj shapely

# 3. Install s1grits from PyPI
pip install s1grits
```

#### Option B: Install from source (developers)

```bash
# Clone repository
git clone https://github.com/ottoKae/S1-GRiTS.git
cd S1-GRiTS

# Create conda environment
conda install -n base conda-libmamba-solver
conda env create -f environment.yml --solver=libmamba
conda activate py312_s1grits

# Install package
pip install .
```

#### Optional: Jupyter notebook support

```bash
pip install "s1grits[notebook]"
python -m ipykernel install --user --name py312_s1grits --display-name "Python (s1grits)"
jupyter lab
```

#### Optional: web interface

```bash
pip install "s1grits[web]"
s1grits serve --root <workspace>  # see docs/webapp.md
```

#### Install all extras

```bash
pip install "s1grits[all]"
```

### Step 2: Earthdata Authentication (optional)

**The production workflows do not require credentials**: OPERA RTC-S1 burst
metadata (CMR) and downloads (ASF CDN) are public, and `s1grits doctor`
reports credential status accordingly. Set up `.netrc` only if you extend
the pipeline to restricted ASF datasets.

**2.1 Register Account**

1. Register at https://urs.earthdata.nasa.gov
2. Authorize ASF DAAC at https://urs.earthdata.nasa.gov/profile (Applications > Alaska Satellite Facility Data Access)

**2.2 Create `.netrc` File**

**Linux/macOS:** `~/.netrc`
**Windows:** `%USERPROFILE%\.netrc`

```
machine urs.earthdata.nasa.gov
  login YOUR_USERNAME
  password YOUR_PASSWORD
```

**2.3 Enable in Python**

```python
import asf_search as asf
session = asf.ASFSession()
session.trust_env = True  # Allow reading .netrc
```

### Step 3: Configure

Choose a workflow and edit the corresponding config file:

**Per-Scene Processing:** `config/s1grits_scenes.yaml` (enable `processing.monthly` for composites)
**Static Layers:** `config/s1grits_static.yaml`

**Minimal config example** (scenes workflow):

```yaml
roi:
  # Option A: WKT polygon (auto-detect tiles)
  wkt: "POLYGON((113.587 30.0001,114.8881 30.0001,114.8881 30.9441,113.587 30.9441,113.587 30.0001))"
  
  # Option B: Manual tile list (faster)
  # manual_mgrs_tiles:
  #   - "50RKV"
  #   - "50RLV"
  
  flight_direction: "ASCENDING"   # ASCENDING | DESCENDING
  polarization: "VV+VH"           # VV+VH | HH+HV

time:
  years: [2024]
  months: [1, 2, 3]   # optional; omit for full year

output:
  base_dir: "./output"
```

### Step 3.5: Validate before long runs — `s1grits doctor`

```bash
s1grits doctor --config config/s1grits_scenes.yaml            # fast, offline
s1grits doctor --config config/s1grits_scenes.yaml --network  # + ASF reachability
```

`doctor` finds in seconds the problems that would otherwise kill a multi-hour
run midway: broken geospatial imports (rasterio/GDAL, zarr, cv2; missing
`osgeo` is a warning with a conda-forge hint), invalid or misplaced config
keys and output policies (including deprecated v2 keys), unwritable output /
burst-cache directories, insufficient disk space (per the `preflight.disk`
policy), tiles whose existing Zarr stores sit on inconsistent grids, and a
RAM/CPU sanity check against the resolved worker counts. Exit code 0 means no
hard failures (warnings don't fail). Run it after editing a config and before
every long or production run.

### Step 4: Run

```bash
# Per-scene workflow (+ monthly composites via processing.monthly)
s1grits process_scenes --config config/s1grits_scenes.yaml

# Static layers workflow
s1grits process_static --config config/s1grits_static.yaml
```

**What happens:**
- ASF metadata query for ROI and time range
- S3 streaming download (no local zip files)
- Multi-burst mosaic per MGRS tile
- Speckle reduction (temporal + optional spatial)
- Feature extraction (Ratio, RVI, optional GLCM)
- Zarr data cube + COG + preview generation
- STAC catalog + Parquet index creation

---
