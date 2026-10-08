"""Derive the depot inputs of the disposition model from inputs/depot_dataset.xlsx.

Primary input : inputs/depot_dataset.xlsx, one sheet per aspect of the depot site

                'consumption' the metered site load: a 'date' column
                              (DD.MM.YYYY HH:MM:SS) and a power column at the native
                              sampling rate of the meter
                'generation'  the on-site PV plant: one row with location, peak power,
                              tilt and azimuth. These are the user inputs the PVGIS
                              query of the model is built from
                'charging'    the charging stations of the depot, one row per station
                              with its id and its power

Outputs       : data/depot_load_profile.csv     (strict 30-min grid, column 'power_kW')
                data/depot_pv_parameters.csv    (one row, the PV plant of the site)
                data/depot_charging_stations.csv (one row per charging station)
                data/plot_load_profiles_full_timespan.png
                data/plot_load_profiles_full_timespan_part2.png
                data/plot_load_profiles_intraday.png (consumption sheet)

The 30-min grid matches the 48 time steps of the disposition model.
"""

# 1 SETUP
# 1.1 load modules
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import matplotlib


# 1.2 project paths
#     inputs/     the Excel datasets. Primary input, never written to.
#     data/  what this script builds from them for the model to read.
#     results/   the figures. See hdv_disposition_optimization 1.2 for why the three.
PROJECT_ROOT           = Path(__file__).resolve().parent.parent
USER_DATA_DIR          = PROJECT_ROOT / 'inputs'
WORKING_DATA_DIR       = PROJECT_ROOT / 'data'
RESULT_DATA_DIR        = PROJECT_ROOT / 'results'
DEPOT_DATASET          = USER_DATA_DIR / 'depot_dataset.xlsx'
DEPOT_LOAD_PROFILE_CSV = WORKING_DATA_DIR / 'depot_load_profile.csv'
DEPOT_PV_PARAMETER_CSV = WORKING_DATA_DIR / 'depot_pv_parameters.csv'
DEPOT_CHARGING_CSV     = WORKING_DATA_DIR / 'depot_charging_stations.csv'
CSV_ENCODING           = 'utf-8'


def ensure_working_data_dir():
    """Create data/ on demand and return it."""
    WORKING_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return WORKING_DATA_DIR


def ensure_result_data_dir():
    """Create results/ on demand and return it."""
    RESULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return RESULT_DATA_DIR


def require_input(path, produced_by=None):
    """Fail fast with an actionable message instead of a bare FileNotFoundError."""
    path = Path(path)
    if path.exists():
        return path
    hint = f" Run {produced_by} first." if produced_by else ""
    raise FileNotFoundError(f"Missing input file: {path}.{hint}")


# 1.3 headless rendering unless the module is run interactively
SHOW_PLOTS = __name__ == '__main__'
if not SHOW_PLOTS:
    matplotlib.use('Agg')
import matplotlib.pyplot as plt
from hdv_figure_style import use_figure_style
import matplotlib.dates as mdates

# 1.4 canonical power column name expected downstream
# how much of a load profile may be interpolated before it stops being a
# measurement. Gaps inside a metered period are ordinary and get closed; a sheet
# that is mostly gap is a different thing and is refused rather than filled in.
MAX_INTERPOLATED_SHARE = 0.20
POWER_COLUMN = 'power_kW'
DATE_COLUMN = 'date'

# 1.5 the measured power column as the 'consumption' sheet spells it
POWER_COLUMN_INPUT = 'power_kw'

# 1.6 sheets of the primary input that are not metered load profiles
#     'consumption' is the site load the model uses as its baseline; 'generation'
#     describes the PV plant and 'charging' the chargers, so neither is resampled.
LOAD_SHEET = 'consumption'
PV_SHEET = 'generation'
CHARGING_SHEET = 'charging'
NON_LOAD_SHEETS = (PV_SHEET, CHARGING_SHEET)

# 1.7 PV plant of the site: canonical name in the derived CSV -> column in the Excel.
#     One spelling each, exactly what sheet 'generation' provides. All five are required;
#     a missing one is a fault in the dataset, not something to substitute a default for.
PV_PARAMETER_COLUMNS = {
    'pv_latitude_deg':  'pv_latlocation_deg',
    'pv_longitude_deg': 'pv_lonlocation_deg',
    'pv_peak_power_kW': 'pv_peakpower_kw',
    'pv_tilt_deg':      'pv_tilt_deg',
    'pv_azimuth_deg':   'pv_azimuth_deg',
}

# 1.8 charging stations of the depot: canonical name -> column in sheet 'charging'.
CHARGING_COLUMNS = {
    'charger_id':       'charger_id',
    'charger_power_kW': 'charger_power_kw',
}

# 1.9 font selection
# every figure is written as PNG. A raster keeps the browser fast: the Streamlit
# page lays the figures out again on every interaction, and a vector plot of a
# dense schedule costs it thousands of DOM nodes each time. 150 dpi so the raster
# still holds up when zoomed.
FIGURE_DPI = 150





# 2 PREPROCESSING
# 2.1 locate the timestamp and power columns of a raw sheet
def identify_columns(df):
    """The timestamp and power column of a load sheet, by name.

    Both are named, so both are looked up by name and a sheet that does not carry them is
    rejected. Guessing - first column as the timestamp, first numeric one as the power -
    turns a renamed or reordered column into a silently wrong load profile.
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    lowered = {c.lower(): c for c in df.columns}

    missing = [name for name in (DATE_COLUMN, POWER_COLUMN_INPUT) if name not in lowered]
    if missing:
        raise ValueError(
            f"Load sheet of {DEPOT_DATASET.name} lacks the column(s) {missing}; "
            f"columns present: {list(df.columns)}."
        )
    return df, lowered[DATE_COLUMN], lowered[POWER_COLUMN_INPUT]


# 2.2 infer the native sampling step as the median of the time deltas
def infer_native_step(timestamps):
    deltas = timestamps.sort_values().diff().dropna()
    deltas = deltas[deltas > pd.Timedelta(0)]
    if deltas.empty:
        return pd.NaT
    return pd.to_timedelta(deltas.median())


# 2.3 parse timestamps, resample to a strict 30-minute grid starting at 00:00
def to_30min_grid(df, agg='mean'):
    """Return a single-column DataFrame ['power_kW'] on a gap-free 30-min index."""
    df, date_col, power_col = identify_columns(df)

    # the dataset states DD.MM.YYYY HH:MM:SS; anything else is a fault in the sheet
    parsed = pd.to_datetime(df[date_col], format='%d.%m.%Y %H:%M:%S', errors='coerce')
    df = df.assign(**{date_col: parsed})

    unparsed = int(df[date_col].isna().sum())
    duplicates = int(df[date_col].duplicated().sum())

    out = (
        df.dropna(subset=[date_col])
          .drop_duplicates(subset=[date_col])
          .set_index(date_col)
          .sort_index()
    )
    if out.empty:
        raise ValueError("Depot sheet contains no parsable timestamps.")

    native_step = infer_native_step(pd.Series(out.index))
    out = out[[power_col]].rename(columns={power_col: POWER_COLUMN})
    out[POWER_COLUMN] = pd.to_numeric(out[POWER_COLUMN], errors='coerce')

    # resample to a strict 30-min grid with bins anchored at start-of-day
    resampled = out.resample('30min', origin='start_day', label='left', closed='left')
    out_30 = resampled.sum() if agg == 'sum' else resampled.mean(numeric_only=True)

    # complete every calendar day that has readings, and no others.
    #
    # Each day present is filled out to its own 48 steps, 00:00 up to but not including the
    # next midnight - that last midnight would otherwise add a 49th step to the first date
    # once the year is ignored, and the model matches its depot day on day and month, so
    # 01.01 00:00 of the following year would land in the same bucket as this one and
    # quietly average two days' worth into one step.
    #
    # Per day, not one range from the first reading to the last. A sheet is not required to
    # be a contiguous block: the shipped one is a metered year plus a single 29 February
    # borrowed from a leap year, because the model needs that month-day and a non-leap year
    # cannot supply it. Spanning min to max there would build a grid of three years, leave
    # 68 % of it empty and then *interpolate* the emptiness - inventing two years of depot
    # load and averaging it into every day of every run. Days that are absent are absent;
    # only days that exist are completed.
    # from the readings, not from out_30: resample() already spans its own index
    # end to end, so asking it which days exist answers with every day in between
    days = pd.Index(sorted({stamp.normalize() for stamp in out.index}))
    full_grid = pd.DatetimeIndex(
        [day + pd.Timedelta(minutes=30 * step) for day in days for step in range(48)])
    out_30 = out_30.reindex(full_grid)

    # close the gaps left inside a day: time-weighted interpolation, constant hold at the
    # edges. Bounded, and the bound is the point - a profile that is mostly invented is not
    # a measurement, and it drives the demand-charge peak and how much PV the site eats
    # before a truck sees any. Reported as a count either way; refused past the share.
    gaps = int(out_30[POWER_COLUMN].isna().sum())
    if gaps:
        share = gaps / max(len(out_30), 1)
        if share > MAX_INTERPOLATED_SHARE:
            raise ValueError(
                f"{gaps} of {len(out_30)} half-hour steps ({share:.0%}) of this load sheet "
                f"have no reading behind them. Interpolating that much would invent most "
                f"of the profile rather than close gaps in it. Check the sheet for missing "
                f"periods, or raise MAX_INTERPOLATED_SHARE if a sparse profile really is "
                f"intended.")
        out_30 = out_30.interpolate(method='time', limit_direction='both')
        out_30 = out_30.ffill().bfill()

    out_30.index.name = DATE_COLUMN
    stats = {
        'unparsed_timestamps': unparsed,
        'duplicate_timestamps': duplicates,
        'native_step': native_step,
        'interpolated_steps': gaps,
    }
    return out_30, stats


# 2.4 read one PV parameter from the 'generation' sheet
def _pv_value(row, canonical):
    """Value of one PV parameter. Every one of them is required."""
    column = PV_PARAMETER_COLUMNS[canonical]
    if column not in row.index or pd.isna(row[column]):
        raise ValueError(
            f"Sheet '{PV_SHEET}' of {DEPOT_DATASET.name} has no value for {canonical}. "
            f"It is read from the column '{column}'."
        )
    return float(row[column])


# 2.5 read the PV plant of the site from the 'generation' sheet
def read_depot_pv_parameters():
    """PV plant of the depot as a dict, from inputs/depot_dataset.xlsx sheet 'generation'.

    These five numbers are the user inputs the model's PVGIS query is built from, so
    they are validated here rather than being passed on to the API unchecked.
    """
    require_input(DEPOT_DATASET)
    sheets = pd.read_excel(DEPOT_DATASET, sheet_name=None)
    if PV_SHEET not in sheets:
        raise ValueError(
            f"{DEPOT_DATASET.name} has no sheet '{PV_SHEET}'. It states the PV plant of "
            f"the depot (location, peak power, tilt, azimuth); sheets present: "
            f"{', '.join(sheets)}."
        )

    sheet = sheets[PV_SHEET].copy()
    sheet.columns = [str(c).strip().lower() for c in sheet.columns]
    sheet = sheet.dropna(how='all')
    if sheet.empty:
        raise ValueError(f"Sheet '{PV_SHEET}' of {DEPOT_DATASET.name} contains no rows.")
    row = sheet.iloc[0]

    parameters = {name: _pv_value(row, name) for name in PV_PARAMETER_COLUMNS}

    # a wrong sign or a swapped lat/lon silently moves the plant to another climate,
    # so the ranges are checked before the value ever reaches PVGIS
    if not -90.0 <= parameters['pv_latitude_deg'] <= 90.0:
        raise ValueError(f"pv latitude {parameters['pv_latitude_deg']} is outside -90...90.")
    if not -180.0 <= parameters['pv_longitude_deg'] <= 180.0:
        raise ValueError(f"pv longitude {parameters['pv_longitude_deg']} is outside -180...180.")
    if parameters['pv_peak_power_kW'] <= 0:
        raise ValueError(
            f"pv peak power {parameters['pv_peak_power_kW']} kWp must be > 0. Remove the "
            f"'{PV_SHEET}' sheet only if the depot has no PV plant at all."
        )
    if not 0.0 <= parameters['pv_tilt_deg'] <= 90.0:
        raise ValueError(f"pv tilt {parameters['pv_tilt_deg']}° is outside 0...90 "
                         "(0 = horizontal, 90 = vertical).")
    # compass azimuth, 0 = north, 90 = east, 180 = south, 270 = west
    parameters['pv_azimuth_deg'] = float(parameters['pv_azimuth_deg']) % 360.0

    return parameters


# 2.6 read the charging stations of the depot from the 'charging' sheet
def _station_id(value):
    """Station label as it will appear in the schedule and the result table.

    Excel hands whole numbers over as floats as soon as one cell of the column is blank,
    so a plain str() would label station 1 'charging_station_1.0'.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        raise ValueError(
            f"Sheet '{CHARGING_SHEET}' of {DEPOT_DATASET.name} has a station without an id."
        )
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    label = str(value).strip()
    if not label:
        raise ValueError(
            f"Sheet '{CHARGING_SHEET}' of {DEPOT_DATASET.name} has a station without an id."
        )
    return label


def read_depot_charging_stations():
    """Charging stations of the depot as a DataFrame [charger_id, charger_power_kW].

    One row per station, in sheet order: the depot's charging infrastructure is this
    list, used verbatim. The model builds one charging opportunity per station and step
    from it, so the number of rows is the number of stations and each row's power is the
    cap of that station - nothing about the infrastructure is synthesized.
    """
    require_input(DEPOT_DATASET)
    sheets = pd.read_excel(DEPOT_DATASET, sheet_name=None)
    if CHARGING_SHEET not in sheets:
        raise ValueError(
            f"{DEPOT_DATASET.name} has no sheet '{CHARGING_SHEET}'. It lists the charging "
            f"stations of the depot, one row per station; sheets present: "
            f"{', '.join(sheets)}."
        )

    sheet = sheets[CHARGING_SHEET].copy()
    sheet.columns = [str(c).strip().lower() for c in sheet.columns]
    sheet = sheet.dropna(how='all')
    if sheet.empty:
        raise ValueError(
            f"Sheet '{CHARGING_SHEET}' of {DEPOT_DATASET.name} lists no charging station. "
            "The depot needs at least one."
        )

    missing = [column for column in CHARGING_COLUMNS.values() if column not in sheet.columns]
    if missing:
        raise ValueError(
            f"Sheet '{CHARGING_SHEET}' of {DEPOT_DATASET.name} lacks the column(s) "
            f"{missing}; columns present: {list(sheet.columns)}."
        )

    stations = pd.DataFrame({
        'charger_id': [_station_id(v) for v in sheet[CHARGING_COLUMNS['charger_id']]],
        'charger_power_kW': pd.to_numeric(sheet[CHARGING_COLUMNS['charger_power_kW']],
                                          errors='coerce'),
    }).reset_index(drop=True)

    # a blank or non-positive power would silently become a station that cannot charge
    invalid = stations[stations['charger_power_kW'].isna() | (stations['charger_power_kW'] <= 0)]
    if not invalid.empty:
        raise ValueError(
            f"Sheet '{CHARGING_SHEET}' of {DEPOT_DATASET.name}: charger(s) "
            f"{list(invalid['charger_id'])} have no positive power. Remove the row or "
            "state its kW."
        )
    duplicates = stations['charger_id'][stations['charger_id'].duplicated()].unique()
    if len(duplicates):
        raise ValueError(
            f"Sheet '{CHARGING_SHEET}' of {DEPOT_DATASET.name}: charger id(s) "
            f"{list(duplicates)} appear more than once. Every station needs its own id."
        )

    return stations



# 3 PROCESSING
# 3.1 convert the load sheets of the primary input
def build_depot_load_profiles():
    require_input(DEPOT_DATASET)
    ensure_working_data_dir()

    dfs_raw = pd.read_excel(DEPOT_DATASET, sheet_name=None)
    if not dfs_raw:
        raise ValueError(f"{DEPOT_DATASET.name} contains no sheets.")

    # only the metered sheets are load profiles. 'generation' and 'charging' describe
    # the site instead of measuring it, and resampling them onto a 30-min grid would
    # turn their row numbers into timestamps.
    load_sheets = {name: df for name, df in dfs_raw.items()
                   if str(name).strip().lower() not in NON_LOAD_SHEETS}
    if not load_sheets:
        raise ValueError(
            f"{DEPOT_DATASET.name} has no load sheet. The metered site load belongs in a "
            f"sheet named '{LOAD_SHEET}'; sheets present: {', '.join(dfs_raw)}."
        )

    profiles_30 = {}
    for name, df in load_sheets.items():
        df30, stats = to_30min_grid(df)
        profiles_30[name] = df30
        print(f"depot sheet '{name}': {len(df)} raw rows -> {len(df30)} 30-min steps "
              f"({df30.index.min().date()} ... {df30.index.max().date()}, "
              f"native step {stats['native_step']}, "
              f"{stats['duplicate_timestamps']} duplicate / {stats['unparsed_timestamps']} unparsable "
              f"timestamps dropped, {stats['interpolated_steps']} steps interpolated)")

    # 3.2 the canonical sheet is the one the model reads as its baseline
    canonical = next((name for name in profiles_30
                      if str(name).strip().lower() == LOAD_SHEET), None)
    if canonical is None:
        raise ValueError(
            f"{DEPOT_DATASET.name} has no sheet '{LOAD_SHEET}'. That sheet is the metered "
            f"site load the model uses as its baseline; load sheets present: "
            f"{', '.join(str(n) for n in profiles_30)}."
        )
    # 3.2b one file, one column.
    #
    # There used to be a CSV per load sheet *plus* a copy of the canonical one under the
    # fixed name the model reads, so a single load sheet produced two files with identical
    # bytes and nothing ever read the per-sheet copy. The workbook carries one metered load
    # sheet - 'consumption', the site's own draw - and that is the only profile the model
    # has a use for, so this is the only file written. A second load sheet would still be
    # read, resampled and plotted above; it simply has no CSV of its own, because nothing
    # would read it.
    combined = profiles_30[canonical].copy()
    combined[POWER_COLUMN] = combined[POWER_COLUMN].round(2)
    combined.to_csv(DEPOT_LOAD_PROFILE_CSV, index=True, index_label=DATE_COLUMN,
                    float_format='%.2f', encoding=CSV_ENCODING)

    print(f"  -> {DEPOT_LOAD_PROFILE_CSV.relative_to(PROJECT_ROOT)}")

    return profiles_30


# 3.3 write the PV plant of the site as a derived one-row table
def build_depot_pv_parameters():
    """Turn the 'generation' sheet into data/depot_pv_parameters.csv.

    The model reads the PV site from that derived file, exactly as it reads the depot
    baseline load and the energy prices from theirs - the Excel stays the single source.
    """
    ensure_working_data_dir()
    parameters = read_depot_pv_parameters()
    pd.DataFrame([parameters]).to_csv(DEPOT_PV_PARAMETER_CSV, index=False,
                                      encoding=CSV_ENCODING)
    print(f"depot sheet '{PV_SHEET}': PV plant {parameters['pv_peak_power_kW']:g} kWp at "
          f"{parameters['pv_latitude_deg']:.3f}/{parameters['pv_longitude_deg']:.3f}, "
          f"tilt {parameters['pv_tilt_deg']:g}°, azimuth {parameters['pv_azimuth_deg']:g}°")
    print(f"  -> {DEPOT_PV_PARAMETER_CSV.relative_to(PROJECT_ROOT)}")
    return parameters


# 3.4 write the charging stations of the site as a derived table
def build_depot_charging_stations():
    """Turn the 'charging' sheet into data/depot_charging_stations.csv.

    The model reads its charging infrastructure from that derived file, so the station
    list is a property of depot_dataset.xlsx and not a run parameter.
    """
    ensure_working_data_dir()
    stations = read_depot_charging_stations()
    stations.to_csv(DEPOT_CHARGING_CSV, index=False, encoding=CSV_ENCODING)
    powers = stations['charger_power_kW']
    print(f"depot sheet '{CHARGING_SHEET}': {len(stations)} charging stations, "
          f"{powers.sum():g} kW installed "
          f"({', '.join(f'{p:g}' for p in powers)} kW)")
    print(f"  -> {DEPOT_CHARGING_CSV.relative_to(PROJECT_ROOT)}")
    return stations



# 4 OUTPUT
# 4.1 full-timespan plots, split into two figures for readability
def plot_full_timespan(combined, output_dir):
    use_figure_style(plt)
    sheet_names = list(combined.columns)
    mid = (len(sheet_names) + 1) // 2
    groups = [sheet_names[:mid], sheet_names[mid:]]

    for i, cols in enumerate(groups, start=1):
        if not cols:
            continue
        fig, ax = plt.subplots(figsize=(12, 7))
        combined[cols].plot(ax=ax, linewidth=1)
        ax.set_title(f"Depot load profiles - full timespan (part {i})", y=1.02, fontweight="bold")
        ax.set_xlabel("date [YYYY-MM]")
        ax.set_ylabel("power [kW]")
        ax.grid(True, alpha=0.3)
        ax.legend(title="sheet", ncol=2, fontsize=9)
        fig.autofmt_xdate()
        filename = ("plot_load_profiles_full_timespan.png" if i == 1
                    else f"plot_load_profiles_full_timespan_part{i}.png")
        fig.savefig(output_dir / filename, bbox_inches="tight", dpi=FIGURE_DPI)


# 4.2 intraday overlay per depot with a 00:00-24:00 axis
def plot_intraday_overlay(combined, output_dir):
    use_figure_style(plt)
    day_start = pd.Timestamp(2000, 1, 1, 0, 0)
    day_end = pd.Timestamp(2000, 1, 2, 0, 0)  # allows showing up to the "24:00" boundary

    def to_dummy_tod(idx):
        return pd.to_datetime(idx.strftime("2000-01-01 %H:%M"))

    for name in combined.columns:
        s = combined[name].dropna()
        if s.empty:
            continue

        fig, ax = plt.subplots(figsize=(12, 7))

        # overlay each day's curve; append an explicit 24:00 point equal to the next day's 00:00
        for day, group in s.groupby(s.index.normalize()):
            x = list(to_dummy_tod(group.index))
            y = list(group.values)
            next_midnight = day + pd.Timedelta(days=1)
            if next_midnight in s.index and pd.notna(s.loc[next_midnight]):
                x.append(day_end)
                y.append(float(s.loc[next_midnight]))
            ax.plot(x, y, color="tab:blue", alpha=0.2, linewidth=0.8)

        # central tendency for orientation: median per 30-min slot
        diurnal_median = s.groupby([s.index.hour, s.index.minute]).median()
        diurnal_median.index = [pd.Timestamp(2000, 1, 1, h, m) for h, m in diurnal_median.index]
        if day_start in diurnal_median.index:
            diurnal_median.loc[day_end] = float(diurnal_median.loc[day_start])
            diurnal_median = diurnal_median.sort_index()
        ax.plot(diurnal_median.index, diurnal_median.values, color="black", linewidth=2.0, label="median")

        ax.set_title(f"{name} - intraday overlay (all days, 30-min, 24:00 added)", y=1.02, fontweight="bold")
        ax.set_xlabel("Time of day [hh:mm]")
        ax.set_ylabel("power [kW]")
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 25, 2)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.set_xlim(day_start, day_end)
        ax.grid(True, alpha=0.3)

        slug = ''.join(ch if ch.isalnum() else '_' for ch in str(name)).strip('_').lower()
        filename = ("plot_load_profiles_intraday.png" if slug == "consumption"
                    else f"plot_load_profiles_intraday_all_days_{slug}.png")
        fig.savefig(output_dir / filename,
                    bbox_inches="tight", dpi=FIGURE_DPI)


# 4.3 pipeline entry point
def generate_depot_load_profile(make_plots=True):
    profiles_30 = build_depot_load_profiles()
    build_depot_pv_parameters()
    build_depot_charging_stations()
    if make_plots:
        output_dir = ensure_working_data_dir()
        combined = pd.DataFrame({name: df[POWER_COLUMN] for name, df in profiles_30.items()}).sort_index()
        plot_full_timespan(combined, output_dir)
        plot_intraday_overlay(combined, output_dir)
    return profiles_30



# 5 MAIN
if __name__ == '__main__':
    generate_depot_load_profile()
    plt.show()
