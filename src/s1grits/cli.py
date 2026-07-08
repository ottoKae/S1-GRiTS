"""
CLI with subcommands structure

Provides a professional CLI interface with subcommands:
- s1grits process          --config config.yaml   (monthly composite workflow)
- s1grits process_scenes   --config config.yaml   (per-pass scenes + smonthly)
- s1grits process_static   --config config.yaml   (static geometry layers)
- s1grits catalog  resync   --output-dir ./output
- s1grits catalog  validate --output-dir ./output
- s1grits catalog  inspect  --output-dir ./output
- s1grits tile     inspect  --tile 50RKV --output-dir ./output
- s1grits tile     inspect  --tile 50RKV --direction ASCENDING --output-dir ./output
- s1grits mosaic   --month 2024-01 --direction ASCENDING
"""

import argparse
import os
import sys
from pathlib import Path
from rich.console import Console
from rich.table import Table
import rioxarray  # Register .rio accessor for xarray

from s1grits.__version__ import __version__
from s1grits.logger_config import get_logger

logger = get_logger(__name__)
console = Console(legacy_windows=True, no_color=False)


def print_summary(results: dict):
    """
    Print processing results summary (using Rich for formatting)

    Args:
        results: Processing results dictionary
    """
    console.print("\n")
    console.rule(f"[bold blue]Processing Results Summary[/bold blue]", style="blue")

    # Statistics
    success_count = sum(1 for r in results.values() if r['status'] == 'success')
    failed_count = sum(1 for r in results.values() if r['status'] == 'failed')

    # Create summary table
    summary_table = Table(title="Overall Statistics", show_header=True, header_style="bold magenta")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", justify="right", style="green")

    summary_table.add_row("Total MGRS Tiles", str(len(results)))
    summary_table.add_row("Success", f"[green]{success_count}[/green]")
    summary_table.add_row("Failed", f"[red]{failed_count}[/red]")
    if len(results) > 0:
        summary_table.add_row("Success Rate", f"{success_count/len(results)*100:.1f}%")

    console.print(summary_table)

    # Detailed results table
    detail_table = Table(title=f"\nDetailed Results", show_header=True, header_style="bold cyan")
    detail_table.add_column("MGRS Tile", style="cyan", width=12)
    detail_table.add_column("Status", justify="center", width=6)
    detail_table.add_column("Months", justify="right", width=7)
    detail_table.add_column("Size", justify="right", width=10)
    detail_table.add_column("Path/Error", style="dim")

    def get_dir_size(path):
        """Calculate total directory size (GB)"""
        import os
        total = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if os.path.exists(filepath):
                        total += os.path.getsize(filepath)
        except Exception as _e:
            logger.debug("Could not compute directory size for %s: %s", path, _e)
            return 0.0
        return total / (1024**3)  # Convert to GB

    for mgrs_id, result in sorted(results.items()):
        if result['status'] == 'success':
            status_icon = "[green]OK[/green]"
            months_count = str(len(result['written_months']))
            tile_dir = result.get('tile_dir', result.get('zarr_path', 'N/A'))
            path_info = tile_dir

            import os
            if tile_dir and tile_dir != 'N/A' and os.path.exists(tile_dir):
                size_gb = get_dir_size(tile_dir)
                size_str = f"{size_gb:.2f} GB" if size_gb >= 0.01 else f"{size_gb*1024:.1f} MB"
            else:
                size_str = "-"
        else:
            status_icon = "[red]FAIL[/red]"
            months_count = "-"
            size_str = "-"
            err_msg = str(result.get('error', 'Unknown error'))
            path_info = f"[red]{err_msg[:50]}...[/red]" if len(err_msg) > 50 else f"[red]{err_msg}[/red]"

        detail_table.add_row(mgrs_id, status_icon, months_count, size_str, path_info)

    console.print(detail_table)

    # Quality summary: tracks dropped by the coverage filter and acquisitions
    # skipped/flagged for missing bursts. Worker processes run quietly in
    # parallel mode, so this is where these events become visible (full detail
    # is in each tile's processing_report.json).
    _cov_rows, _inc_rows = [], []
    for _mid, _r in sorted(results.items()):
        for _d in (_r.get('dropped_tracks') or []):
            _cov_rows.append((_mid, _d.get('track_token', '?'), _d.get('coverage_frac')))
        for _a in (_r.get('incomplete_acquisitions') or []):
            _inc_rows.append((
                _mid, _a.get('track_token', '?'), _a.get('date', '?'),
                _a.get('loaded_bursts'),
                _a.get('footprint_bursts', _a.get('expected_bursts')),
                _a.get('cause', ''),
            ))
    if _cov_rows or _inc_rows:
        console.print(
            f"\n[yellow]Data quality:[/yellow] "
            f"{len(_cov_rows)} track(s) dropped (<coverage threshold), "
            f"{len(_inc_rows)} acquisition(s) incomplete (missing bursts). "
            f"See each tile's processing_report.json."
        )
        for _mid, _tok, _frac in _cov_rows[:20]:
            console.print(
                f"  [dim]drop  {_mid} TK{str(_tok).replace('_','-')}: "
                f"{(_frac or 0)*100:.1f}% of tile[/dim]"
            )
        # Group by (tile, track, cause): a systematic gap (e.g. ASF permanently
        # missing 2 bursts of a track for a year) is one operational fact, not
        # hundreds of console lines. Full per-acquisition detail stays in each
        # tile's processing_report.json.
        _grouped: dict[tuple, list] = {}
        for _mid, _tok, _dt, _ld, _ex, _cause in _inc_rows:
            _grouped.setdefault((_mid, _tok, _cause), []).append((_dt, _ld, _ex))
        for (_mid, _tok, _cause), _rows in sorted(_grouped.items()):
            _dates = sorted(str(r[0]) for r in _rows)
            _span = _dates[0] if len(_dates) == 1 else f"{_dates[0]} → {_dates[-1]}"
            _worst = min((r[1] or 0) for r in _rows)
            _exp = _rows[0][2]
            console.print(
                f"  [dim]incomplete {_mid} TK{str(_tok).replace('_','-')} "
                f"({_cause}): {len(_rows)} acquisition(s), {_span}, "
                f"worst {_worst}/{_exp} bursts[/dim]"
            )

    console.rule(style="blue")


def write_run_summary(
    results: dict,
    base_dir,
    *,
    duration_seconds: float | None = None,
    config_path: str | None = None,
):
    """Write a machine-readable run summary JSON next to the global catalog.

    One document per run (``run_summary.json``, overwritten): per-tile status,
    months written, error text, plus data-quality counters — the fields a
    pipeline needs to decide "retry / alert / proceed" without scraping the
    Rich console output. Returns the path, or None when it cannot be written
    (never fails the run over a summary file).
    """
    import json
    from datetime import datetime, timezone
    try:
        tiles = {}
        for mid, r in sorted(results.items()):
            tiles[mid] = {
                "status": r.get("status"),
                "months_written": sorted(r.get("written_months") or []),
                "n_months": len(r.get("written_months") or []),
                "scenes_written": r.get("written_scenes"),
                "tile_dir": r.get("tile_dir"),
                "error": r.get("error"),
                "dropped_tracks": r.get("dropped_tracks") or [],
                "n_incomplete_acquisitions": len(
                    r.get("incomplete_acquisitions") or []
                ),
            }
        doc = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "s1grits_version": __version__,
            "config_path": config_path,
            "duration_seconds": duration_seconds,
            "n_tiles": len(results),
            "n_success": sum(
                1 for r in results.values() if r.get("status") == "success"
            ),
            "n_failed": sum(
                1 for r in results.values() if r.get("status") == "failed"
            ),
            "tiles": tiles,
        }
        out = Path(base_dir) / "run_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return out
    except Exception as _e:
        logger.debug("Could not write run_summary.json: %s", _e)
        return None


def _add_output_flags(parser):
    """Register the shared output-control flags on a workflow subparser.

    Note: workflows NEVER write STAC (that is `catalog resync`'s job), so there
    is no per-workflow STAC flag — only data-format control.
    """
    parser.add_argument(
        '--zarr-only', dest='zarr_only', action='store_true',
        help='Produce only the Zarr time-series store: skip COG and PNG preview '
             'generation (overrides output.formats.cog/preview). Saves storage; '
             'the Zarr asset is self-describing for the ML pipeline.',
    )


def _workflow_overrides(args) -> dict | None:
    """Build a config-overrides dict from the shared output flags (or None)."""
    overrides: dict = {}
    if getattr(args, 'zarr_only', False):
        overrides.setdefault('output', {}).setdefault('formats', {}).update(
            {'cog': False, 'preview': False}
        )
        overrides.setdefault('processing', {}).setdefault('monthly', {}).update(
            {'generate_cog': False, 'generate_preview': False}
        )
    return overrides or None


def cmd_process(args):
    """Run the main processing workflow"""
    from s1grits.workflow import run_multi_mgrs_monthly_workflow
    from s1grits.logger_config import setup_logging, get_logger
    import pandas as pd
    import yaml

    config_path = Path(args.config)
    if not config_path.exists():
        console.print(f"[red]ERROR: Config file does not exist: {config_path}[/red]")
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    expected = config.get("workflow")
    if expected and expected != "monthly":
        console.print(
            f"[red]ERROR: Config workflow='{expected}' but CLI is 'process'. "
            f"Expected workflow='monthly'.[/red]"
        )
        sys.exit(1)

    console.rule("[bold cyan]S1-GRiTS: Sentinel-1 Gridded Time Series[/bold cyan]", style="cyan")
    console.print(f"[dim]Config: {config_path}[/dim]\n")

    log_file, logger = setup_logging(config)
    console.print(f"[dim]Log: {log_file}[/dim]\n")

    from s1grits.product_registry import load_product_registry
    _products = load_product_registry(workflow_config=config)
    logger.info("Product registry: %s", _products.config_path or "built-in defaults")
    logger.info("Starting workflow: %s", config_path)
    start_time = pd.Timestamp.now()

    console.print("[dim]Processing...[/dim]")
    results = run_multi_mgrs_monthly_workflow(config_path, overrides=_workflow_overrides(args))

    end_time = pd.Timestamp.now()
    duration = end_time - start_time

    print_summary(results)

    # Show output directory
    for r in results.values():
        if r.get('tile_dir'):
            out_root = str(Path(r['tile_dir']).parent)
            console.print(f"\n[bold]Output:[/bold] {out_root}")
            break

    logger.info("Workflow completed in %s", duration)
    console.print(f"\nTotal time: [bold]{duration}[/bold]")

    if all(r['status'] == 'success' for r in results.values()):
        console.print(f"\n[bold green]Success![/bold green]\n")
        sys.exit(0)
    else:
        console.print(f"\n[bold yellow]WARNING: Some tasks failed[/bold yellow]\n")
        sys.exit(1)


def cmd_process_static(args):
    """Run the static layer workflow (DEM, incidence angle map, layover/shadow mask)."""
    from s1grits.workflow_static import run_static_layer_workflow
    from s1grits.logger_config import setup_logging
    import pandas as pd
    import yaml

    config_path = Path(args.config)
    if not config_path.exists():
        console.print(f"[red]ERROR: Config file does not exist: {config_path}[/red]")
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    expected = config.get("workflow")
    if expected and expected != "static":
        console.print(
            f"[red]ERROR: Config workflow='{expected}' but CLI is 'process_static'. "
            f"Expected workflow='static'.[/red]"
        )
        sys.exit(1)

    console.rule("[bold cyan]S1-GRiTS: Static Layer Workflow[/bold cyan]", style="cyan")
    console.print(f"[dim]Config: {config_path}[/dim]\n")

    log_file, logger_inst = setup_logging(config)
    console.print(f"[dim]Log: {log_file}[/dim]\n")

    from s1grits.product_registry import load_product_registry
    _products = load_product_registry(workflow_config=config)
    logger_inst.info("Product registry: %s", _products.config_path or "built-in defaults")
    start_time = pd.Timestamp.now()
    results = run_static_layer_workflow(config_path, overrides=_workflow_overrides(args))
    duration = pd.Timestamp.now() - start_time

    console.print(f"\nTotal time: [bold]{duration}[/bold]")

    for tile_id, r in sorted(results.items()):
        if r['status'] in ('success', 'skipped'):
            groups = r.get('groups_written', [])
            for g in groups:
                layers_ok = [k for k, v in g.get('layers_written', {}).items() if v]
                tag = "[dim]SKIP[/dim]" if g['status'] == 'skipped' else "[green]OK  [/green]"
                console.print(
                    f"  {tag} {tile_id} {g['name_prefix']}: "
                    f"{layers_ok}"
                )
        else:
            console.print(f"  [red]FAIL[/red] {tile_id}: {r['error']}")

    n_ok = sum(1 for r in results.values() if r['status'] in ('success', 'skipped'))
    if n_ok == len(results):
        console.print(f"\n[bold green]Success![/bold green]\n")
        sys.exit(0)
    else:
        console.print(f"\n[bold yellow]WARNING: Some tasks failed[/bold yellow]\n")
        sys.exit(1)

def cmd_catalog_validate(args):
    """Validate catalog schema integrity and STAC Item alignment"""
    from s1grits.analysis import validate_catalog
    import pandas as pd

    output_dir = Path(args.output_dir)
    catalog_path = output_dir / 'catalog.parquet'

    console.rule("[bold cyan]Validate Catalog[/bold cyan]", style="cyan")
    console.print(f"[dim]Catalog: {catalog_path}[/dim]\n")

    result = validate_catalog(catalog_path)

    # --- STAC alignment check (CLI layer) ---
    if catalog_path.exists():
        try:
            df = pd.read_parquet(catalog_path)
            stac_missing = []
            for _, row in df.iterrows():
                tile_id = row['mgrs_tile_id']
                direction = row.get('flight_direction', '') or ''
                month = str(row['month'])
                suffix = f"_{direction}" if direction else ""
                item_id = f"{tile_id}{suffix}_{month}"
                item_path = output_dir / f"{tile_id}{suffix}" / f"{item_id}.json"
                if not item_path.exists():
                    stac_missing.append(item_id)

            if stac_missing:
                result.setdefault('warnings', [])
                result['warnings'].append(
                    f"{len(stac_missing)} STAC Item JSON(s) missing on disk — "
                    "run 's1grits catalog resync'."
                )
                for item_id in stac_missing[:5]:
                    result['warnings'].append(f"  Missing: {item_id}.json")
                if len(stac_missing) > 5:
                    result['warnings'].append(f"  ... and {len(stac_missing) - 5} more")
        except Exception as _e:
            logger.debug("STAC alignment check skipped: %s", _e)

    # --- Display results ---
    if result['valid']:
        console.print(f"[green]INFO   Catalog schema is valid[/green]")
        console.print(f"[dim]       Records: {result.get('record_count', '?')}[/dim]")
    else:
        issues = result.get('issues', [])
        first_issue = issues[0] if issues else "Unknown validation failure"
        console.print(f"[red]ERROR  {first_issue}[/red]")
        for issue in issues[1:]:
            console.print(f"[red]       {issue}[/red]")

    warnings = result.get('warnings', [])
    for warning in warnings:
        console.print(f"[yellow]WARN   {warning}[/yellow]")

    sys.exit(0 if result['valid'] else 1)


def cmd_catalog_doctor(args):
    """Validate catalog consistency: catalog.parquet ↔ STAC items ↔ Zarr attrs."""
    from s1grits.catalog_validator import validate_catalog_integrity

    console.rule("[bold cyan]Catalog Doctor[/bold cyan]", style="cyan")
    console.print(f"[dim]Checking: {args.output_dir}[/dim]\n")

    result = validate_catalog_integrity(args.output_dir, strict=args.strict)
    console.print(result.report())

    if result.valid:
        console.print("\n[bold green]All checks passed![/bold green]")
        sys.exit(0)
    else:
        console.print(f"\n[bold red]{len(result.errors)} error(s) found[/bold red]")
        sys.exit(1)


def cmd_catalog_resync(args):
    """Resync catalog + STAC from existing filesystem data."""
    from s1grits.analysis.catalog import resync_catalog_from_filesystem

    output_dir = args.output_dir
    write_stac = getattr(args, 'write_stac', True)
    stac_format = getattr(args, 'stac_format', 'geoparquet')
    console.rule("[bold cyan]Catalog Resync[/bold cyan]", style="cyan")
    console.print(f"[dim]Scanning: {output_dir}[/dim]")
    _mode = (
        f'catalog + STAC ({stac_format})' if write_stac
        else 'catalog-only (STAC removed)'
    )
    console.print(f"[dim]Mode: {_mode}[/dim]\n")

    result = resync_catalog_from_filesystem(
        output_dir, write_stac=write_stac, stac_format=stac_format,
    )

    if result['success']:
        _counts = result.get('collection_counts') or {}
        if _counts:
            for _cid, _n in _counts.items():
                console.print(f"  [cyan]{_cid}[/cyan]: {_n} item(s)")
        console.print(f"[green]OK[/green] {result['message']}")
        _tiles = result.get('tiles', [])
        console.print(f"  [dim]tiles: {', '.join(_tiles)}[/dim]")
        sys.exit(0)
    else:
        console.print(f"[red]FAIL[/red] {result['message']}")
        sys.exit(1)


def cmd_catalog_inspect(args):
    """Show global coverage summary across all tiles and directions"""
    from s1grits.analysis.reporting import generate_coverage_report

    output_dir = args.output_dir

    console.rule("[bold cyan]Catalog Coverage[/bold cyan]", style="cyan")
    console.print(f"[dim]Output directory: {output_dir}[/dim]\n")

    result = generate_coverage_report(output_dir)

    if not result['success']:
        console.print(f"[red]ERROR  {result['message']}[/red]")
        sys.exit(1)

    # Overall summary
    overall = result['overall']
    console.print(f"Total records:  {overall['total_records']}")
    console.print(f"MGRS tiles:     {overall['tile_count']}")
    console.print(f"Date range:     {overall['date_range'][0]} to {overall['date_range'][1]}")
    console.print(f"Total months:   {overall['total_months']}")
    if 'directions' in overall:
        console.print(f"Directions:     {', '.join(str(d) for d in overall['directions'])}")

    # Coverage table
    table = Table(title="\nCoverage by Tile", show_header=True, header_style="bold cyan")
    table.add_column("Tile",      style="cyan", width=10)
    table.add_column("Direction",              width=12)
    table.add_column("Months",    justify="right", width=7)
    table.add_column("Expected",  justify="right", width=9)
    table.add_column("Missing",   justify="right", width=8)
    table.add_column("Complete",  justify="right", width=9)
    table.add_column("Range",                  width=18)

    for tile in result['tiles']:
        completeness = tile['completeness']
        color = "green" if completeness == 100.0 else ("yellow" if completeness >= 80.0 else "red")
        direction = str(tile.get('direction') or '-')
        table.add_row(
            tile['tile_id'],
            direction,
            str(tile['months']),
            str(tile['expected_months']),
            str(tile['missing_months']),
            f"[{color}]{completeness:.1f}%[/{color}]",
            f"{tile['start_date']} ~ {tile['end_date']}",
        )

    console.print(table)

    gaps = result['gaps']
    if gaps['tiles_with_gaps'] > 0:
        console.print(
            f"\n[yellow]WARN   {gaps['tiles_with_gaps']}/{gaps['total_tiles']} "
            f"tile-direction(s) have temporal gaps[/yellow]"
        )
    else:
        console.print(
            f"\n[green]INFO   All {gaps['total_tiles']} tile-direction(s) are complete[/green]"
        )

    sys.exit(0)


def cmd_tile_inspect(args):
    """Show detailed temporal completeness for a single MGRS tile"""
    import pandas as pd
    from s1grits.analysis.reporting import analyze_temporal_gaps

    output_dir = Path(args.output_dir)
    tile_id = args.tile
    filter_direction = args.direction.upper() if getattr(args, 'direction', None) else None

    catalog_path = output_dir / 'catalog.parquet'
    if not catalog_path.exists():
        console.print(f"[red]ERROR  Catalog not found: {catalog_path}[/red]")
        console.print(f"[dim]       Run: s1grits catalog resync --output-dir {output_dir}[/dim]")
        sys.exit(1)

    catalog = pd.read_parquet(catalog_path)
    tile_data = catalog[catalog['mgrs_tile_id'] == tile_id]

    if len(tile_data) == 0:
        console.print(f"[red]ERROR  No data found for tile {tile_id}[/red]")
        # Show available tiles as hint
        available = sorted(catalog['mgrs_tile_id'].unique().tolist())
        console.print(f"[dim]       Available tiles: {', '.join(available)}[/dim]")
        sys.exit(1)

    # Filter by direction if --direction was specified
    direction_col = 'flight_direction' if 'flight_direction' in tile_data.columns else None
    if filter_direction and direction_col:
        tile_data = tile_data[tile_data[direction_col] == filter_direction]
        if len(tile_data) == 0:
            available_dirs = sorted(catalog[catalog['mgrs_tile_id'] == tile_id][direction_col].dropna().unique().tolist())
            console.print(f"[red]ERROR  No data found for tile {tile_id} direction {filter_direction}[/red]")
            console.print(f"[dim]       Available directions for this tile: {', '.join(available_dirs)}[/dim]")
            sys.exit(1)

    title = f"Tile: {tile_id}" + (f"  |  {filter_direction}" if filter_direction else "")

    directions = sorted(tile_data[direction_col].dropna().unique()) if direction_col else [None]

    # Collect all output lines first, then print once to avoid Rich re-render artifacts
    lines = []
    sep = "─" * 60
    lines.append(f"\n{sep} {title} {sep}")

    for direction in directions:
        if direction is not None:
            lines.append(f"\n{direction}")

        gaps = analyze_temporal_gaps(tile_data, tile_id=tile_id, direction=direction)

        completeness = gaps['completeness']

        lines.append(f"  Present months:  {gaps['present_months']}")
        lines.append(f"  Expected months: {gaps['total_months']}")
        lines.append(f"  Date range:      {gaps['date_range'][0]} ~ {gaps['date_range'][1]}")
        lines.append(f"  Completeness:    {completeness:.1f}%")

        if gaps['missing_list']:
            lines.append(f"\n  Missing months ({len(gaps['missing_list'])}):")
            for month_str in gaps['missing_list']:
                if direction:
                    cog_path = (
                        output_dir / f"{tile_id}_{direction}" / "cog"
                        / f"{tile_id}_S1_Monthly_{direction}_{month_str}.tif"
                    )
                    if cog_path.exists():
                        lines.append(
                            f"    - {month_str}"
                            f"  (COG exists but missing from catalog -- run resync)"
                        )
                    else:
                        lines.append(f"    - {month_str}  (no source data)")
                else:
                    lines.append(f"    - {month_str}")
        else:
            lines.append("\n  No missing months -- complete time series")

    lines.append(sep)
    print("\n".join(lines))
    sys.exit(0)


def _import_gdal():
    """Import osgeo.gdal with an actionable error when it is missing.

    rasterio bundles libgdal but NOT the ``osgeo`` python bindings, and pip
    has no reliable osgeo wheels — conda-forge is the supported install path.
    """
    try:
        from osgeo import gdal
        return gdal
    except ImportError as exc:
        console.print(
            "[red]ERROR: the 'osgeo' GDAL python bindings are not installed. "
            "The mosaic/mosaic_scenes commands need them for VRT/COG "
            "building.\n"
            "Install via conda-forge (pip has no osgeo wheels):\n"
            "    conda install -c conda-forge gdal\n"
            "or create the full environment: conda env create -f "
            "environment.yml[/red]"
        )
        raise SystemExit(1) from exc


def cmd_mosaic(args):
    """Create a multi-tile mosaic VRT or COG for a given month"""
    from s1grits.analysis import create_mosaic_vrt, find_cog_files_for_mosaic

    output_dir = Path(args.output_dir).resolve()

    console.rule("[bold cyan]Create Mosaic[/bold cyan]", style="cyan")
    console.print(f"[dim]Month: {args.month}, Direction: {args.direction}[/dim]\n")

    try:
        cog_files = find_cog_files_for_mosaic(
            month=args.month,
            direction=args.direction,
            output_root=str(output_dir),
            mgrs_prefix=args.mgrs_prefix,
        )

        if not cog_files:
            console.print("[red]ERROR  No COG files found[/red]")
            console.print(f"[yellow]WARN   Searched in: {output_dir}[/yellow]")
            sys.exit(1)

        console.print(f"[dim]Found {len(cog_files)} COG file(s)[/dim]")

        # Resolve output directory for mosaic files
        if args.output:
            mosaic_output_dir = Path(args.output)
        else:
            mosaic_output_dir = Path("analysis_results") / "mosaic"
        mosaic_output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve target CRS: --keep-utm overrides --crs
        target_crs = None if args.keep_utm else args.crs

        allow_mixed = (args.direction == "ALL")

        result_path = create_mosaic_vrt(
            cog_files,
            output_dir=str(mosaic_output_dir),
            output_format=args.format,
            target_crs=target_crs,
            allow_mixed_directions=allow_mixed,
        )

        if not result_path:
            console.print("[red]ERROR  Failed to create mosaic[/red]")
            sys.exit(1)

        console.print(f"[green]INFO   Mosaic created: {result_path}[/green]")
        sys.exit(0)

    except Exception as e:
        console.print(f"[red]ERROR  {e}[/red]")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def cmd_doctor(args):
    """Environment + config health check; exit 0 iff no hard failure."""
    from s1grits.doctor import run_doctor, format_results

    exit_code, results = run_doctor(
        config_path=args.config, network=args.network
    )
    print(format_results(results))
    sys.exit(exit_code)


def cmd_process_scenes(args):
    """Run the scenes workflow (per-pass outputs + optional monthly)."""
    from s1grits.workflow_scenes import run_scenes_workflow
    from s1grits.logger_config import setup_logging
    import pandas as pd
    import yaml

    config_path = Path(args.config)
    if not config_path.exists():
        console.print(
            f"[red]ERROR: Config file does not exist: {config_path}[/red]"
        )
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    expected = config.get("workflow")
    if expected and expected != "scenes":
        console.print(
            f"[red]ERROR: Config workflow='{expected}' but CLI is 'process_scenes'. "
            f"Expected workflow='scenes'.[/red]"
        )
        sys.exit(1)

    processing_cfg = config.get('processing', {})
    monthly_enabled = processing_cfg.get('monthly', {}).get('enabled', False)

    console.rule(
        "[bold cyan]S1-GRiTS: Scenes Workflow[/bold cyan]", style="cyan"
    )
    console.print(f"[dim]Config:              {config_path}[/dim]")
    console.print(f"[dim]Monthly:             {monthly_enabled}[/dim]")
    console.print(f"[dim]Output:              {config.get('output', {}).get('base_dir', 'unknown')}[/dim]")
    console.print(f"[dim]Product dirs:        scenes_{{despeckle}}_{{bands}}/[zarr|cog|preview][/dim]\n")

    log_file, logger = setup_logging(config)
    console.print(f"[dim]Log: {log_file}[/dim]\n")

    from s1grits.product_registry import load_product_registry
    _products = load_product_registry(workflow_config=config)
    logger.info(
        "Starting scenes workflow: %s (monthly=%s), products=%s",
        config_path, monthly_enabled,
        _products.config_path or "built-in defaults",
    )
    start_time = pd.Timestamp.now()

    results = run_scenes_workflow(config_path, overrides=_workflow_overrides(args))

    duration = pd.Timestamp.now() - start_time
    print_summary(results)

    for r in results.values():
        if r.get('tile_dir'):
            out_root = str(Path(r['tile_dir']).parent)
            console.print(f"\n[bold]Output:[/bold] {out_root}")
            break

    # Machine-readable run summary for pipeline integration/monitoring.
    _summary_path = write_run_summary(
        results, config.get('output', {}).get('base_dir', '.'),
        duration_seconds=duration.total_seconds(),
        config_path=str(config_path),
    )
    if _summary_path:
        console.print(f"[dim]Run summary: {_summary_path}[/dim]")

    logger.info("Scenes workflow completed in %s", duration)
    console.print(f"\nTotal time: [bold]{duration}[/bold]")

    if all(r['status'] == 'success' for r in results.values()):
        console.print(f"\n[bold green]Success![/bold green]\n")
        sys.exit(0)
    else:
        console.print(
            f"\n[bold yellow]WARNING: Some tasks failed[/bold yellow]\n"
        )
        sys.exit(1)


def cmd_mosaic_scenes(args):
    """Create a multi-tile mosaic VRT or COG for per-pass scenes on a given date."""
    import pandas as pd
    from pathlib import Path

    output_dir = Path(args.output_dir).resolve()
    catalog_path = output_dir / 'catalog.parquet'

    console.rule(
        "[bold cyan]Create Scenes Mosaic[/bold cyan]", style="cyan"
    )

    if not catalog_path.exists():
        console.print(
            f"[red]ERROR: Catalog not found: {catalog_path}[/red]"
        )
        sys.exit(1)

    try:
        df = pd.read_parquet(catalog_path)
    except Exception as e:
        console.print(f"[red]ERROR: Failed to read catalog: {e}[/red]")
        sys.exit(1)

    # Filter to scenes only
    if 'output_type' in df.columns:
        df = df[df['output_type'] == 'scenes']
    console.print(f"[dim]Loaded {len(df)} scenes records[/dim]")

    # Filter by direction
    direction = args.direction.upper()
    if direction != 'ALL':
        df = df[df['flight_direction'].str.upper() == direction]
        console.print(f"[dim]Filtered to direction {direction}: {len(df)} records[/dim]")

    # Filter by date or date range (catalog uses 'datetime' column)
    if 'datetime' not in df.columns:
        console.print("[red]ERROR: Catalog missing 'datetime' column[/red]")
        sys.exit(1)

    df['_date'] = pd.to_datetime(df['datetime'], utc=True, errors='coerce')
    df = df.dropna(subset=['_date'])

    if args.date:
        target_date = pd.Timestamp(args.date).tz_localize('UTC')
        df = df[df['_date'].dt.floor('min') == target_date.floor('min')]
        console.print(
            f"[dim]Filtered to date {args.date}: {len(df)} records[/dim]"
        )
    elif args.start or args.end:
        if args.start:
            start_ts = pd.Timestamp(args.start).tz_localize('UTC')
            df = df[df['_date'] >= start_ts]
        if args.end:
            end_ts = pd.Timestamp(args.end).tz_localize('UTC')
            df = df[df['_date'] <= end_ts]
        console.print(
            f"[dim]Filtered to date range: {len(df)} records[/dim]"
        )
    else:
        console.print(
            "[yellow]WARN: No --date, --start, or --end specified. "
            "Showing all dates.[/yellow]"
        )

    if df.empty:
        console.print("[red]ERROR: No scenes match the filter criteria[/red]")
        sys.exit(1)

    # Group by datetime (minute-floored per-pass)
    df['_acq_grp'] = df['_date'].dt.floor('min')
    groups = df.groupby('_acq_grp')

    console.print(
        f"[dim]{len(groups)} unique acquisition timestamp(s) found[/dim]"
    )

    # Determine output directory
    if args.output:
        mosaic_output_dir = Path(args.output)
    else:
        mosaic_output_dir = Path("analysis_results") / "mosaic_scenes"
    mosaic_output_dir.mkdir(parents=True, exist_ok=True)

    from s1grits.logger_config import get_logger
    _logger = get_logger(__name__)

    for acq_grp, grp_df in groups:
        acq_str = acq_grp.strftime('%Y%m%dT%H%M%S')

        # Collect COG paths that exist on disk
        cog_files = []
        for _, row in grp_df.iterrows():
            cog_rel = row.get('cog_path')
            if cog_rel and not pd.isna(cog_rel):
                tile_id = row['mgrs_tile_id']
                direction_str = row.get('flight_direction', '')
                tile_dir = output_dir / f"{tile_id}_{direction_str}"
                cog_full = tile_dir / cog_rel
                if cog_full.exists():
                    cog_files.append(str(cog_full.resolve()))
                else:
                    _logger.warning(
                        "[WARN] COG not found: %s", cog_full
                    )

        if not cog_files:
            console.print(
                f"[yellow]WARN: No COG files on disk for {acq_str}, "
                f"skipping[/yellow]"
            )
            continue

        # Output filename
        if args.out_file and len(groups) == 1:
            out_path = Path(args.out_file)
        else:
            ext = 'vrt' if args.format == 'vrt' else 'tif'
            out_path = mosaic_output_dir / (
                f"{direction if direction != 'ALL' else 'DualDir'}"
                f"_{acq_str}.{ext}"
            )

        if args.format == 'cog':
            # Full reproject to target CRS
            gdal = _import_gdal()
            ref_crs = args.crs if hasattr(args, 'crs') else 'EPSG:4326'
            vrt_path = str(out_path.with_suffix('.vrt'))
            gdal.BuildVRT(vrt_path, cog_files)
            gdal.Translate(
                str(out_path), vrt_path,
                format='COG', resampleAlg='bilinear',
                dstSRS=ref_crs,
            )
            console.print(
                f"[green]INFO   COG created: {out_path}[/green]"
            )
        else:
            # VRT (fast, no reprojection)
            gdal = _import_gdal()
            gdal.BuildVRT(str(out_path), cog_files)
            console.print(
                f"[green]INFO   VRT created: {out_path}[/green]"
            )

    console.print(f"\n[bold green]Done.[/bold green]")
    sys.exit(0)


def cmd_serve(args):
    """Run the v2.3 web UI + API over a workspace directory."""
    try:
        from s1grits.webapp.server import serve
    except ImportError as exc:
        console.print(
            "[red]The web UI requires the optional 'web' extra:[/red] "
            "pip install 's1grits\\[web]'"
        )
        raise SystemExit(1) from exc
    serve(
        root=args.root, host=args.host, port=args.port, token=args.token,
        max_concurrent_jobs=args.max_concurrent_jobs, insecure=args.insecure,
    )


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        prog='s1grits',
        description='S1-GRiTS: Sentinel-1 Grid Time Series Processor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''\
Examples:
  # Run processing workflows (three products)
  s1grits process          --config config/s1grits_monthly.yaml   # monthly composites
  s1grits process_scenes   --config config/s1grits_scenes.yaml    # per-pass scenes + smonthly
  s1grits process_static   --config config/s1grits_static.yaml    # static geometry layers

  # Catalog management (resync rebuilds catalog.parquet + STAC from disk)
  s1grits catalog resync   --output-dir ./output
  s1grits catalog validate --output-dir ./output
  s1grits catalog inspect  --output-dir ./output

  # Single tile temporal completeness
  s1grits tile inspect --tile 50RKV --output-dir ./output

  # Create multi-tile mosaic (default: EPSG:4326, VRT format)
  s1grits mosaic --month 2024-01 --direction ASCENDING
  s1grits mosaic --month 2024-01 --direction ASCENDING --crs EPSG:3857
  s1grits mosaic --month 2024-01 --direction ASCENDING --keep-utm
  s1grits mosaic --month 2024-01 --direction ASCENDING --format COG
  s1grits mosaic --month 2024-01 --direction ALL
  s1grits mosaic --month 2024-01 --direction ASCENDING --mgrs-prefix 50R

  # Create per-pass scenes mosaic
  s1grits mosaic_scenes --output-dir ./output_scenes_hARDCp --direction ASCENDING --date 2024-03-15
  s1grits mosaic_scenes --output-dir ./output_scenes_hARDCp --direction ASCENDING --start 2024-01 --end 2024-03
        '''
    )

    parser.add_argument(
        '--version', action='version', version=f's1grits {__version__}'
    )

    # metavar hides argparse's auto-generated "{cmd1,cmd2,...}" brace list, which
    # would otherwise expose SUPPRESS-hidden aliases (e.g. process_monthly). The
    # per-command help lines below still list every non-suppressed command.
    subparsers = parser.add_subparsers(
        dest='command', metavar='<command>', help='Available commands'
    )

    # ── process ──────────────────────────────────────────────────────────────
    # `process_monthly` is a back-compat alias: argparse accepts it but does not
    # list it as a separate command, so the production help stays minimal while
    # existing scripts/docs that call `process_monthly` keep working.
    parser_process = subparsers.add_parser(
        'process',
        aliases=['process_monthly'],
        help='Run the monthly composite workflow from a YAML config'
    )
    parser_process.add_argument('--config', required=True, help='Path to YAML config file')
    _add_output_flags(parser_process)
    parser_process.set_defaults(func=cmd_process)

    # ── process_static ───────────────────────────────────────────────────────
    parser_static = subparsers.add_parser(
        'process_static',
        help='Generate static layers (DEM, incidence angle, layover/shadow mask) per MGRS tile'
    )
    parser_static.add_argument('--config', required=True, help='Path to YAML config file')
    _add_output_flags(parser_static)
    parser_static.set_defaults(func=cmd_process_static)

    # ── catalog ───────────────────────────────────────────────────────────────
    parser_catalog = subparsers.add_parser('catalog', help='Catalog management')
    catalog_sub = parser_catalog.add_subparsers(dest='catalog_cmd', help='Catalog operations')

    # catalog validate
    p = catalog_sub.add_parser(
        'validate',
        help='Validate catalog schema and check STAC Item alignment'
    )
    p.add_argument('--output-dir', required=True, help='Output root directory')
    p.set_defaults(func=cmd_catalog_validate)

    # catalog resync
    p = catalog_sub.add_parser(
        'resync',
        help='Resync catalog + STAC from filesystem (no re-processing)'
    )
    p.add_argument('--output-dir', required=True, help='Output root directory')
    p.add_argument(
        '--stac', dest='write_stac', default=True,
        action=argparse.BooleanOptionalAction,
        help='Rebuild the STAC tree (default). Use --no-stac to reconcile only '
             'catalog.parquet and delete all STAC artifacts (catalog-only).',
    )
    p.add_argument(
        '--stac-format', dest='stac_format', choices=['geoparquet', 'json'],
        default='geoparquet',
        help='STAC item serialization: geoparquet (default; one parquet per '
             'collection, zero per-item files, DuckDB/pyarrow queryable) or '
             'json (classic one-file-per-item static catalog).',
    )
    p.set_defaults(func=cmd_catalog_resync)

    # catalog doctor
    p = catalog_sub.add_parser(
        'doctor',
        help='Validate catalog.parquet + STAC items + Zarr attrs for consistency'
    )
    p.add_argument('--output-dir', required=True, help='Output root directory')
    p.add_argument('--strict', action='store_true', help='Treat warnings as errors')
    p.set_defaults(func=cmd_catalog_doctor)

    # catalog inspect
    p = catalog_sub.add_parser(
        'inspect',
        help='Show global coverage summary (completeness per tile and direction)'
    )
    p.add_argument('--output-dir', required=True, help='Output root directory')
    p.set_defaults(func=cmd_catalog_inspect)

    # ── tile ──────────────────────────────────────────────────────────────────
    parser_tile = subparsers.add_parser('tile', help='Tile-level operations')
    tile_sub = parser_tile.add_subparsers(dest='tile_cmd', help='Tile operations')

    # tile inspect
    p = tile_sub.add_parser(
        'inspect',
        help='Show detailed temporal completeness for a single MGRS tile'
    )
    p.add_argument('--tile', required=True, help='MGRS tile ID (e.g., 50RKV)')
    p.add_argument('--direction', required=False, default=None,
                   choices=['ASCENDING', 'DESCENDING', 'ascending', 'descending'],
                   help='Filter by orbit direction (optional, shows all directions if omitted)')
    p.add_argument('--output-dir', required=True, help='Output root directory')
    p.set_defaults(func=cmd_tile_inspect)

    # ── mosaic ────────────────────────────────────────────────────────────────
    parser_mosaic = subparsers.add_parser(
        'mosaic',
        help='Create a multi-tile mosaic VRT or COG for a given month'
    )
    parser_mosaic.add_argument('--month', required=True, help='Month to mosaic (YYYY-MM)')
    parser_mosaic.add_argument(
        '--direction', required=True,
        choices=['ASCENDING', 'DESCENDING', 'ALL'],
        help=(
            'Flight direction. '
            'ALL: ASCENDING has pixel-level priority; DESCENDING fills NoData gaps.'
        )
    )
    parser_mosaic.add_argument(
        '--output-dir', default='./output',
        help='Source output directory containing tile subdirectories (default: ./output)'
    )
    parser_mosaic.add_argument(
        '--output',
        help='Destination directory for mosaic output (default: analysis_results/mosaic/)'
    )
    parser_mosaic.add_argument(
        '--format', choices=['VRT', 'COG'], default='VRT',
        help='Output format (default: VRT)'
    )
    parser_mosaic.add_argument(
        '--crs', default='EPSG:4326',
        help='Target CRS for reprojection (default: EPSG:4326). Ignored when --keep-utm is set.'
    )
    parser_mosaic.add_argument(
        '--keep-utm', action='store_true',
        help='Keep original per-tile UTM projection; skip reprojection'
    )
    parser_mosaic.add_argument(
        '--mgrs-prefix',
        help='Filter tiles by MGRS prefix (e.g., 50R includes 50RKU, 50RKV, …)'
    )
    parser_mosaic.set_defaults(func=cmd_mosaic)

    # ── doctor ────────────────────────────────────────────────────────────────
    parser_doctor = subparsers.add_parser(
        'doctor',
        help='Check environment, config, disk, and resource plan before a run'
    )
    parser_doctor.add_argument(
        '--config', default=None,
        help='YAML config to validate (schema, policies, paths, workers)'
    )
    parser_doctor.add_argument(
        '--network', action='store_true',
        help='Also check ASF/CMR reachability (needs internet)'
    )
    parser_doctor.set_defaults(func=cmd_doctor)

    # ── process_scenes ────────────────────────────────────────────────────────
    parser_scenes = subparsers.add_parser(
        'process_scenes',
        help='Scenes workflow: per-pass outputs with optional monthly compositing'
    )
    parser_scenes.add_argument(
        '--config', required=True, help='Path to YAML config file'
    )
    _add_output_flags(parser_scenes)
    parser_scenes.set_defaults(func=cmd_process_scenes)

    # ── mosaic_scenes ─────────────────────────────────────────────────────────
    parser_mosaic_scenes = subparsers.add_parser(
        'mosaic_scenes',
        help='Create multi-tile mosaic VRT or COG for per-pass scenes on a given date'
    )
    parser_mosaic_scenes.add_argument(
        '--output-dir', required=True,
        help='Output root directory containing catalog.parquet'
    )
    parser_mosaic_scenes.add_argument(
        '--direction', required=True,
        choices=['ASCENDING', 'DESCENDING', 'ALL'],
        help='Flight direction. ALL includes both directions.',
    )
    parser_mosaic_scenes.add_argument(
        '--date', help='Single date YYYY-MM-DD'
    )
    parser_mosaic_scenes.add_argument(
        '--start', help='Start date YYYY-MM-DD (for date range)'
    )
    parser_mosaic_scenes.add_argument(
        '--end', help='End date YYYY-MM-DD (for date range)'
    )
    parser_mosaic_scenes.add_argument(
        '--format', choices=['vrt', 'cog'], default='vrt',
        help='Output format (default: vrt)'
    )
    parser_mosaic_scenes.add_argument(
        '--crs', default='EPSG:4326',
        help='Target CRS for COG output (default: EPSG:4326)'
    )
    parser_mosaic_scenes.add_argument(
        '--out-file', help='Output file path (auto-named if omitted)'
    )
    parser_mosaic_scenes.add_argument(
        '--output',
        help='Destination directory for mosaic output (default: analysis_results/mosaic_scenes/)'
    )
    parser_mosaic_scenes.set_defaults(func=cmd_mosaic_scenes)

    # ── serve (v2.3 web UI) ───────────────────────────────────────────────────
    parser_serve = subparsers.add_parser(
        'serve',
        help='Run the web UI + API over a workspace (requires s1grits[web])'
    )
    parser_serve.add_argument(
        '--root', required=True,
        help='Workspace directory (the output.base_dir of your runs)'
    )
    parser_serve.add_argument(
        '--host', default='127.0.0.1',
        help='Bind address (default 127.0.0.1; non-local requires --token)'
    )
    parser_serve.add_argument('--port', type=int, default=8765)
    parser_serve.add_argument(
        '--token', default=os.environ.get('S1GRITS_WEB_TOKEN') or None,
        help='Bearer token required on /api (default: $S1GRITS_WEB_TOKEN)'
    )
    parser_serve.add_argument(
        '--max-concurrent-jobs', type=int, default=1,
        help='Concurrent pipeline jobs (default 1; runs parallelise internally)'
    )
    parser_serve.add_argument(
        '--insecure', action='store_true',
        help='Allow non-localhost bind without a token (not recommended)'
    )
    parser_serve.set_defaults(func=cmd_serve)

    # ── dispatch ──────────────────────────────────────────────────────────────
    args = parser.parse_args()

    # Keep the console clean: silence noisy third-party logging up front. The
    # process_* commands additionally call setup_logging (file + concise
    # console); the catalog/tile/mosaic commands rely on this quieting + their
    # own rich console output.
    from s1grits.logger_config import quiet_noisy_loggers
    quiet_noisy_loggers()

    if not hasattr(args, 'func'):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print(f"\n[yellow]WARNING: Interrupted by user[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]ERROR: {e}[/red]")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
