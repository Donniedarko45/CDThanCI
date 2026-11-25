"""Synthetic hourly air-quality stream for a north-Indian-style city.

The generator exists so the whole pipeline can be exercised and measured without
external data. It produces weather, six pollutants and next-24-hour weather
forecasts with the structure a forecasting model has to cope with: seasons,
daily and weekly traffic cycles, multi-day stagnation episodes, rain washout,
crop-burning smoke and dust.

``events`` inject the situations the monitoring is built for:

* ``sensor_stuck`` / ``sensor_dropout`` / ``sensor_out_of_range`` - sensor faults
* ``sensor_bias`` - a column scaled from a date: an instrument recalibration, or
  an upstream feed that changes units (for example wind forecasts in km/h)
* ``emission_regime`` - a change in how emissions and weather produce pollution
  (concept drift), abrupt or ramped in over ``ramp_days``

This is simulated data. It is not a record of any real station.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from .schema import FORECAST, POLLUTANTS, WEATHER, to_utc_naive

# Parameters an ``emission_regime`` event may change.
REGIME_DEFAULTS = {
    "traffic_scale": 1.0,  # overall traffic emissions
    "weekend_scale": 0.72,  # weekend traffic relative to weekdays
    "wind_beta": 0.80,  # how strongly wind disperses pollution
    "baseline_scale": 1.0,  # multiplier on all particulate sources
    "industry": 0.0,  # an extra round-the-clock source
    "washout": 1.0,  # efficiency of rain washout
    "persistence": 1.0,  # weight of multi-day stagnation episodes
    "ozone_scale": 1.0,  # photochemical ozone production (precursor emissions)
}


def _ar1(rng: np.random.Generator, n: int, rho: float, sd: float) -> np.ndarray:
    """Stationary AR(1) noise with standard deviation ``sd``."""
    if n <= 0:
        return np.empty(0, dtype=float)
    e = rng.normal(0.0, sd * np.sqrt(1.0 - rho**2), n)
    e[0] = rng.normal(0.0, sd)
    return lfilter([1.0], [1.0, -rho], e)


def _bump(doy: np.ndarray, center: float, width: float) -> np.ndarray:
    d = np.abs(doy - center)
    d = np.minimum(d, 365.25 - d)
    return np.exp(-0.5 * (d / width) ** 2)


def _hour_bump(hod: np.ndarray, center: float, width: float) -> np.ndarray:
    d = np.abs(hod - center)
    d = np.minimum(d, 24 - d)
    return np.exp(-0.5 * (d / width) ** 2)


def _forward(x: np.ndarray, hours: int, how: str = "mean") -> np.ndarray:
    """Aggregate of x over the next ``hours`` (t+1 .. t+hours); NaN at the tail."""
    c = np.concatenate([[0.0], np.cumsum(x)])
    out = np.full(len(x), np.nan)
    last = len(x) - hours
    if last > 0:
        idx = np.arange(last)
        total = c[idx + hours + 1] - c[idx + 1]
        out[:last] = total / hours if how == "mean" else total
    return out


def _regime_arrays(index: pd.DatetimeIndex, events: Iterable[dict]) -> dict[str, np.ndarray]:
    params = {k: np.full(len(index), v, dtype=float) for k, v in REGIME_DEFAULTS.items()}
    for ev in events:
        if ev.get("type") != "emission_regime":
            continue
        start = to_utc_naive(ev["start"])
        ramp = float(ev.get("ramp_days", 0.0))
        days = (index - start) / pd.Timedelta(days=1)
        weight = np.clip(days / ramp, 0.0, 1.0) if ramp > 0 else (days >= 0).astype(float)
        if ev.get("end"):
            weight = np.where(index >= to_utc_naive(ev["end"]), 0.0, weight)
        for key, new in ev.get("params", {}).items():
            if key not in params:
                raise KeyError(f"unknown emission_regime parameter: {key}")
            params[key] = params[key] + weight * (float(new) - REGIME_DEFAULTS[key])
    return params


def generate(start: str, end: str, seed: int = 7, events: Iterable[dict] | None = None) -> pd.DataFrame:
    """Hourly observations from ``start`` (inclusive) to ``end`` (exclusive)."""
    events = list(events or [])
    rng = np.random.default_rng(seed)
    tail = 24  # extra hours so forecasts exist at the end
    start_ts, end_ts = to_utc_naive(start), to_utc_naive(end)
    index = pd.date_range(start_ts, end_ts + pd.Timedelta(hours=tail - 1), freq="h")
    n = len(index)
    doy = index.day_of_year.to_numpy().astype(float)
    hod = index.hour.to_numpy().astype(float)
    dow = index.day_of_week.to_numpy()
    year = index.year.to_numpy()
    regime = _regime_arrays(index, events)

    winter = _bump(doy, 5, 38)
    monsoon = _bump(doy, 215, 32)
    premonsoon = _bump(doy, 140, 28)
    smoke_season = _bump(doy, 308, 13)

    # Season-to-season variation, so no two winters or burning seasons are alike.
    # A "season year" runs July to June, which keeps one winter under one multiplier.
    season = year + (index.month.to_numpy() >= 7)
    seasons = np.unique(season)
    yr_rng = np.random.default_rng(seed + 1000)
    winter_mult = dict(zip(seasons, np.exp(yr_rng.normal(0, 0.08, len(seasons)))))
    smoke_mult = dict(zip(seasons, np.exp(yr_rng.normal(0, 0.15, len(seasons)))))
    winter_y = winter * np.array([winter_mult[y] for y in season])
    smoke_y = smoke_season * np.array([smoke_mult[y] for y in season])

    # ---- weather ---------------------------------------------------------
    t_season = 25.5 - 11.5 * np.cos(2 * np.pi * (doy - 18) / 365.25) - 3.0 * monsoon
    temperature = (
        t_season + (4.5 + 1.5 * (1 - monsoon)) * np.cos(2 * np.pi * (hod - 15) / 24) + _ar1(rng, n, 0.995, 2.2)
    )
    wind = np.exp(
        np.log(2.1)
        + 0.25 * premonsoon
        - 0.40 * winter
        + 0.10 * monsoon
        + 0.22 * np.cos(2 * np.pi * (hod - 14) / 24)
        + _ar1(rng, n, 0.985, 0.38)
    )

    n_days = n // 24 + 2
    day_idx = np.arange(n) // 24
    mon_day = np.array([monsoon[min(d * 24, n - 1)] for d in range(n_days)])
    win_day = np.array([winter[min(d * 24, n - 1)] for d in range(n_days)])
    wet = np.zeros(n_days, dtype=bool)
    for d in range(1, n_days):
        p = (0.25 + 0.50 * mon_day[d]) if wet[d - 1] else (0.02 + 0.40 * mon_day[d] + 0.03 * win_day[d])
        wet[d] = rng.random() < p
    wet_hour = wet[day_idx]
    precipitation = wet_hour * (rng.random(n) < 0.30) * rng.exponential(2.5, n)
    humidity = np.clip(
        48 + 30 * monsoon + 14 * winter - 1.5 * (temperature - t_season) + 16 * wet_hour + _ar1(rng, n, 0.97, 6.0),
        8,
        100,
    )

    # ---- emissions and dispersion ---------------------------------------
    stagnation = regime["persistence"] * _ar1(rng, n, 0.997, 0.20)  # multi-day episodes
    dispersion = np.clip((wind / 2.0) ** (-regime["wind_beta"]), 0.2, 5.0)
    mixing = 1 + (0.22 + 0.33 * winter) * np.cos(2 * np.pi * (hod - 4) / 24)
    recent_rain = lfilter([1.0], [1.0, -0.92], precipitation)
    washout = np.exp(-0.10 * regime["washout"] * np.minimum(recent_rain, 12.0))

    weekday_factor = np.where(dow >= 5, regime["weekend_scale"], 1.0) * regime["traffic_scale"]
    traffic = weekday_factor * (0.30 + 0.85 * _hour_bump(hod, 9, 1.8) + 1.0 * _hour_bump(hod, 19.5, 2.4))
    heating = winter_y * (0.35 + 0.90 * _hour_bump(hod, 21.5, 3.0) + 0.45 * _hour_bump(hod, 6.5, 1.8))
    smoke = smoke_y * np.exp(_ar1(rng, n, 0.995, 0.35))
    background = 1 + 1.3 * winter_y - 0.40 * monsoon
    industry = regime["industry"]

    common = dispersion * mixing * np.exp(stagnation)
    pm25 = (
        regime["baseline_scale"]
        * (16 * background + 20 * traffic + 48 * heating + 110 * smoke + 30 * industry)
        * common
        * washout
        * np.exp(_ar1(rng, n, 0.70, 0.10))
    )
    dust = premonsoon * 55 * np.maximum(wind - 3.2, 0.0) ** 1.3
    pm10 = (
        pm25 * (1.45 + 0.25 * premonsoon + _ar1(rng, n, 0.80, 0.04))
        + regime["baseline_scale"] * 22 * traffic * dispersion
        + dust
    ) * np.exp(_ar1(rng, n, 0.70, 0.05))
    gas_common = dispersion**0.8 * mixing * np.exp(0.6 * stagnation)
    no2 = (11 + 36 * traffic + 9 * heating + 6 * industry) * gas_common * np.exp(_ar1(rng, n, 0.70, 0.10))
    co = 0.55 * (0.5 + 0.9 * traffic + 0.8 * heating + 1.3 * smoke) * gas_common * np.exp(_ar1(rng, n, 0.70, 0.10))
    so2 = (
        (7 + 5 * winter + 9 * industry) * dispersion**0.5 * np.exp(0.4 * stagnation) * np.exp(_ar1(rng, n, 0.70, 0.12))
    )
    solar = (
        np.clip(np.sin(np.pi * (hod - 6) / 12), 0, None) ** 1.3
        * (0.70 + 0.30 * np.cos(2 * np.pi * (doy - 172) / 365.25))
        * (1 - 0.45 * wet_hour)
    )
    o3 = np.clip(
        22
        + regime["ozone_scale"]
        * 78
        * solar
        * np.clip((temperature - 8) / 28, 0.15, 1.3)
        * np.exp(_ar1(rng, n, 0.7, 0.08))
        - 0.18 * no2
        + rng.normal(0, 2.0, n),
        2.0,
        None,
    )
    # Particulate analysers saturate just below 1000 µg/m³.
    pm25 = np.minimum(pm25, 985.0)
    pm10 = np.minimum(pm10, 985.0)

    # ---- next-24-hour weather forecasts (truth plus forecast error) ------
    fc_wind = _forward(wind, 24) * np.exp(rng.normal(0, 0.10, n))
    fc_temp = _forward(temperature, 24) + rng.normal(0, 0.8, n)
    fc_hum = np.clip(_forward(humidity, 24) + rng.normal(0, 5.0, n), 5, 100)
    fc_rain = _forward(precipitation, 24, "sum") * np.exp(rng.normal(0, 0.35, n)) * (rng.random(n) > 0.08)

    df = pd.DataFrame(
        {
            "pm25": np.round(pm25, 1),
            "pm10": np.round(pm10, 1),
            "no2": np.round(no2, 1),
            "so2": np.round(so2, 1),
            "co": np.round(co, 2),
            "o3": np.round(o3, 1),
            "temperature": np.round(temperature, 1),
            "humidity": np.round(humidity, 1),
            "wind_speed": np.round(wind, 2),
            "precipitation": np.round(precipitation, 1),
            "fc_temperature": np.round(fc_temp, 1),
            "fc_humidity": np.round(fc_hum, 1),
            "fc_wind_speed": np.round(fc_wind, 2),
            "fc_precipitation": np.round(fc_rain, 1),
        },
        index=index,
    )[POLLUTANTS + WEATHER + FORECAST]

    _apply_sensor_events(df, events, rng)
    df = df.iloc[:-tail]
    df.index.name = "timestamp"
    return df.reset_index()


def _apply_sensor_events(df: pd.DataFrame, events: Iterable[dict], rng: np.random.Generator) -> None:
    for ev in events:
        kind = ev.get("type")
        if kind == "emission_regime":
            continue
        start = to_utc_naive(ev["start"])
        end = to_utc_naive(ev["end"]) if ev.get("end") else df.index[-1] + pd.Timedelta(hours=1)
        mask = (df.index >= start) & (df.index < end)
        columns = ev.get("columns") or [ev["column"]]
        for col in columns:
            if kind == "sensor_stuck":
                if mask.any():
                    df.loc[mask, col] = df.loc[mask, col].iloc[0]
            elif kind == "sensor_dropout":
                drop = mask & (rng.random(len(df)) < float(ev.get("rate", 1.0)))
                df.loc[drop, col] = np.nan
            elif kind == "sensor_out_of_range":
                df.loc[mask, col] = float(ev.get("value", -999.0))
            elif kind == "sensor_bias":
                df.loc[mask, col] = np.round(df.loc[mask, col] * float(ev["factor"]), 1)
            else:
                raise ValueError(f"unknown event type: {kind}")
