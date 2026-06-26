"""
Entry point for running s1grits as a module.

This allows the package to be executed as:
    python -m s1grits [command] [options]

Examples:
    python -m s1grits process --config config.yaml
    python -m s1grits catalog resync --output-dir ./output
    python -m s1grits tile inspect --tile 50RKV --direction ASCENDING
"""

from s1grits.cli import main

if __name__ == '__main__':
    main()
