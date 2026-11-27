# S1-GRiTS Analysis Module

`s1grits.analysis` reads existing products and provides optional analysis and
derived outputs. Creating a new Cube is not required for each analysis method.
Select an explicit product instance, time interval and spatial window before
running a tool; write derived products to a separate output location.

Start with the [entry points and support matrix](../../../docs/entry_points.md)
or the [small existing-Cube tutorial](../../../docs/getting_started_existing_cube_ZH.md).
The [Python API guide](../../../docs/python_api.md) describes programmatic use,
and [runtime call paths](../../../docs/call_flow.md) identifies implementation ownership.

| Capability | Public API examples | Guide |
| --- | --- | --- |
| Features, filtering, monthly products and interpolation | `derive_cube` | [Derived Cubes](../../../docs/cube_derivatives.md), [interpolation](../../../docs/temporal_interpolation.md) |
| Virtual products and explicit materialization | `define_view`, `register_view`, `materialize_view` | [Cube views](../../../docs/cube_views.md) |
| Multi-track monthly combination | `define_multitrack_view` | [Multi-track monthly products](../../../docs/multitrack_monthly.md) |
| Temporal analysis adapters | `analyze_segments`, `analyze_segments_batch`, `analyze_cusum` | [Segmentation](../../../docs/segmentation.md), [CUSUM](../../../docs/cusum.md) |
| Fixed-grid COG mosaics | `create_grid_mosaic` | [Grid mosaics](../../../docs/grid_mosaic.md) |
| Time-series extraction, plots and catalog checks | `extract_pixel_timeseries`, `plot_timeseries_figure`, `validate_catalog` | [Python API](../../../docs/python_api.md) |

Use `python -m s1grits <command> --help` for the installed CLI contract. Dependencies,
input units, supported combinations and experimental backend limits are documented
in each guide. Local CCDC is not implemented; selecting a remote backend is an
explicit action. Legacy readers and mosaic helpers remain available for existing
callers, with their original selection rules.
