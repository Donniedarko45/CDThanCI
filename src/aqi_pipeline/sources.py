"""Data sources. Each returns raw hourly rows for a time span; validation comes after.

* ``FrameSource``     - an in-memory frame (used by the simulation and tests)
* ``CsvSource``       - a CSV file of historical station data
* ``OpenMeteoSource`` - the Open-Meteo air-quality and weather APIs (no key needed)

Open-Meteo values are modelled (CAMS) rather than measured by a station, and
this connector could not be exercised against the live API in the environment
it was written in; its parser is covered by a test on a recorded-shape payload.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from .schema import FORECAST, OBS_COLUMNS, POLLUTANTS, WEATHER, to_utc_naive, to_utc_naive_dt


class Source(Protocol):
    def fetch(self, since: pd.Timestamp | None, until: pd.Timestamp) -> pd.DataFrame:
        """Rows with ``since < timestamp <= until`` and a ``timestamp`` column."""


def add_forecast_columns(df: pd.DataFrame, noise: bool = False) -> pd.DataFrame:
    """Fill the fc_* columns from the weather that followed each row.

    For historical files there is no archive of the forecasts that were issued,
    so the next 24 hours of observed weather stand in for them ("perfect
    prognosis"). Live forecasts are less accurate; expect some loss of skill.
    """
    out = df.copy()
    for col in WEATHER:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    def _rev(s: pd.Series, how: str) -> pd.Series:
        return getattr(s[::-1].rolling(24, min_periods=18), how)()[::-1].shift(-1)

    out["fc_temperature"] = _rev(out["temperature"], "mean")
    out["fc_humidity"] = _rev(out["humidity"], "mean")
    out["fc_wind_speed"] = _rev(out["wind_speed"], "mean")
    out["fc_precipitation"] = _rev(out["precipitation"], "sum")
    return out


class FrameSource:
    def __init__(self, frame: pd.DataFrame):
        frame = frame.copy()
        frame["timestamp"] = to_utc_naive_dt(frame["timestamp"])
        self.frame = frame.sort_values("timestamp").reset_index(drop=True)

    def fetch(self, since, until) -> pd.DataFrame:
        ts = self.frame["timestamp"]
        mask = ts <= to_utc_naive(until)
        if since is not None:
            mask &= ts > to_utc_naive(since)
        return self.frame[mask].copy()


class CsvSource(FrameSource):
    """A CSV with one row per hour.

    ``columns`` maps this pipeline's names to the file's names, for example
    ``{"timestamp": "Datetime", "pm25": "PM2.5", "co": "CO"}``. Unmapped names
    are looked up as-is. CO must be in mg/m³ and the others in µg/m³; use
    ``scale`` (for example ``{"co": 0.001}``) to convert.
    """

    def __init__(self, path: str | Path, columns: dict[str, str] | None = None, scale: dict[str, float] | None = None):
        raw = pd.read_csv(path)
        mapping = {v: k for k, v in (columns or {}).items()}
        raw = raw.rename(columns=mapping)
        if "timestamp" not in raw.columns:
            raise ValueError("CSV needs a timestamp column (map it with columns={'timestamp': ...})")
        keep = ["timestamp"] + [c for c in OBS_COLUMNS if c in raw.columns]
        frame = raw[keep].copy()
        frame["timestamp"] = to_utc_naive_dt(frame["timestamp"])
        for col, factor in (scale or {}).items():
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce") * factor
        if not any(c in frame.columns for c in FORECAST) and all(c in frame.columns for c in WEATHER):
            frame = add_forecast_columns(frame.sort_values("timestamp").reset_index(drop=True))
        super().__init__(frame)


AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WX_URL = "https://api.open-meteo.com/v1/forecast"
AQ_VARS = {
    "pm2_5": "pm25",
    "pm10": "pm10",
    "nitrogen_dioxide": "no2",
    "sulphur_dioxide": "so2",
    "carbon_monoxide": "co",
    "ozone": "o3",
}
WX_VARS = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "wind_speed_10m": "wind_speed",
    "precipitation": "precipitation",
}


def parse_open_meteo(air_quality: dict[str, Any], weather: dict[str, Any], now: pd.Timestamp) -> pd.DataFrame:
    """Turn the two API responses into pipeline rows up to ``now``.

    The weather response includes forecast hours beyond ``now``; they feed the
    fc_* columns and are then dropped. CO arrives in µg/m³ and is converted to mg/m³.
    """
    aq = pd.DataFrame(air_quality["hourly"]).rename(columns={"time": "timestamp", **AQ_VARS})
    wx = pd.DataFrame(weather["hourly"]).rename(columns={"time": "timestamp", **WX_VARS})
    for col in POLLUTANTS:
        aq[col] = pd.to_numeric(aq[col], errors="coerce") if col in aq.columns else np.nan
    for col in WEATHER:
        wx[col] = pd.to_numeric(wx[col], errors="coerce") if col in wx.columns else np.nan
    for frame in (aq, wx):
        frame["timestamp"] = to_utc_naive_dt(frame["timestamp"])
    unit = air_quality.get("hourly_units", {}).get("carbon_monoxide", "μg/m³")
    if "mg" not in unit.lower():
        aq["co"] = aq["co"] / 1000.0
    wx = add_forecast_columns(wx.sort_values("timestamp").reset_index(drop=True))
    merged = wx.merge(aq[["timestamp"] + POLLUTANTS], on="timestamp", how="left")
    merged = merged[merged["timestamp"] <= to_utc_naive(now)]
    return merged[["timestamp"] + OBS_COLUMNS]


class OpenMeteoSource:
    """Hourly air quality and weather for one location (times in UTC)."""

    def __init__(self, latitude: float, longitude: float, timeout: int = 30):
        self.latitude, self.longitude, self.timeout = latitude, longitude, timeout

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        with urllib.request.urlopen(f"{url}?{query}", timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch(self, since, until) -> pd.DataFrame:
        until_ts = to_utc_naive(until)
        since_ts = to_utc_naive(since) if since is not None else None
        if since_ts is None:
            days_back = 92
        else:
            delta_days = max(0.0, (until_ts - since_ts).total_seconds() / 86400.0)
            days_back = int(np.clip(np.ceil(delta_days) + 2, 2, 92))
        common = {"latitude": self.latitude, "longitude": self.longitude, "timezone": "GMT", "past_days": days_back}
        aq = self._get(AQ_URL, {**common, "hourly": ",".join(AQ_VARS), "forecast_days": 1})
        wx = self._get(WX_URL, {**common, "hourly": ",".join(WX_VARS), "forecast_days": 3, "wind_speed_unit": "ms"})
        frame = parse_open_meteo(aq, wx, until_ts)
        if since_ts is not None:
            frame = frame[frame["timestamp"] > since_ts]
        return frame


def make_source(spec: dict[str, Any]) -> Source:
    kind = spec.get("type")
    if kind == "csv":
        return CsvSource(spec["path"], spec.get("columns"), spec.get("scale"))
    if kind == "open_meteo":
        return OpenMeteoSource(float(spec["latitude"]), float(spec["longitude"]))
    raise ValueError(
        f"source type {kind!r} needs to be 'csv' or 'open_meteo' (the synthetic source is created by the simulation)"
    )
