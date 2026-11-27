"""Coverage reports accept both canonical and historical catalog tile names."""
import pandas as pd
import pytest

from s1grits.analysis.reporting import generate_coverage_report


@pytest.mark.parametrize('tile_column', ['tile_id', 'mgrs_tile_id'])
def test_catalog_tile_names(tmp_path, tile_column):
    pd.DataFrame({tile_column: ['17MPU', '17MPU', '17MQV'],
                  'datetime': ['2026-01-01', '2026-03-01', '2026-02-01'],
                  'flight_direction': ['ASCENDING'] * 3}).to_parquet(tmp_path / 'catalog.parquet')
    report = generate_coverage_report(tmp_path)
    assert report['success'], report['message']
    assert report['overall']['total_records'] == 3
    assert report['overall']['tile_count'] == 2
    assert report['overall']['date_range'] == ('2026-01-01', '2026-03-01')
    tiles = {row['tile_id']: row for row in report['tiles']}
    assert tiles['17MPU']['missing_list'] == ['2026-02']
    assert tiles['17MQV']['missing_months'] == 0
