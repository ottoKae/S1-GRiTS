from __future__ import annotations

import argparse
import os
from importlib.metadata import version
from pathlib import Path

from grits_resolver import COMMON_OPTICAL_ALIASES, resolve_alias
from grits_resolver.runtime import configure_gdal_data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    installed = version("grits-resolver")
    if args.expected_version and installed != args.expected_version:
        raise SystemExit(
            f"grits-resolver version mismatch: expected={args.expected_version}, installed={installed}"
        )
    gdal_data = configure_gdal_data()
    if gdal_data is None or not (Path(gdal_data) / "gdalvrt.xsd").exists():
        raise SystemExit("GDAL_DATA is not configured or gdalvrt.xsd is missing")
    if resolve_alias("sentinel-2", "nir_narrow") != "nir_narrow":
        raise SystemExit("Resolver alias contract check failed")
    print(f"grits-resolver={installed}")
    print(f"GDAL_DATA={os.environ['GDAL_DATA']}")
    print(f"aliases={','.join(COMMON_OPTICAL_ALIASES)}")


if __name__ == "__main__":
    main()

