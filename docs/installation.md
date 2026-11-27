[← Back to README](../README.md)

## Quick Start

Install S1-GRiTS, create a configuration, and validate it before a first run.
For an existing Cube, start with the [small local tutorial](getting_started_existing_cube_ZH.md).

This checkout describes the V4 development candidate. A PyPI installation or a
clone of `main` does not install unpublished changes; use the reviewed candidate
checkout or wheel when testing V4. See [migration and compatibility](migration_v4.md).

### Prerequisites

- **Python:** 3.12 (`>=3.12,<3.13`)
- **RAM:** >= 16 GB (64 GB+ recommended for large regions)
- **OS:** Windows, Linux or macOS
- **Network:** Access to ASF (asf.alaska.edu) and AWS S3

### Step 1: Installation

Use a dedicated Python 3.12 environment. S1-GRiTS itself is pure Python and ships
a universal wheel; its geospatial dependencies contain native libraries.

#### Option A: Install the published package from PyPI

```bash
# 1. Create dedicated Python 3.12 environment
conda create -n s1grits python=3.12
conda activate s1grits

# 2. Install geospatial core libraries via conda-forge (optional but recommended on Windows)
conda install -c conda-forge rasterio geopandas rioxarray pyproj shapely

# 3. Install s1grits from PyPI, including the bilingual web console
pip install "s1grits[web]"
```

The base-only form (`pip install s1grits`) remains valid for headless CLI and
Python API users. Use the `web` extra when `s1grits serve` is required.

#### Option B: Install from source (developers)

```bash
# Clone repository
git clone https://github.com/ottoKae/S1-GRiTS.git
cd S1-GRiTS

# Create conda environment
conda install -n base conda-libmamba-solver
conda env create -f environment.yml --solver=libmamba
conda activate py312_s1grits

# Install this checkout, including the optional web console
pip install -e ".[web]"
```

If you already have the reviewed V4 checkout, start from that directory and skip
the clone step. Confirm the active installation with `python -m s1grits --version`
and `python -c "import s1grits; print(s1grits.__file__)"`.

#### Optional: GRiTS-Resolver for existing-Cube access

GRiTS-Resolver is installed separately for the Catalog/Resolver-based analysis
and spatial readers. The compatible candidate used by this checkout's CI is
`0.3.1rc1`, pinned to a public source commit:

```bash
python -m pip install "grits-resolver @ git+https://github.com/ottoKae/GRiTS-Resolver.git@9a92523d8cdbf20ec0b3b377c2265875a6521d73"
```

Install it in the same environment as S1-GRiTS. See [spatial products](spatial-products.md)
and the [entry-point support matrix](entry_points.md) for the relevant workflows.

#### Optional: Jupyter notebook support

```bash
pip install "s1grits[notebook]"
python -m ipykernel install --user --name py312_s1grits --display-name "Python (s1grits)"
jupyter lab
```

#### Bilingual web console

```bash
pip install "s1grits[web]"
mkdir s1grits-output
s1grits serve --root s1grits-output --port 5556  # see docs/webapp.md
```

The public **S1-GRiTS Data Cube Console / S1-GRiTS 数据立方体控制台** supports
switching between English and Chinese on the same page. See the [Web guide](webapp.md).

#### Install all extras

```bash
pip install "s1grits[all]"
```

This extra covers the dependencies listed in `pyproject.toml`; it does not install
GRiTS-Resolver or the separately documented temporal backends. GDAL Python bindings
(`osgeo`) are also required for [grid mosaics](grid_mosaic.md); the repository's
Conda environment supplies them. Rasterio's bundled GDAL does not supply `osgeo`.

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

PyPI users should generate the built-in starter template; unlike a repository
path such as `config/s1grits_scenes.yaml`, this command is available from the
installed wheel:

```bash
s1grits init my_run.yaml
```

Choose a workflow and edit the corresponding config file:

**Per-Scene Processing:** `config/s1grits_scenes.yaml` (enable `processing.monthly` for composites)
**Static Layers:** enable `static_layers.run_after_scenes` in the scenes YAML.

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
s1grits doctor --config my_run.yaml            # fast, offline
s1grits doctor --config my_run.yaml --network  # + ASF reachability
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
s1grits process_scenes --config my_run.yaml

# Static layers workflow
s1grits static ensure --output-dir ./output --product-label smonthly_ASCENDING
```

**What happens:**
- ASF metadata query for ROI and time range
- S3 streaming download (no local zip files)
- Multi-burst mosaic per MGRS tile
- Optional spatial speckle filtering and monthly compositing
- Feature extraction (Ratio, RVI, optional GLCM)
- Zarr data cube + COG + preview generation
- Hierarchical metadata and Parquet catalog creation; STAC export is an explicit
  `s1grits catalog resync` step, described in [workflows](workflows.md)

---
