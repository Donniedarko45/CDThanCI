# Monitoring: data quality, drift and accuracy

Before the pipeline decides anything, each cycle answers three questions:

| Question | Module | When the answer is available |
| --- | --- | --- |
| Can the new data be trusted? | `validation.py` (quality gate) | immediately |
| Do the inputs still look like the data the model was trained on? | `drift.py` (input drift) | immediately, no labels needed |
| Is the model still as accurate as expected? | `performance.py` (accuracy) | 24 hours later, when the true AQI is known |

The answers are passed to the decision policy (`policy.py`) as `Signals`. A fourth mechanism,
safe mode, protects what users are served while the model is struggling.

All thresholds below are defaults from `src/aqi_pipeline/config.py`; each can be changed in the
YAML config (`data:`, `monitor:` and `retrain:` sections).

## 1. Data quality gate (`validation.py`)

Every batch of new hourly rows passes through `validate_batch` before it is stored.

| Check | Rule | Effect |
| --- | --- | --- |
| Schema | The 6 pollutant and 4 weather columns are present and timestamps parse (time-zone-aware values are converted to UTC) | The batch is rejected |
| Duplicates | A timestamp appears more than once | The first row is kept |
| Gaps | An hour is missing, including between the last stored hour and the new batch | An empty row is inserted, so the series stays strictly hourly |
| Range | A reading is physically impossible (`schema.BOUNDS`, e.g. PM2.5 outside 0–1000 µg/m³, humidity outside 0–100 %) | The cell is flagged |
| Stuck sensor | A pollutant, temperature or humidity repeats one value for 6 or more consecutive hours (`data.stuck_hours`). The tail of already-stored rows is included, so a run that began in an earlier batch is still caught | The cell is flagged |

**Raw values are never altered.** Each row gets a `qc_mask` (a bitmask with one bit per column)
and `qc_notes` (for example `range:co,stuck:pm25`). Flagged cells are blanked only when data is
loaded for modelling (`Store.load_observations(clean=True)` calls `apply_mask`), so the original
readings stay available for audit.

The batch status is `fail` when 25 % or more of the PM2.5 or PM10 readings are missing or
faulty, `warn` when any required column is 5 % or more bad or there were duplicates or gaps,
and `pass` otherwise. Every batch that does not pass is written to the event log.

### Sensor faults are not drift

A broken sensor should lead to fixing the sensor, not to retraining the model. Two rules
enforce this:

- An AQI value counts as reliable only if the preceding 24 hours contain at least 16 valid
  readings of both PM2.5 and PM10 (`data.min_label_hours`). Forecasts made from unreliable
  inputs are stored with `input_ok = 0`, and forecasts whose target hour is unreliable get
  `label_ok = 0`. Neither is used for accuracy monitoring, for training or by the retraining gate.
- If 15 % or more of the last 3 days of PM2.5 or PM10 readings are missing or faulty
  (`retrain.quality_hold_fraction`, `retrain.quality_window_days`), the decision is `hold`:
  no retraining until the data is clean.

## 2. Input drift (`drift.py`)

Drift monitoring compares the last 14 days of inputs (`monitor.drift_window_days`) with the
champion model's training data. It needs no labels, so it can react before accuracy is
measurable. Fifteen series are monitored: the 14 observation columns and the AQI itself.

The statistic is the **Population Stability Index (PSI)**, computed per input on 10 quantile
bins of the reference data. A PSI near 0 means the two samples have the same distribution;
around 0.25 and above is conventionally read as a large shift.

Air quality is strongly seasonal, so comparing a fortnight in November with "all training data"
would raise an alarm every few weeks. Three refinements keep the signal meaningful:

1. **Seasonal reference.** The window is compared with training data from the same time of
   year (day of year within ±30 days, `season_half_width_days`), provided that slice holds at
   least 500 rows (`min_reference_rows`). Otherwise all training data is used. A model trained
   in spring that meets its first winter falls back to "all", where a large PSI correctly means
   "the model has never seen conditions like these".
2. **Calibrated thresholds.** When a model is registered, the same test is replayed over its own
   training history: 14-day windows stepped by 7 days (`calibration_step_days`), each compared
   with a reference that excludes the window and 7 days either side. An input's alarm threshold
   is 1.25 × the 95th percentile of those historical PSI values (`calibration_margin`,
   `calibration_quantile`), never below 0.25 (`psi_floor`). Ordinary weather variability
   therefore does not trip the alarm. With fewer than 8 calibration windows the floor is used.
3. **Novelty.** An input also counts as drifted when 25 % or more of its last 3 days of readings
   lie outside the 0.5 %–99.5 % range of *all* training data (`novelty_share`,
   `novelty_window_days`, `envelope_quantile`). The model is then being asked to extrapolate,
   and this check shows it within days rather than weeks.

**Importance weighting.** The drift score is the share of the model's reliance that sits on
drifted inputs. Reliance is measured at training time by permutation importance per raw input
(`modeling.group_importance`): all features derived from one input are shuffled together, the
rise in MAE is that input's importance, and the importances are normalised to sum to 1. Drift
in an input the model barely uses barely moves the score.

| Level | Drift score |
| --- | --- |
| `alert` | 0.35 or more (`drift_alert_score`) |
| `warn` | 0.15 or more (`drift_warn_score`) |
| `ok` | below 0.15 |

The pipeline also counts consecutive cycles at `alert` (`drift_streak`). Each drift report lists
the drifted inputs, ordered by how far they exceed their thresholds, which makes a silent
upstream change easy to diagnose: in the end-to-end test (`tests/test_pipeline.py`) the wind
forecast silently switches from m/s to km/h, and the monitor names `fc_wind_speed` within days.

## 3. Accuracy with delayed labels (`performance.py`)

A forecast issued at hour *t* is for *t* + 24 h, so its true value is known a day later. Each
cycle, `Pipeline.score_matured` attaches the true AQI (`y_true`, `label_ok`) to every forecast
whose target hour has passed.

### Judged against persistence

Persistence is the forecast "AQI in 24 hours = AQI now". Every forecast row stores the model's
forecast (`y_model`), the persistence forecast (`y_naive`) and what was actually served
(`y_pred`). Accuracy is expressed as a ratio:

```
ratio = model MAE / (ref_rel_mae × persistence MAE)        over the last 7 days
```

`ref_rel_mae` is the edge over persistence the model showed at validation; 0.8 means 20 % lower
error than persistence. A ratio of 1.0 means the model keeps exactly the edge it was expected to
have. A difficult week (a dust storm, a stagnant spell) raises the error of every method and
leaves the ratio alone; only a loss of edge raises it.

| Level | Condition (window: `perf_window_days` = 7) |
| --- | --- |
| `critical` | ratio ≥ 1.5 (`perf_critical_ratio`) and MAE at least 3 AQI points above expectation (`perf_min_gap`) |
| `warn` | ratio ≥ 1.25 (`perf_warn_ratio`) and MAE at least 3 AQI points above expectation |
| `ok` | otherwise |
| `unknown` | fewer than 24 usable forecasts, or usable forecasts cover less than half the window (`min_coverage`) |

Only forecasts with clean inputs **and** clean labels count (`valid_rows`). The pipeline counts
consecutive cycles at `warn` or `critical` (`perf_streak`).

### CUSUM: slow decay

Some decay is too gradual to cross the window thresholds. A one-sided CUSUM on the daily error
ratio accumulates sustained excess error:

```
g = max(0, g + min(daily_ratio − 1, 1.5) − 0.30)           alarm when g > 4.0
```

The slack of 0.30 per day (`cusum_slack`) absorbs small excesses, so only error that stays high
day after day reaches the alarm (`cusum_alarm`). The daily ratio uses a floor on the expected
error (a quarter of the validation MAE, at least 1 AQI point), so a single calm day cannot
inflate it. The CUSUM is reset after every retraining attempt and every rollback.

## 4. Safe mode

Over the last 3 days of usable forecasts (`safe_window_days`; at least 36 are needed):

- **enter** when the model's MAE is at least 1.25 × the persistence MAE and at least 3 AQI
  points worse (`safe_enter_ratio`, `perf_min_gap`);
- **leave** when the model's MAE is no worse than persistence (`safe_exit_ratio` = 1.0).

In safe mode the persistence forecast is served (`y_pred`, with `served = "persistence"`), but
the model keeps forecasting in the background (`y_model`) and monitoring keeps scoring those
model forecasts, so its recovery, or its replacement, is still measured. A newly promoted model
that did not beat persistence on its holdout starts in safe mode. Entering and leaving safe mode
are logged as `safe_mode_on` and `safe_mode_off` events.

In the simulated demo year, persistence was served for 1,320 of 8,585 scored hours (15 %); the
README flags these thresholds for tuning on real data.

## Where to see the signals

- `aqi-pipeline status` prints the latest levels and the decision.
- `GET /monitoring` returns the full record of the latest cycle.
- The dashboard's *Monitoring & Model Health* section and the report's *Monitoring signals*
  chart plot the drift score, the accuracy ratio and the CUSUM for every cycle.
