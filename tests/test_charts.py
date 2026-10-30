"""The report always writes three PNG charts."""

from datetime import date

import pandas as pd

from etl.report.charts import render_report


def test_render_writes_three_pngs(tmp_path) -> None:
    frame = pd.DataFrame(
        [
            {
                "location_name": "Oslo",
                "observation_date": date(2026, 10, 27),
                "temp_mean_c": 4.0,
                "precipitation_sum_mm": 1.2,
                "wind_speed_mean_kmh": 14.0,
            },
            {
                "location_name": "Oslo",
                "observation_date": date(2026, 10, 28),
                "temp_mean_c": 6.5,
                "precipitation_sum_mm": 0.0,
                "wind_speed_mean_kmh": 9.0,
            },
            {
                "location_name": "Paris",
                "observation_date": date(2026, 10, 28),
                "temp_mean_c": 11.0,
                "precipitation_sum_mm": 0.4,
                "wind_speed_mean_kmh": 12.0,
            },
        ]
    )
    paths = render_report(frame, tmp_path, date(2026, 10, 28))
    assert [path.name for path in paths] == [
        "temperature_2026-10-28.png",
        "precipitation_2026-10-28.png",
        "wind_2026-10-28.png",
    ]
    for path in paths:
        data = path.read_bytes()
        assert data.startswith(b"\x89PNG")
        assert len(data) > 1000


def test_empty_report_still_writes_three_pngs(tmp_path) -> None:
    paths = render_report(pd.DataFrame(), tmp_path, date(2026, 10, 28))
    assert len(paths) == 3
    for path in paths:
        assert path.read_bytes().startswith(b"\x89PNG")
