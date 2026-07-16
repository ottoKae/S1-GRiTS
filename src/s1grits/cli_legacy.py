"""
Command-line entry module

Provides CLI interface for running S1 monthly mosaic multi-MGRS processor

v1.0 - Optimized output and logging system
"""

import argparse
import sys
import os
from pathlib import Path

# Lazy imports: Only import heavy modules when actually needed
# This makes --help and --version instant
from rich.console import Console
from rich.table import Table

# Initialize console with no fancy Unicode features for Windows compatibility
console = Console(legacy_windows=True, no_color=False)


def print_summary(results: dict):
    """
    Print processing results summary (using Rich for formatting)

    Args:
        results: Processing results dictionary
    """
    console.print("\n")
    console.rule("[bold blue]Processing Results Summary[/bold blue]", style="blue")

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
    detail_table = Table(title="\nDetailed Results", show_header=True, header_style="bold cyan")
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
        except Exception:
            return 0
        return total / (1024**3)  # Convert to GB

    for mgrs_id, result in sorted(results.items()):
        if result['status'] == 'success':
            status_icon = "[green]OK[/green]"
            months_count = str(len(result['written_months']))
            # Use new architecture tile_dir
            tile_dir = result.get('tile_dir', result.get('zarr_path', 'N/A'))
            path_info = tile_dir

            # Calculate file size
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
    console.rule(style="blue")


def main():
    """
    Main function
    """
    parser = argparse.ArgumentParser(
        description='S1-GRiTS: Sentinel-1 Gridded Time Series',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Usage Examples:
    # Run full workflow
    python main.py --config ../config/processing_config.yaml

    # Rebuild global catalog from existing tiles
    python main.py --rebuild-catalog --config ../config/processing_config.yaml
    python main.py --rebuild-catalog --output-dir ./output

Configuration File:
    See comments in ../config/processing_config.yaml for details
    ''')

    parser.add_argument(
        '--config',
        required=False,
        help='YAML configuration file path (required for workflow, optional for --rebuild-catalog)'
    )

    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 1.0.0'
    )

    parser.add_argument(
        '--rebuild-catalog',
        action='store_true',
        help='Rebuild global catalog.parquet from existing output tiles (skips processing)'
    )

    parser.add_argument(
        '--output-dir',
        default=None,
        help='Output directory path (only used with --rebuild-catalog)'
    )

    args = parser.parse_args()

    # Handle --rebuild-catalog mode
    if args.rebuild_catalog:
        from s1grits.rebuild_catalog import rebuild_catalog

        # Determine output directory
        if args.output_dir:
            output_dir = args.output_dir
        elif args.config:
            # Load config to get output directory
            config_path = Path(args.config)
            if not config_path.exists():
                console.print(f"[red]ERROR: Config file does not exist: {config_path}[/red]")
                sys.exit(1)
            import yaml
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            output_dir = config.get('output', {}).get('base_dir', './output')
        else:
            output_dir = './output'

        console.rule("[bold cyan]Rebuild Global Catalog[/bold cyan]", style="cyan")
        console.print(f"[dim]Output directory: {output_dir}[/dim]\n")

        success = rebuild_catalog(output_dir)
        sys.exit(0 if success else 1)

    # Check configuration file exists (for normal workflow mode)
    if not args.config:
        console.print("[red]ERROR: --config is required for workflow mode[/red]")
        console.print("[dim]Hint: Use --rebuild-catalog to rebuild catalog without running workflow[/dim]")
        sys.exit(1)

    config_path = Path(args.config)
    if not config_path.exists():
        console.print(f"[red]ERROR: Config file does not exist: {config_path}[/red]")
        sys.exit(1)

    try:
        # Lazy import: Only load heavy modules when running workflow
        import pandas as pd
        from s1grits.workflow import run_multi_mgrs_monthly_workflow
        from s1grits.logger_config import setup_logging, get_logger

        # Load configuration
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # Print banner
        console.rule("[bold cyan]S1-GRiTS: Sentinel-1 Gridded Time Series v1.0[/bold cyan]", style="cyan")
        console.print(f"[dim]Config file: {config_path}[/dim]\n")

        # Setup logging
        log_file, logger = setup_logging(config)
        console.print(f"[dim]Log file: {log_file}[/dim]\n")

        # Run workflow
        logger.info(f"Starting workflow: {config_path}")
        start_time = pd.Timestamp.now()

        with console.status("[bold green]Processing MGRS tiles...", spinner="dots"):
            results = run_multi_mgrs_monthly_workflow(config_path)

        end_time = pd.Timestamp.now()
        duration = end_time - start_time

        # Print summary
        print_summary(results)

        logger.info("Workflow completed")
        logger.info(f"Total time: {duration}")

        console.print(f"\nTotal time: [bold]{duration}[/bold]")

        # Set exit code based on results
        if all(r['status'] == 'success' for r in results.values()):
            console.print("\n[bold green]Workflow completed successfully![/bold green]\n")
            sys.exit(0)
        else:
            console.print("\n[bold yellow]WARNING: Some tasks failed[/bold yellow]\n")
            sys.exit(1)

    except KeyboardInterrupt:
        from s1grits.logger_config import get_logger
        console.print("\n\n[bold yellow]WARNING: User interrupted execution[/bold yellow]")
        get_logger().warning("User interrupted execution")
        sys.exit(130)

    except Exception as e:
        from s1grits.logger_config import get_logger
        console.print(f"\n\n[bold red]ERROR: Execution failed: {e}[/bold red]")
        get_logger().error(f"Execution failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
