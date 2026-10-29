"""Full S1 workflow tests with real COG decoding, monthly stores and Catalogs."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import Affine
import yaml
import zarr

from s1grits import asf_io, workflow
from s1grits import workflow_scenes as scenes
from s1grits.atomic_write import atomic_write_parquet
from s1grits.config_schema import find_unknown_config_keys
from s1grits.provenance_contract import read_tables
from s1grits.scenes.smonthly_writer import _write_smonthly_one_track
from s1grits.stac_builder import set_stac_output_enabled

TILE = "17MPU"
TRANSFORM = Affine(30, 0, 500010, 0, -30, 8500020)
CRS = "EPSG:32717"


def _snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and path.name != ".s1grits.lock"}


def _stores(root):
    return {path.name: path for path in root.glob("*/smonthly_*/zarr/*.zarr")}


def _pixel_snapshot(root):
    return {name: _snapshot(path) for name, path in _stores(root).items()}


def _tables(root):
    pointer = json.loads((root / "provenance/current.json").read_text(encoding="utf-8"))
    return pointer, read_tables(root / "provenance/releases" / pointer["metadata_release_id"])


def _seed(root):
    """Create a genuine tiny historical store whose locked grid can be adopted."""
    set_stac_output_enabled(False)
    record = _write_smonthly_one_track(
        mgrs_tile_id=TILE, direction_label="ASCENDING", tile_dir=root / TILE,
        final_vv=[np.full((3, 4), 2, dtype="f4")],
        prof_vv=[{"crs": CRS, "transform": TRANSFORM, "nodata": np.nan}],
        final_vh=[np.ones((3, 4), dtype="f4")],
        prof_vh=[{"crs": CRS, "transform": TRANSFORM, "nodata": np.nan}],
        clean_dates=[pd.Timestamp("2024-12-15T00:00:00Z")], target_crs=CRS,
        target_res=30.0, generate_cog=False, generate_preview=False,
        chunk_y=2, chunk_x=2, cog_block=16, on_time_conflict="skip",
        monthly_cfg={"composite_method": "nanmedian"}, processing_level="ARDC",
        transform=TRANSFORM, width=4, height=3,
        x_coords=TRANSFORM.c + (np.arange(4) + 0.5) * 30,
        y_coords=TRANSFORM.f - (np.arange(3) + 0.5) * 30,
        tile_clip=False, track_token="18",
    )
    frame = pd.DataFrame(record)
    atomic_write_parquet(frame, root / TILE / "catalog.parquet")
    atomic_write_parquet(frame, root / "catalog.parquet")


class Case:
    def __init__(self, directory, monkeypatch):
        self.directory = directory
        self.root = directory / "tracked"
        _seed(self.root)
        self.calls = []
        self.queries = []
        self.payloads = {}
        self.missing = set()
        self.rows = []
        source_dir = directory / "source-cogs"
        source_dir.mkdir()
        for month in (1, 2):
            # Two same-time bursts retain independent source identities.
            for track, burst, day in ((18, 1, 5), (18, 2, 5), (19, 3, 15)):
                product = f"fixture-{month}-{track}-{burst}"
                row = {
                    "mgrs_tile_id": TILE, "opera_id": product,
                    "jpl_burst_id": f"B{burst:03d}", "track_token": str(track),
                    "track_number": track, "pass_id": track,
                    "acq_group_id_within_mgrs_tile": month * 100 + track,
                    "orbit_pass": "ASCENDING",
                    "acq_dt": pd.Timestamp(f"2025-{month:02d}-{day:02d}T00:00:00Z"),
                }
                for band, key, scale in (("VV", "url_copol", 1), ("VH", "url_crosspol", 0.5)):
                    url = f"https://example.invalid/rtc/{product}_{band}.tif"
                    row[key] = url
                    array = (np.arange(1, 13, dtype="f4").reshape(3, 4) + month + burst) * scale
                    path = source_dir / url.rsplit("/", 1)[1]
                    with rasterio.open(path, "w", driver="COG", height=3, width=4,
                                       count=1, dtype="float32", crs=CRS,
                                       transform=TRANSFORM, nodata=np.nan,
                                       compress="DEFLATE", blocksize=128) as raster:
                        raster.write(array, 1)
                    self.payloads[url] = path.read_bytes()
                self.rows.append(row)
        self.config = {
            "metadata": {"provenance": {"enabled": True, "dataset_id": "tiny-workflow",
                                         "run_id": "batch-initial"}},
            "roi": {"manual_mgrs_tiles": [TILE], "flight_direction": "ASCENDING",
                    "polarization": "VV+VH", "min_tile_coverage_frac": 0},
            "time": {"years": [2025], "months": [1, 2]},
            "output": {"base_dir": str(self.root), "existing_store": "resume",
                       "existing_month": "skip", "formats": {"cog": False, "preview": False}},
            "processing": {"target_resolution": 30, "tile_clip": False,
                           "spatial_despeckle": False, "features_ratio": False,
                           "features_rvi": False, "features_glcm": False,
                           "zarr_chunks": {"y": 2, "x": 2},
                           "monthly": {"enabled": True, "only": True,
                                       "composite_method": "nanmedian", "blockwise_threads": 1}},
            "parallel": {"enabled": False, "max_workers": 1},
            "memory": {"batch_strategy": "monthly", "max_download_workers": 1,
                       "download_prefetch": False, "batch_max_retries": 0,
                       "scene_max_retries": 0, "scene_retry_timeout_seconds": 0},
            "query": {"max_retries": 0, "footprint_lookback_months": 0},
            "preflight": {"disk": {"mode": "off"}}, "runtime": {"enabled": False},
            "static_layers": {"enabled": False},
        }
        monkeypatch.setattr(workflow, "get_rtc_s1_ts_metadata_from_mgrs_tiles", self.query)
        monkeypatch.setattr(asf_io, "_download_to_bytes", self.download)

    def query(self, tiles, **kwargs):
        self.queries.append((tuple(tiles), kwargs["start_acq_dt"], kwargs["stop_acq_dt"]))
        start = pd.Timestamp(kwargs["start_acq_dt"], tz="UTC")
        stop = pd.Timestamp(kwargs["stop_acq_dt"], tz="UTC") + pd.Timedelta(days=1)
        return pd.DataFrame([row for row in self.rows
                             if start <= row["acq_dt"] < stop]).copy()

    def download(self, url, **kwargs):
        self.calls.append(url)
        if url in self.missing:
            raise FileNotFoundError("fixture HTTP 404")
        return self.payloads[url]

    def run(self, config=None):
        path = self.directory / "workflow.yaml"
        path.write_text(yaml.safe_dump(config or self.config, sort_keys=False), encoding="utf-8")
        return scenes.run_scenes_workflow(path)


@pytest.fixture
def case(tmp_path_factory, monkeypatch):
    value = Case(tmp_path_factory.mktemp("wf"), monkeypatch)
    yield value
    set_stac_output_enabled(False)


def _assert_no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A committed workflow restart must not query or download again")
    monkeypatch.setattr(workflow, "get_rtc_s1_ts_metadata_from_mgrs_tiles", forbidden)
    monkeypatch.setattr(asf_io, "_download_to_bytes", forbidden)


def _forbid_image_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A committed workflow restart must not rewrite Zarr arrays")
    monkeypatch.setattr(zarr.Array, "__setitem__", forbidden)
    monkeypatch.setattr(zarr.Array, "resize", forbidden)


def test_real_yaml_multiple_months_tracks_and_native_numerical_parity(case):
    assert find_unknown_config_keys(case.config) == []
    result = case.run()
    assert result[TILE]["status"] == "success"
    pointer, (outputs, sources, links) = _tables(case.root)
    produced = [row for row in outputs if row["generation_id"]]
    assert len(produced) == 4
    assert {row["period_start"] for row in produced} == {"2025-01-01", "2025-02-01"}
    assert {row["geometry_group_id"] for row in produced} == {
        TILE + "_ASCENDING_TK18", TILE + "_ASCENDING_TK19",
    }
    for path in (case.root / "catalog.parquet", case.root / TILE / "catalog.parquet"):
        catalog = pd.read_parquet(path)
        new = catalog[catalog["month"].isin(["2025-01", "2025-02"])]
        assert len(new) == 4
        assert set(new["geometry_group_id"]) == {row["geometry_group_id"] for row in produced}
    source_map = {row["source_id"]: row["href"] for row in sources}
    used = {source_map[row["source_id"]] for row in links if row["role"] == "used"}
    assert used == set(case.payloads)
    assert pointer["data_revision"]
    release = case.root / "provenance/releases" / pointer["metadata_release_id"]
    for row in produced:
        run = case.root / "provenance/runs" / row["run_id"]
        receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
        intent = json.loads((run / "intent.json").read_text(encoding="utf-8"))
        assert "operator_steps" not in intent["request"]
        numeric = receipt["numeric_processing"]
        assert numeric["input"]["actual_domain"] is None
        assert numeric["input"]["operator_expected_domain"] == "linear_power"
        assert numeric["steps"]
        assert receipt["generation_id"] == row["generation_id"]
        saved = json.loads((release / row["evidence_ref"]).read_text(encoding="utf-8"))
        assert saved["numeric_processing"] == numeric

    native_root = case.directory / "native"
    _seed(native_root)
    native_config = deepcopy(case.config)
    native_config["metadata"]["provenance"]["enabled"] = False
    native_config["output"]["base_dir"] = str(native_root)
    assert case.run(native_config)[TILE]["status"] == "success"
    assert set(_stores(case.root)) == set(_stores(native_root))
    for name, path in _stores(case.root).items():
        tracked = zarr.open_group(str(path), mode="r", use_consolidated=False)
        native = zarr.open_group(str(_stores(native_root)[name]), mode="r", use_consolidated=False)
        for variable in ("VV_dB", "VH_dB", "n_obs", "time", "x", "y"):
            np.testing.assert_array_equal(tracked[variable][:], native[variable][:])


def test_same_time_bursts_keep_source_binding_after_http_404(case):
    missing_row = dict(case.rows[0])
    missing_row["opera_id"] = "fixture-missing"
    missing_row["jpl_burst_id"] = "B000"
    for band, key in (("VV", "url_copol"), ("VH", "url_crosspol")):
        missing_row[key] = f"https://example.invalid/rtc/fixture-missing_{band}.tif"
        case.missing.add(missing_row[key])
    case.rows.insert(0, missing_row)
    case.config["time"]["months"] = [1]
    assert case.run()[TILE]["status"] == "success"
    _, (_, sources, links) = _tables(case.root)
    href = {row["source_id"]: row["href"] for row in sources}
    used = {href[row["source_id"]] for row in links if row["role"] == "used"}
    expected = {url for row in case.rows if row["opera_id"] != "fixture-missing"
                and row["acq_dt"].month == 1
                for url in (row["url_copol"], row["url_crosspol"])}
    assert used == expected
    assert not used & case.missing


def test_same_month_replacement_changes_only_target_generations(case):
    case.run()
    _, (before, _, _) = _tables(case.root)
    previous = {row["output_id"]: row for row in before}
    prior_feb = {}
    for name, path in _stores(case.root).items():
        group = zarr.open_group(str(path), mode="r", use_consolidated=False)
        feb = list(pd.to_datetime(group["time"][:]).strftime("%Y-%m")).index("2025-02")
        prior_feb[name] = {band: group[band][feb].copy() for band in ("VV_dB", "VH_dB", "n_obs")}
    case.config["metadata"]["provenance"]["run_id"] = "batch-replace-jan"
    case.config["time"]["months"] = [1]
    case.config["output"]["existing_month"] = "overwrite"
    case.run()
    _, (after, _, _) = _tables(case.root)
    assert {row["output_id"] for row in after} == set(previous)
    for row in after:
        old = previous[row["output_id"]]
        if row["period_start"] == "2025-01-01":
            assert row["generation_id"] != old["generation_id"]
        else:
            assert row["generation_id"] == old["generation_id"]
    for name, path in _stores(case.root).items():
        group = zarr.open_group(str(path), mode="r", use_consolidated=False)
        feb = list(pd.to_datetime(group["time"][:]).strftime("%Y-%m")).index("2025-02")
        for band, values in prior_feb[name].items():
            np.testing.assert_array_equal(group[band][feb], values)


def test_completed_workflow_restart_never_queries_downloads_or_rewrites(case, monkeypatch):
    case.run()
    before = _snapshot(case.root)
    _assert_no_network(monkeypatch)
    _forbid_image_writes(monkeypatch)
    assert case.run()[TILE]["status"] == "success"
    assert _snapshot(case.root) == before


def test_catalog_failure_recovers_committed_outputs_without_network(case, monkeypatch):
    from s1grits import provenance_catalog
    actual = provenance_catalog._install_catalog
    global_installs = 0
    failed = False
    def fail_final_global(root, target, entry):
        nonlocal global_installs, failed
        if target == "catalog.parquet":
            global_installs += 1
            if global_installs == 4:
                failed = True
                raise OSError("interruption before global Catalog publication")
        return actual(root, target, entry)
    monkeypatch.setattr(provenance_catalog, "_install_catalog", fail_final_global)
    with pytest.raises((OSError, RuntimeError), match="Catalog|catalog"):
        case.run()
    assert failed
    receipt_before = {path.relative_to(case.root).as_posix(): path.read_bytes()
                      for path in (case.root / "provenance/runs").glob("*/receipt.json")}
    assert len(receipt_before) == 4
    assert all(json.loads(payload)["numeric_processing"]["steps"] for payload in receipt_before.values())
    before = _pixel_snapshot(case.root)
    monkeypatch.setattr(provenance_catalog, "_install_catalog", actual)
    _assert_no_network(monkeypatch)
    _forbid_image_writes(monkeypatch)
    assert case.run()[TILE]["status"] == "success"
    assert _pixel_snapshot(case.root) == before
    for relative, payload in receipt_before.items():
        assert (case.root / relative).read_bytes() == payload
    _, (outputs, _, _) = _tables(case.root)
    assert len([row for row in outputs if row["generation_id"]]) == 4
    assert not list((case.root / "provenance/pending").glob("*.json"))


@pytest.mark.parametrize("unsupported", ["parallel", "rebuild", "static"])
def test_unsupported_tracking_modes_reject_before_network_and_image_changes(case, monkeypatch,
                                                                         unsupported):
    if unsupported == "parallel":
        case.config["parallel"]["enabled"] = True
    elif unsupported == "rebuild":
        case.config["output"]["existing_store"] = "rebuild-incompatible"
    else:
        case.config["static_layers"]["enabled"] = True
    before = _snapshot(case.root)
    _assert_no_network(monkeypatch)
    _forbid_image_writes(monkeypatch)
    with pytest.raises(ValueError):
        case.run()
    assert _snapshot(case.root) == before


def test_partial_second_store_write_blocks_restart_before_network(case, monkeypatch):
    from s1grits.scenes import smonthly_writer
    case.config["roi"]["manual_mgrs_tiles"] = [TILE, "17MPV"]
    actual = smonthly_writer._write_smonthly_month_zarr_blockwise
    writes = 0
    def interrupted_after_pixels(**kwargs):
        nonlocal writes
        result = actual(**kwargs)
        writes += 1
        if writes == 2:
            raise OSError("interrupted after second store pixels before receipt")
        return result
    monkeypatch.setattr(smonthly_writer, "_write_smonthly_month_zarr_blockwise",
                        interrupted_after_pixels)
    with pytest.raises(RuntimeError, match="second store pixels before receipt"):
        case.run()
    assert writes == 2
    assert {tiles for tiles, _, _ in case.queries} == {(TILE,)}
    _, (outputs, _, _) = _tables(case.root)
    assert len([row for row in outputs if row["generation_id"]]) == 1
    pending = list((case.root / "provenance/pending").glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text(encoding="utf-8"))["phase"] == "writing"
    before = _snapshot(case.root)
    monkeypatch.setattr(smonthly_writer, "_write_smonthly_month_zarr_blockwise", actual)
    _assert_no_network(monkeypatch)
    _forbid_image_writes(monkeypatch)
    with pytest.raises((ValueError, RuntimeError), match="receipt|ambiguous"):
        case.run()
    assert _snapshot(case.root) == before


def test_source_declarations_and_raw_read_steps_survive_delivery(case):
    from rasterio.io import MemoryFile
    from shutil import copytree
    resolver = pytest.importorskip("grits_resolver.provenance")

    url = case.rows[0]["url_copol"]
    with MemoryFile(case.payloads[url]) as original:
        with original.open() as source:
            values = source.read(1)
            profile = source.profile
    with MemoryFile() as memory:
        with memory.open(**profile) as source:
            source.write(values, 1)
            source.set_band_unit(1, "1")
            source.scales = (2.0,)
            source.offsets = (3.0,)
            source.update_tags(PRODUCT_VERSION="fixture-v1")
            source.update_tags(1, DATA_DOMAIN="linear_power")
        case.payloads[url] = memory.read()
    config = deepcopy(case.config)
    config["time"]["months"] = [1]
    assert case.run(config)[TILE]["status"] == "success"
    with MemoryFile(case.payloads[case.rows[1]["url_copol"]]) as memory:
        with memory.open() as source:
            second_values = source.read(1)
    output = next(row for row in _tables(case.root)[1][0]
                  if row["generation_id"] and row["geometry_group_id"].endswith("TK18"))
    group = zarr.open_group(str(case.root / output["store_path"]), mode="r")
    np.testing.assert_allclose(group["VV_dB"][output["time_index"]],
                               10 * np.log10((values + second_values) / 2), rtol=1e-6)
    view = resolver.ProvenanceView(case.root, required=True)
    records = view.explain()["numeric_processing"]
    observed = [source for result in records if result["record"]
                for source in result["record"]["input"]["sources"]
                if source.get("asset", {}).get("tags", {}).get("dataset", {}).get("product_version") == "fixture-v1"]
    assert len(observed) == 1
    record = observed[0]
    assert record["asset"]["unit"] == "1"
    assert record["asset"]["scale"] == 2.0 and record["asset"]["offset"] == 3.0
    assert record["read"]["scale_offset_applied"] is False
    assert record["profile_nodata"] == {"nonfinite": "nan"}
    assert all(result["record"]["input"]["actual_domain"] is None for result in records if result["record"])
    inputs = [item for item in _tables(case.root)[1][1] if item["source_id"] == record["source_id"]]
    assert len(inputs) == 1 and inputs[0]["href"] == url
    delivery = case.directory / "delivery"
    copytree(case.root, delivery)
    case.root.rename(case.directory / "original-unavailable")
    assert resolver.ProvenanceView(delivery, required=True).explain()["numeric_processing"] == records
