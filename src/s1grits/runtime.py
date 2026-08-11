from __future__ import annotations

import os
import sys
from pathlib import Path


def configure_gdal_data() -> Path | None:
    configured = os.environ.get("GDAL_DATA")
    if configured and (Path(configured) / "gdalvrt.xsd").exists():
        return Path(configured)
    for candidate in (
        Path(sys.prefix) / "Library" / "share" / "gdal",
        Path(sys.prefix) / "share" / "gdal",
    ):
        if (candidate / "gdalvrt.xsd").exists():
            os.environ["GDAL_DATA"] = str(candidate)
            return candidate
    return None
