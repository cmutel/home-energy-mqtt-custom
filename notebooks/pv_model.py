# %% [markdown]
# # PV forecast: simple ratio vs gradient boosting
#
# Compares ways to forecast our hourly PV from the day-ahead weather
# forecast, to decide what `dorfsteig_forecast/pv.py` should do. Run on the
# Mac with a copy of the Pi's database:
#
#     scp pi:.local/share/dorfsteig/dorfsteig.sqlite /tmp/
#     uv run --extra ml python notebooks/pv_model.py /tmp/dorfsteig.sqlite
#
# (or open it in VS Code, which runs the `# %%` cells one by one).
#
# Each month is forecast by models fitted on all the other months
# (leave-one-month-out), so no model sees the month it's scored on.
#
# Result on 2025-10 to 2026-10 (8231 hours; mean PV 31 kWh/day):
#
# | model                                    | hourly MAE | daily MAE |
# |------------------------------------------|-----------:|----------:|
# | one factor × radiation                   |     520 Wh |  7.1 kWh  |
# | factor per (week, hour), station fit     |     417 Wh |  6.0 kWh  |
# | factor per (week, hour), forecast fit    |     415 Wh |  5.9 kWh  |
# | gradient boosting, forecast              |     397 Wh |  5.8 kWh  |
#
# The forecast-fitted ratio has no bias (0.0 kWh/day; the station-fitted
# one +2.2, because the forecast is sunnier than the station). Gradient
# boosting is barely better, so pv.py uses the forecast-fitted ratio.

# %%
import sqlite3
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dorfsteig.sqlite"
TZ = "Europe/Zurich"
LATITUDE, LONGITUDE = 47.5, 8.2   # roughly; only used for the sun's position

conn = sqlite3.connect(DB)

# %% [markdown]
# ## Data
# Hourly PV, the station's measured radiation, and the newest ICON-CH2
# forecast issued 12-36 h before each hour (as in pv.py).

# %%
pv = pd.read_sql("SELECT ts, pv_wh FROM energy_hourly WHERE pv_wh IS NOT NULL", conn).set_index("ts")
station = pd.read_sql("SELECT ts, radiation AS rad, temperature AS temp FROM weather "
                      "WHERE source = 'meteoswiss'", conn).set_index("ts")
forecast = pd.read_sql(
    "SELECT issued, ts, radiation AS frad, temperature AS ftemp, cloud_cover AS fcloud "
    "FROM weather_forecast WHERE model = 'icon_ch2' AND ts - issued BETWEEN 12 * 3600 AND 36 * 3600", conn)
forecast = forecast.sort_values("issued").groupby("ts").last().drop(columns="issued")

data = pv.join(station).join(forecast)
utc = pd.to_datetime(data.index, unit="s", utc=True)
local = utc.tz_convert(TZ)
data["hour"] = utc.hour                 # the sun follows UTC, not daylight saving time
data["doy"] = utc.dayofyear
data["day"] = local.date
data["month_key"] = local.year * 100 + local.month

# Sun elevation and hour angle (approximate), as features for gradient boosting
declination = np.radians(23.44) * np.sin(2 * np.pi * (284 + data.doy) / 365)
hour_angle = np.radians(15 * (utc.hour + 0.5 + LONGITUDE / 15 - 12))
lat = np.radians(LATITUDE)
data["elevation"] = np.degrees(np.arcsin(np.sin(lat) * np.sin(declination)
                                         + np.cos(lat) * np.cos(declination) * np.cos(hour_angle)))
data["hour_angle"] = np.degrees(hour_angle)

with_forecast = data.dropna(subset=["frad"]).copy()
print(f"{len(data)} hours of PV, {len(with_forecast)} with a day-ahead forecast")

# %% [markdown]
# ## Models

# %%
def ratio_table(train: pd.DataFrame, radiation: str, window: int = 30) -> dict:
    """Factor per (week, hour): least squares through zero over days within ±window of the week's middle."""
    table = {}
    for hour in range(24):
        at_hour = train[train.hour == hour].dropna(subset=[radiation])
        for week in range(52):
            middle = week * 7 + 4
            distance = (at_hour.doy - middle).abs()
            distance = np.minimum(distance, 365 - distance)
            near = at_hour[distance <= window]
            denominator = (near[radiation] ** 2).sum()
            table[(week, hour)] = (near.pv_wh * near[radiation]).sum() / denominator if denominator else 0.0
    return table


def apply_ratio(table: dict, test: pd.DataFrame) -> np.ndarray:
    weeks = np.minimum((test.doy - 1) // 7, 51)
    return np.array([table[(w, h)] * r for w, h, r in zip(weeks, test.hour, test.frad)])


FEATURES = ["frad", "ftemp", "fcloud", "elevation", "hour_angle", "doy"]
predictions = {name: pd.Series(index=with_forecast.index, dtype=float)
               for name in ("one factor", "ratio, station fit", "ratio, forecast fit", "gradient boosting")}

for month in sorted(with_forecast.month_key.unique()):
    test = with_forecast[with_forecast.month_key == month]
    train_all = data[data.month_key != month]
    train_forecast = with_forecast[with_forecast.month_key != month]

    observed = train_all.dropna(subset=["rad"])
    factor = (observed.pv_wh * observed.rad).sum() / (observed.rad ** 2).sum()
    predictions["one factor"][test.index] = factor * test.frad
    predictions["ratio, station fit"][test.index] = apply_ratio(ratio_table(train_all, "rad"), test)
    predictions["ratio, forecast fit"][test.index] = apply_ratio(ratio_table(train_forecast, "frad"), test)
    model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05)
    model.fit(train_forecast[FEATURES], train_forecast.pv_wh)
    predictions["gradient boosting"][test.index] = model.predict(test[FEATURES]).clip(0)

# %% [markdown]
# ## Scores

# %%
for name, predicted in predictions.items():
    hourly_mae = (with_forecast.pv_wh - predicted).abs().mean()
    daily = pd.DataFrame({"actual": with_forecast.pv_wh, "predicted": predicted, "day": with_forecast.day})
    daily = daily.groupby("day").sum() / 1000
    error = daily.predicted - daily.actual
    print(f"{name:22s} hourly MAE {hourly_mae:5.0f} Wh   daily MAE {error.abs().mean():4.1f} kWh   "
          f"bias {error.mean():+4.1f} kWh/day")
