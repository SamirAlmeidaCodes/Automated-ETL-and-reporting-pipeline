"""Column contracts shared by transform, load, and reporting."""

HOURLY_COLUMNS = (
    "location",
    "timestamp",
    "temperature_c",
    "humidity_pct",
    "precipitation_mm",
    "wind_speed_kmh",
    "source",
)

METRIC_COLUMNS = (
    "temperature_c",
    "humidity_pct",
    "precipitation_mm",
    "wind_speed_kmh",
)

DAILY_COLUMNS = (
    "location_name",
    "observation_date",
    "temp_min_c",
    "temp_max_c",
    "temp_mean_c",
    "precipitation_sum_mm",
    "humidity_mean_pct",
    "wind_speed_mean_kmh",
    "observation_count",
    "source",
)

# Columns replaced when a location and UTC date already exist.
UPDATE_COLUMNS = (
    "temp_min_c",
    "temp_max_c",
    "temp_mean_c",
    "precipitation_sum_mm",
    "humidity_mean_pct",
    "wind_speed_mean_kmh",
    "observation_count",
    "source",
    "loaded_at",
)

OPEN_METEO_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
)
