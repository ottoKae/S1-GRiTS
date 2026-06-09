class NoMGRSCoverage(Exception):
    """Exception raised for no MGRS coverage."""


class ProcessingSignatureMismatchError(ValueError):
    """
    Raised when catalog records or product instances have incompatible
    processing signatures and mixing them is not permitted.

    This occurs when:
    - Two records for the same Zarr store were processed with different
      variant settings (e.g. TV5 vs TV25 despeckle strength).
    - The resolver encounters multiple processing_signature values for
      a single product_type when require_same_processing=True.
    - A STAC item's s1grits:processing_signature does not match the
      corresponding catalog record.
    """
