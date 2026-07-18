"""
Jupyter Notebook utilities for running s1grits CLI

Provides functions to run CLI commands in notebooks with proper formatting
and real-time output streaming that mimics terminal behavior.

Key Features:
- Automatic project root detection
- Real-time streaming output for long-running tasks
- Proper Windows path and encoding handling
- Unified configuration path resolution
- All commands run from project root directory
"""

import subprocess
import sys
import os
import shlex
import time
from collections import deque
from pathlib import Path
from typing import Optional, List, Union

try:
    from IPython.display import clear_output
    IPYTHON_AVAILABLE = True
except ImportError:
    IPYTHON_AVAILABLE = False


class CLIRunner:
    """
    Run s1grits CLI commands in Jupyter Notebook with terminal-like output

    Design Philosophy:
    - Notebook environments prioritize REAL-TIME STREAMING over ANSI colors
    - All commands run from project root for consistent path resolution
    - Windows-safe path handling with proper encoding
    - CLI already handles logging, so wrapper focuses on display

    Features:
    - Auto-detects project root directory
    - Real-time output streaming (default for long tasks)
    - Proper Windows encoding (UTF-8, unbuffered)
    - Safe command parsing (handles spaces in paths)
    - Unified config path resolution
    """

    def __init__(
        self,
        project_root: Optional[Union[str, Path]] = None,
        max_display_lines: int = 30,
        refresh_rate: float = 0.5,
        enable_filter: bool = True,
        filter_keywords: Optional[List[str]] = None
    ):
        """
        Initialize CLI runner with project root detection

        Args:
            project_root: Project root directory. If None, auto-detects by looking for:
                         - pyproject.toml
                         - setup.py
                         - src/s1grits directory
            max_display_lines: Maximum number of lines to show in notebook display (default: 30)
            refresh_rate: UI refresh interval in seconds (default: 0.5)
            enable_filter: Enable line filtering to show only important lines (default: True)
            filter_keywords: Custom keywords for filtering. If None, uses default keywords.

        The runner will:
        - Set cwd to project_root for all commands
        - Auto-locate config/processing_config.yaml
        - Ensure consistent path resolution
        - Buffer output for better notebook display
        """
        self.project_root = self._find_project_root(project_root)
        self.config_dir = self.project_root / "config"
        self.default_config = self.config_dir / "processing_config.yaml"

        # Buffered output configuration
        self.max_display_lines = max_display_lines
        self.refresh_rate = refresh_rate
        self.enable_filter = enable_filter

        # Default filter keywords (log levels, progress indicators, status)
        self.filter_keywords = filter_keywords or [
            'INFO', 'WARN', 'WARNING', 'ERROR', 'CRITICAL', 'DEBUG',
            '%', 'Tile', 'month', 'Processing', 'Completed', 'Failed',
            'SUCCESS', 'FAILED', 'SKIPPED', 'progress', 'done', 'finished'
        ]

        # Validate project structure
        if not (self.project_root / "src" / "s1grits").exists():
            raise RuntimeError(
                f"Invalid project root: {self.project_root}\n"
                f"Expected to find src/s1grits directory"
            )

    def _find_project_root(self, provided_root: Optional[Union[str, Path]] = None) -> Path:
        """
        Find project root directory

        Search order:
        1. Provided root (if given)
        2. Look for pyproject.toml/setup.py in current dir and parents
        3. Look for src/s1grits in current dir and parents

        Args:
            provided_root: Explicitly provided project root

        Returns:
            Absolute path to project root

        Raises:
            RuntimeError: If project root cannot be determined
        """
        if provided_root:
            root = Path(provided_root).resolve()
            if root.exists():
                return root
            raise RuntimeError(f"Provided project root does not exist: {root}")

        # Start from current working directory
        current = Path.cwd()

        # Search up to 5 levels
        for _ in range(5):
            # Check for project markers
            if (current / "pyproject.toml").exists():
                return current
            if (current / "setup.py").exists():
                return current
            if (current / "src" / "s1grits").exists():
                return current

            # Move up one level
            parent = current.parent
            if parent == current:  # Reached filesystem root
                break
            current = parent

        raise RuntimeError(
            "Could not auto-detect project root. Please provide project_root explicitly:\n"
            "  runner = CLIRunner(project_root='/path/to/s1-monthly-mosaic-processor')"
        )

    def get_config_path(self, config_name: str = "processing_config.yaml") -> Path:
        """
        Resolve config file path

        Args:
            config_name: Config filename (default: processing_config.yaml)

        Returns:
            Absolute path to config file
        """
        config_path = self.config_dir / config_name
        if not config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}\n"
                f"Available configs in {self.config_dir}:\n" +
                "\n".join(f"  - {f.name}" for f in self.config_dir.glob("*.yaml"))
            )
        return config_path

    def run(self,
            command: Union[str, List[str]],
            stream: Optional[bool] = None,
            check: bool = False) -> subprocess.CompletedProcess:
        """
        Run a CLI command with proper output handling

        Args:
            command: Command string or list of arguments
                    - String: Will be safely parsed with shlex (handles spaces in paths)
                    - List: Used directly (recommended for paths with spaces)
            stream: Output mode:
                   - True: Real-time streaming (default for process/mosaic/export)
                   - False: Capture then display (default for help/info/validate)
                   - None: Auto-detect based on command
            check: If True, raise exception on non-zero exit code

        Returns:
            CompletedProcess object with returncode, stdout, stderr

        Examples:
            >>> runner = CLIRunner()
            >>> # Help/info commands (captured)
            >>> runner.run("s1grits --help")
            >>> runner.run(["s1grits", "catalog", "validate", "--output-dir", "./output"])
            >>>
            >>> # Long-running tasks (streamed)
            >>> runner.run("s1grits process_scenes --config config.yaml")
            >>> runner.run(["s1grits", "mosaic", "create", "--month", "2024-01"])
        """
        # Parse command safely
        if isinstance(command, str):
            # Use shlex for safe parsing (handles quotes, spaces, Windows paths)
            cmd_list = shlex.split(command, posix=False)  # posix=False for Windows
        else:
            cmd_list = list(command)

        # Convert 's1grits' to 'python -m s1grits' for cross-platform compatibility
        cmd_list = self._normalize_command(cmd_list)

        # Auto-detect streaming mode if not specified
        if stream is None:
            stream = self._should_stream(cmd_list)

        # Prepare environment
        env = self._prepare_environment()

        # Run command
        if stream:
            return self._run_streaming(cmd_list, env, check)
        else:
            return self._run_captured(cmd_list, env, check)

    def _normalize_command(self, cmd_list: List[str]) -> List[str]:
        """
        Normalize command for cross-platform compatibility

        Converts 's1grits' to 'python -m s1grits' on all platforms
        for consistent behavior.

        Args:
            cmd_list: Original command list

        Returns:
            Normalized command list
        """
        if cmd_list and cmd_list[0] == 's1grits':
            # Replace 's1grits' with 'python -m s1grits'
            return [sys.executable, '-m', 's1grits'] + cmd_list[1:]
        return cmd_list

    def _should_stream(self, cmd_list: List[str]) -> bool:
        """
        Determine if command should use streaming output

        Streaming (True) for:
        - process (long-running workflow)
        - mosaic create (multi-tile processing)
        - export (batch operations)

        Captured (False) for:
        - --help, --version (short info)
        - validate, inspect (quick checks)
        - catalog resync (unless very large)
        """
        # Check for help/version flags
        if any(arg in cmd_list for arg in ['--help', '-h', '--version', '-v']):
            return False

        # Check for long-running commands
        if len(cmd_list) >= 2:
            subcommand = cmd_list[1] if cmd_list[0] in ['s1grits', 'python'] else cmd_list[0]
            if subcommand in ['process', 'mosaic', 'export']:
                return True
            if subcommand == 'catalog' and 'resync' in cmd_list:
                return True

        # Default to captured for short commands
        return False

    def _prepare_environment(self) -> dict:
        """
        Prepare environment variables for subprocess

        Critical settings:
        - PYTHONUNBUFFERED=1: Disable output buffering (real-time streaming)
        - PYTHONIOENCODING=utf-8: Force UTF-8 encoding (avoid Windows GBK issues)
        - NO_COLOR=0: Allow color output (but don't force it)

        Note: We don't set FORCE_COLOR or TERM because:
        - Notebook is not a TTY, forcing terminal behavior can cause issues
        - Rich/Click will auto-detect and use appropriate output mode
        - CLI's --ui flag already controls UI behavior
        """
        env = os.environ.copy()

        # Critical: Unbuffered output for real-time streaming
        env['PYTHONUNBUFFERED'] = '1'

        # Critical: UTF-8 encoding to avoid Windows GBK/Unicode errors
        env['PYTHONIOENCODING'] = 'utf-8'

        # Allow colors but don't force (let CLI decide)
        env['NO_COLOR'] = '0'

        return env

    def _should_display_line(self, line: str, filter_keywords: List[str]) -> bool:
        """
        Determine if a line should be displayed based on filter keywords

        Args:
            line: The output line to check
            filter_keywords: List of keywords to match

        Returns:
            True if line should be displayed, False otherwise
        """
        if not self.enable_filter:
            return True

        line_upper = line.upper()
        return any(keyword.upper() in line_upper for keyword in filter_keywords)

    def _run_streaming(self,
                      cmd_list: List[str],
                      env: dict,
                      check: bool) -> subprocess.CompletedProcess:
        """
        Run command with real-time output streaming and buffered display

        This is the preferred mode for long-running tasks in notebooks.
        Uses a rolling window buffer to prevent output flooding and applies
        filtering to show only important lines.

        Features:
        - Buffered output: Only shows last N lines (configurable)
        - Rate limiting: Refreshes UI periodically (configurable)
        - Filtering: Shows only lines with important keywords
        - Full output preserved: All lines kept in return value
        """
        process = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            text=True,
            encoding='utf-8',  # Explicitly use UTF-8 (critical for Windows)
            errors='replace',  # Replace invalid characters instead of crashing
            bufsize=1,  # Line-buffered
            env=env,
            cwd=str(self.project_root)  # Run from project root
        )

        output_lines = []  # Full output (unfiltered) for return value

        # Use buffered display if max_display_lines is set and IPython is available
        use_buffered = self.max_display_lines is not None and IPYTHON_AVAILABLE

        if use_buffered:
            # Buffered display with rolling window
            display_buffer = deque(maxlen=self.max_display_lines)
            last_refresh = time.time()

            try:
                for line in process.stdout:
                    # Always keep full output (unfiltered)
                    output_lines.append(line)

                    # Filter and add to display buffer
                    if self._should_display_line(line, self.filter_keywords):
                        display_buffer.append(line)

                    # Rate-limited UI refresh
                    current_time = time.time()
                    if current_time - last_refresh >= self.refresh_rate:
                        clear_output(wait=True)
                        print(''.join(display_buffer), end='', flush=True)
                        last_refresh = current_time

            except KeyboardInterrupt:
                process.terminate()
                process.wait()
                raise

            # Final refresh after process completes
            clear_output(wait=True)
            print(''.join(display_buffer), end='', flush=True)

        else:
            # Fallback to immediate printing (legacy behavior)
            try:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    output_lines.append(line)
            except KeyboardInterrupt:
                process.terminate()
                process.wait()
                raise

        process.wait()

        # Create result object
        result = subprocess.CompletedProcess(
            args=cmd_list,
            returncode=process.returncode,
            stdout=''.join(output_lines),
            stderr=''
        )

        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd_list, result.stdout, result.stderr
            )

        return result

    def _run_captured(self,
                     cmd_list: List[str],
                     env: dict,
                     check: bool) -> subprocess.CompletedProcess:
        """
        Run command with captured output (display after completion)

        Used for short commands like --help, validate, inspect.
        Output is captured then displayed all at once.
        """
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            encoding='utf-8',  # Explicitly use UTF-8 (critical for Windows)
            errors='replace',  # Replace invalid characters instead of crashing
            env=env,
            cwd=str(self.project_root),  # Run from project root
            check=check
        )

        # Display output (plain text, no ANSI processing)
        if result.stdout:
            print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)

        return result


# Convenience functions for common commands

def s1grits(*args, **kwargs) -> subprocess.CompletedProcess:
    """
    Run s1grits CLI command

    Args:
        *args: Command arguments (e.g., 'catalog', 'resync', '--output-dir', './output')
        **kwargs: Additional arguments passed to CLIRunner.run()

    Returns:
        CompletedProcess object

    Examples:
        >>> s1grits('--help')
        >>> s1grits('catalog', 'resync', '--output-dir', './output')
        >>> s1grits('process', '--config', 'config.yaml')
    """
    runner = CLIRunner()
    cmd = ['s1grits'] + list(args)
    return runner.run(cmd, **kwargs)


def process_scenes(config_path: str, **kwargs) -> subprocess.CompletedProcess:
    """Run the scenes workflow ('s1grits process_scenes'). Produces per-scene
    products, plus monthly composites when ``processing.monthly`` is enabled."""
    return s1grits('process_scenes', '--config', config_path, **kwargs)


def process_static(config_path: str, **kwargs) -> subprocess.CompletedProcess:
    """Run the static-layer workflow ('s1grits process_static')."""
    return s1grits('process_static', '--config', config_path, **kwargs)


def catalog_resync(output_dir: str = './output', **kwargs) -> subprocess.CompletedProcess:
    """Resync catalog + STAC from the filesystem ('s1grits catalog resync')."""
    return s1grits('catalog', 'resync', '--output-dir', output_dir, **kwargs)


# Backward-compatible alias (the CLI subcommand is 'resync', not 'rebuild').
catalog_rebuild = catalog_resync


def catalog_validate(output_dir: str = './output', **kwargs) -> subprocess.CompletedProcess:
    """Validate catalog"""
    return s1grits('catalog', 'validate', '--output-dir', output_dir, **kwargs)


def catalog_inspect(tile: str, output_dir: str = './output', **kwargs) -> subprocess.CompletedProcess:
    """Inspect tile catalog completeness"""
    return s1grits('catalog', 'inspect', '--tile', tile, '--output-dir', output_dir, **kwargs)


def cog_validate(file_path: Optional[str] = None,
                 output_dir: Optional[str] = None,
                 sample: Optional[int] = None,
                 verbose: bool = False,
                 **kwargs) -> subprocess.CompletedProcess:
    """Validate COG files"""
    args = ['cog', 'validate']
    if file_path:
        args.extend(['--file', file_path])
    if output_dir:
        args.extend(['--output-dir', output_dir])
    if sample:
        args.extend(['--sample', str(sample)])
    if verbose:
        args.append('--verbose')
    return s1grits(*args, **kwargs)


def zarr_inspect(tile: str, direction: str, output_dir: str = './output', **kwargs) -> subprocess.CompletedProcess:
    """Inspect Zarr dataset structure"""
    return s1grits('zarr', 'inspect', '--tile', tile, '--direction', direction,
                   '--output-dir', output_dir, **kwargs)


def timeseries_plot(tile: str, direction: str,
                    pixel: Optional[tuple] = None,
                    lonlat: Optional[tuple] = None,
                    output_dir: str = './output',
                    output: Optional[str] = None,
                    **kwargs) -> subprocess.CompletedProcess:
    """Plot time series from Zarr dataset"""
    args = ['timeseries', 'plot', '--tile', tile, '--direction', direction, '--output-dir', output_dir]
    if pixel:
        args.extend(['--pixel', str(pixel[0]), str(pixel[1])])
    if lonlat:
        args.extend(['--lonlat', str(lonlat[0]), str(lonlat[1])])
    if output:
        args.extend(['--output', output])
    return s1grits(*args, **kwargs)


def export_png(tile: str, direction: str, month: str,
               variable: Optional[str] = None,
               output_dir: str = './output',
               output: Optional[str] = None,
               **kwargs) -> subprocess.CompletedProcess:
    """Export Zarr monthly data to PNG"""
    args = ['export', 'png', '--tile', tile, '--direction', direction, '--month', month, '--output-dir', output_dir]
    if variable:
        args.extend(['--variable', variable])
    if output:
        args.extend(['--output', output])
    return s1grits(*args, **kwargs)


def mosaic_create(month: str, direction: str,
                  band: Optional[str] = None,
                  output_dir: str = './output',
                  output: Optional[str] = None,
                  format: str = 'VRT',
                  mgrs_prefix: Optional[str] = None,
                  **kwargs) -> subprocess.CompletedProcess:
    """Create mosaic from multiple tiles"""
    args = ['mosaic', 'create', '--month', month, '--direction', direction, '--output-dir', output_dir]
    if band:
        args.extend(['--band', band])
    if output:
        args.extend(['--output', output])
    if format:
        args.extend(['--format', format])
    if mgrs_prefix:
        args.extend(['--mgrs-prefix', mgrs_prefix])
    return s1grits(*args, **kwargs)


def report_coverage(output_dir: str = './output', **kwargs) -> subprocess.CompletedProcess:
    """Generate coverage report"""
    return s1grits('report', 'coverage', '--output-dir', output_dir, **kwargs)
