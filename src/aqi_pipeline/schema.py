"""Column names, units and physical limits shared by every stage.

Units: pollutants in µg/m³ except CO in mg/m³ (the CPCB convention);
temperature °C, humidity %, wind speed m/s, precipitation mm per hour.
"""
from __future__ import annotations

POLLUTANTS = ["pm25", "pm10", "no2", "so2", "co", "o3"]
WEATHER = ["temperature", "humidity", "wind_speed", "precipitation"]
# Weather expected over the next 24 hours (mean, or total for precipitation),
# as issued at the row's timestamp. Optional: rows may leave these empty.
FORECAST = ["fc_temperature", "fc_humidity", "fc_wind_speed", "fc_precipitation"]

OBS_COLUMNS = POLLUTANTS + WEATHER + FORECAST
REQUIRED_COLUMNS = POLLUTANTS + WEATHER
CORE_SENSORS = ["pm25", "pm10"]          # the AQI is unreliable without these

# Physical plausibility limits; readings outside them are treated as sensor faults.
BOUNDS = {
    "pm25": (0.0, 1000.0), "pm10": (0.0, 1000.0), "no2": (0.0, 1000.0),
    "so2": (0.0, 1600.0), "co": (0.0, 50.0), "o3": (0.0, 800.0),
    "temperature": (-30.0, 55.0), "humidity": (0.0, 100.0),
    "wind_speed": (0.0, 60.0), "precipitation": (0.0, 200.0),
    "fc_temperature": (-30.0, 55.0), "fc_humidity": (0.0, 100.0),
    "fc_wind_speed": (0.0, 60.0), "fc_precipitation": (0.0, 1000.0),
}
# Continuous sensors where several identical consecutive readings mean a stuck sensor.
STUCK_COLUMNS = POLLUTANTS + ["temperature", "humidity"]

COLUMN_BIT = {c: i for i, c in enumerate(OBS_COLUMNS)}
