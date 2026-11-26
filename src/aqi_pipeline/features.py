"""Feature engineering for the 24-hour-ahead AQI forecast.

Every feature at time t uses only information available at t: readings up to t,
the weather forecast issued at t, and the calendar of the target window. The
target is the AQI at t + horizon, whose averaging window (t+1 .. t+horizon for
the 24-hour pollutants) lies entirely in the future, so nothing leaks.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .aqi import compute_aqi
from .schema import FORECAST, POLLUTANTS, WEATHER

AQI_LAGS = [6, 12, 24, 48, 72, 168]
HISTORY_HOURS = 24 * 9          # history needed before the first usable feature row


@dataclass
class Dataset:
    X: pd.DataFrame             # features, indexed by issue time t
    y: pd.Series                # AQI at t + horizon (NaN where not yet known)
    aqi_now: pd.Series          # AQI at t: the persistence forecast and the delta reference
    aqi_ok: pd.Series           # AQI at t rests on enough valid PM2.5 and PM10 hours
    target_ok: pd.Series        # same test for the label at t + horizon
    groups: dict[str, list[str]]  # raw input -> features derived from it

    def usable(self) -> pd.Series:
        """Rows fit for training and scoring: clean inputs and a clean, known label."""
        return self.aqi_ok & self.target_ok & self.y.notna() & self.aqi_now.notna()


def _weekend_fraction(index: pd.DatetimeIndex, shift_hours: int) -> np.ndarray:
    """Share of the 24 hours ending at t + shift that fall on a weekend."""
    if len(index) == 0:
        return np.zeros(0, dtype=float)
    # the calendar is known in advance, so build it on an extended index
    ext = pd.date_range(index[0] - pd.Timedelta(hours=24), index[-1] + pd.Timedelta(hours=shift_hours), freq="h")
    wk = pd.Series((ext.day_of_week >= 5).astype(float), index=ext).rolling(24, min_periods=1).mean()
    return wk.reindex(index + pd.Timedelta(hours=shift_hours)).to_numpy()


def build_dataset(obs: pd.DataFrame, horizon: int = 24, min_label_hours: int = 16,
                  use_day_of_year: bool = False) -> Dataset:
    """``obs``: cleaned observations on a complete hourly DatetimeIndex."""
    idx = obs.index
    aq = compute_aqi(obs)
    aqi = aq["aqi"]
    f: dict[str, pd.Series | np.ndarray] = {}
    groups: dict[str, list[str]] = {c: [] for c in POLLUTANTS + WEATHER + FORECAST + ["aqi", "calendar"]}

    def add(group: str, name: str, values) -> None:
        f[name] = values
        groups[group].append(name)

    for col in POLLUTANTS + WEATHER:
        add(col, col, obs[col])
    add("aqi", "aqi", aqi)
    for lag in AQI_LAGS:
        add("aqi", f"aqi_lag{lag}", aqi.shift(lag))
    add("aqi", "aqi_diff6", aqi - aqi.shift(6))
    add("aqi", "aqi_diff24", aqi - aqi.shift(24))
    add("aqi", "aqi_mean72", aqi.rolling(72, min_periods=36).mean())
    add("aqi", "aqi_mean168", aqi.rolling(168, min_periods=84).mean())

    for col in ["pm25", "pm10", "no2", "co", "o3"]:
        add(col, f"{col}_mean3", obs[col].rolling(3, min_periods=2).mean())
        add(col, f"{col}_mean24", obs[col].rolling(24, min_periods=12).mean())
    for col in ["pm25", "pm10"]:
        mean6 = obs[col].rolling(6, min_periods=3).mean()
        add(col, f"{col}_mean6", mean6)
        add(col, f"{col}_max24", obs[col].rolling(24, min_periods=12).max())
        add(col, f"{col}_trend", mean6 / f[f"{col}_mean24"].clip(lower=1.0))
    add("pm25", "pm25_std24", obs["pm25"].rolling(24, min_periods=12).std())

    wind24 = obs["wind_speed"].rolling(24, min_periods=12).mean()
    temp24 = obs["temperature"].rolling(24, min_periods=12).mean()
    hum24 = obs["humidity"].rolling(24, min_periods=12).mean()
    add("wind_speed", "wind_mean24", wind24)
    add("temperature", "temp_mean24", temp24)
    add("humidity", "hum_mean24", hum24)
    add("precipitation", "precip_sum24", obs["precipitation"].rolling(24, min_periods=12).sum())
    add("precipitation", "precip_sum72", obs["precipitation"].rolling(72, min_periods=24).sum())

    for col in FORECAST:
        add(col, col, obs[col] if col in obs.columns else np.nan)
    fc = {c: (obs[c] if c in obs.columns else pd.Series(np.nan, index=idx)) for c in FORECAST}
    add("fc_wind_speed", "fc_wind_ratio", fc["fc_wind_speed"] / wind24.clip(lower=0.2))
    add("fc_temperature", "fc_temp_change", fc["fc_temperature"] - temp24)
    add("fc_humidity", "fc_hum_change", fc["fc_humidity"] - hum24)

    hour = idx.hour.to_numpy()
    target_time = idx + pd.Timedelta(hours=horizon)
    doy = target_time.day_of_year.to_numpy()
    add("calendar", "hour_sin", np.sin(2 * np.pi * hour / 24))
    add("calendar", "hour_cos", np.cos(2 * np.pi * hour / 24))
    if use_day_of_year:
        # Off by default: with only a year or two of history a tree model uses the
        # date to memorise last year's episodes instead of learning from the weather.
        add("calendar", "doy_sin", np.sin(2 * np.pi * doy / 365.25))
        add("calendar", "doy_cos", np.cos(2 * np.pi * doy / 365.25))
    add("calendar", "weekend_now", _weekend_fraction(idx, 0))
    add("calendar", "weekend_target", _weekend_fraction(idx, horizon))

    X = pd.DataFrame(f, index=idx).astype(float)

    pm_ok = pd.Series(True, index=idx)
    for col in ("pm25", "pm10"):
        pm_ok &= obs[col].rolling(24, min_periods=1).count() >= min_label_hours
    aqi_ok = pm_ok & aqi.notna()
    y = aqi.shift(-horizon)
    target_ok = aqi_ok.shift(-horizon, fill_value=False).astype(bool)
    return Dataset(X=X, y=y, aqi_now=aqi, aqi_ok=aqi_ok, target_ok=target_ok, groups=groups)
