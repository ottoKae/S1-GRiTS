"""
Logging configuration module

Provides a layered logging system:
- Console: Concise output (WARNING and above)
- File: Full logs (DEBUG level)
- Third-party libraries: Redirected to file
"""

import logging
import sys
from pathlib import Path
from datetime import datetime


class SuppressFilter(logging.Filter):
    """Suppress specific log records"""

    def __init__(self, patterns: list[str]):
        super().__init__()
        self.patterns = patterns

    def filter(self, record):
        # Check if the log name matches the suppression patterns
        for pattern in self.patterns:
            if record.name.startswith(pattern):
                return False
        return True


def setup_logging(config: dict) -> tuple[str, logging.Logger]:
    """
    Configure layered logging system

    Args:
        config: Configuration dictionary

    Returns:
        (log_file_path, logger)
    """
    logging_config = config.get('logging', {})

    # Log levels
    file_level = logging_config.get('file_level', 'DEBUG')
    console_level = logging_config.get('console_level', 'WARNING')

    # Log file path
    log_file_template = logging_config.get('log_file', './logs/s1_processing_{timestamp}.log')
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_file_template.format(timestamp=timestamp)

    # Create log directory
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Clear existing handlers
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)

    # === File Handler (Full Logs) ===
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, file_level))
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # === Console Handler (Concise Output) ===
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level))
    console_formatter = logging.Formatter(
        '%(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # === Suppress third-party library logs ===
    if logging_config.get('suppress_third_party', True):
        # List of third-party libraries to suppress
        suppress_patterns = [
            'urllib3',
            'requests',
            'rasterio',
            'fiona',
            'botocore',
            'boto3',
            's3transfer',
            'PIL',
            'matplotlib',
            'numba',
            'stac_geoparquet',
            'pyproj',
            'aiobotocore',
            'aiohttp',
            'py.warnings',
        ]

        # Add suppression filter to console
        suppress_filter = SuppressFilter(suppress_patterns)
        console_handler.addFilter(suppress_filter)

        # Simultaneously lower the log level for these libraries
        for lib in suppress_patterns:
            logging.getLogger(lib).setLevel(logging.WARNING)

    # Special handling: completely silence certain libraries
    logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)
    logging.getLogger('rasterio._env').setLevel(logging.ERROR)

    # Route Python warnings (e.g. pandas FutureWarning) through logging so they
    # land in the log file but are filtered off the console (py.warnings is in
    # the console suppress list) instead of cluttering stdout.
    logging.captureWarnings(True)

    # Create application logger
    app_logger = logging.getLogger('s1_processor')
    app_logger.setLevel(logging.DEBUG)

    app_logger.info(f"Logging system configured")
    app_logger.info(f"  - Log file: {log_file}")
    app_logger.info(f"  - File level: {file_level}")
    app_logger.info(f"  - Console level: {console_level}")

    return str(log_file), app_logger


_NOISY_LIBS = (
    'urllib3', 'requests', 'rasterio', 'fiona', 'botocore', 'boto3',
    's3transfer', 'PIL', 'matplotlib', 'numba', 'stac_geoparquet', 'pyproj',
    'aiobotocore', 'aiohttp', 'asyncio',
)


def quiet_noisy_loggers() -> None:
    """Keep the console clean for commands that do NOT call ``setup_logging``
    (e.g. ``catalog resync``/``inspect``, ``tile``, ``mosaic``).

    A third-party import can install a root StreamHandler via
    ``logging.basicConfig`` and leak INFO spam (rasterio GDAL notes,
    stac-geoparquet progress) to stdout. This removes stray root handlers,
    lowers the noisy libraries to WARNING (rasterio._env to ERROR), and routes
    Python warnings through logging. App output for these commands is printed
    explicitly via the rich console, so no console log handler is added.
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.WARNING)
    for lib in _NOISY_LIBS:
        logging.getLogger(lib).setLevel(logging.WARNING)
    logging.getLogger('rasterio._env').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)
    logging.captureWarnings(True)


def get_logger(name: str = None) -> logging.Logger:
    """
    Get logger instance

    Args:
        name: logger name (default is 's1_processor')

    Returns:
        logging.Logger
    """
    if name:
        return logging.getLogger(f's1_processor.{name}')
    return logging.getLogger('s1_processor')
