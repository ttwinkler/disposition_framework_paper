"""Derive the energy cost parameters of the disposition model from inputs/costs_dataset.xlsx.

Primary input : inputs/costs_dataset.xlsx
                  sheet 'energy_yearly' -> year        x {low, medium, high}
                  sheet 'energy_daily'  -> hour (1-24), one column per price, under an
                                           optional note row naming the day it describes
                Neither sheet's header position is assumed - it is located by what it
                contains (2.0), so a note row above the headings or a missing band row
                keeps loading, and 'hour'/'Hour' are the same column name.
                Both sheets carry the same four price groups:
                  electricity_spot_price_€/kWh   (public spot price of electricity)
                  flexibility_spot_price_€/kWh   (public spot price of flexibility)
                  public_charging_price_€/kWh
                  public_diesel_price_€/l
                The first two were called energy_spot_price / private_charging_price and
                public_flexibility_price before. Those names no longer load: one heading
                per price, see COLUMN_GROUPS.

                The two sheets are shaped differently and are meant to be. 'energy_yearly'
                is an outlook, so it brackets every year with a low/medium/high band over
                a two-row header. 'energy_daily' is one operating day, so it states ONE
                series per price and no band: there is a single intraday shape, and the
                scenario decides the level it sits at rather than the shape itself. That
                one series is read as all three bands (2.0/2.1), so everything downstream
                keeps one structure.

                The shape of the two price curves - electricity spot and flexibility spot
                - therefore always comes from 'energy_daily'. Everything else about a
                run's prices is its energy_price_basis (disposition model 1.4b2):

                  'daily'   all four prices out of 'energy_daily' as written. The curves
                            keep their own level as well as their shape; the public
                            charger and the diesel are that sheet's. A disposition run.
                  'yearly'  the scenario year and band of 'energy_yearly' sets the level -
                            the curves rescaled onto it, the other two read straight off
                            it. A sizing run and a sweep.

                Only on the second path does the year or the scenario band reach a price
                at all, which is why both files are written in full here: one carries the
                day, the other the outlook, and which one a run reads is the model's to
                decide.

Outputs : data/cost_parameter_yearly.csv (yearly, €/kWh, €/l and €/MWh -
                                                       native units; the disposition
                                                       model converts them to a cost per
                                                       km with each vehicle's own
                                                       consumption)
                data/cost_parameter_hourly.csv  (hourly: the whole of sheet
                                                 'energy_daily' in model units)
                data/plot_cost_parameter_yearly.png
                data/plot_cost_parameter_hourly.png
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
PROJECT_ROOT              = Path(__file__).resolve().parent.parent
USER_DATA_DIR             = PROJECT_ROOT / 'inputs'
WORKING_DATA_DIR          = PROJECT_ROOT / 'data'
RESULT_DATA_DIR           = PROJECT_ROOT / 'results'
ENERGY_DATASET = USER_DATA_DIR / 'costs_dataset.xlsx'
COST_PARAMETER_ENERGY_CSV = WORKING_DATA_DIR / 'cost_parameter_yearly.csv'
COST_PARAMETER_DAILY_CSV  = WORKING_DATA_DIR / 'cost_parameter_hourly.csv'
CSV_ENCODING              = 'utf-8'


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

# 1.4 the dataset labels the three scenario columns low/medium/high;
#     the model consumes them as min/mean/max
SCENARIO_LEVELS = {'low': 'min', 'medium': 'mean', 'high': 'max'}

# the column groups of costs_dataset.xlsx, mapped to the names everything downstream
# uses. Both sheets state prices ('..._price_...'), while the derived CSVs and the
# disposition model speak of the cost of buying energy and the earnings from selling it,
# so the two vocabularies meet here and nowhere else: relabel a heading in the Excel and
# this dict is the only thing that has to follow.
# Matched by prefix, so the '€' and the unit in the heading are never relied on.
# Order in the sheet is irrelevant - the groups are found by heading, not by position.
#
# One prefix per group, and that is the point. There used to be three more - a misspelled
# 'eneryg_spot_price_', and 'private_charging_price_' / 'public_flexibility_price_' from
# before the headings were renamed to say what they are - kept so a workbook mid-rename
# still loaded. Two names for one price is a hazard rather than a convenience: a sheet
# carrying both resolves to whichever the reader meets first, which is column order
# deciding a price. read_scenario_sheet() below still refuses that case if it ever arises,
# but with one name per group it cannot. The shipped workbook is on the current names;
# a sheet on an old one now fails with "missing the column group(s) ...", naming what to
# rename, which is a better answer than loading and being quietly ambiguous.
COLUMN_GROUPS = {
    # the heading says 'electricity' because that is what it is - the public spot price of
    # electricity. The internal name stays energy_spot_price: it is what the derived CSV
    # columns and the disposition model are written against, and renaming a heading is not
    # a reason to rename a schema. This dict is the join between the two, which is the
    # whole point of it.
    'electricity_spot_price_':    'energy_spot_price',
    'flexibility_spot_price_':    'flexibility_price',
    'public_charging_price_':     'public_charging_cost',
    'public_diesel_price_':       'public_diesel_cost',
}

# what each group is called in the sheet now, for error messages: the first prefix above
# that maps to it, which is the canonical one
CANONICAL_HEADING = {}
for _prefix, _group in COLUMN_GROUPS.items():
    CANONICAL_HEADING.setdefault(_group, _prefix)

# 1.4b the two channels a truck can sell a kWh into, and the column each is priced from.
# Arbitrage trades the energy itself, so it settles at the electricity spot price - the
# same curve the truck buys at, which is why its profit is the intraday spread and not a
# margin handed to it by a second price series. Flexibility is paid for the service
# rather than for the energy and has its own spot curve.
V2G_CHANNELS = {
    'arbitrage':   'energy_spot_price',
    'flexibility': 'flexibility_price',
}

# 1.4c the two prices with no intraday shape, and the column each lands in. They are not
# curves and the model never treats them as one - it bills a single number per run - but
# they ARE stated per hour in 'energy_daily', and a disposition run is priced off that
# sheet entire (disposition model 1.4b2). So they are carried over beside the curves, in
# their own units, and the model averages each back to the one number it bills.
FLAT_DAILY_PRICES = {
    'public_charging_cost': 'public_charging_cost_€/kWh',
    'public_diesel_cost':   'public_diesel_cost_€/l',
}

# 1.5 font selection
# every figure is written as PNG. A raster keeps the browser fast: the Streamlit
# page lays the figures out again on every interaction, and a vector plot of a
# dense schedule costs it thousands of DOM nodes each time. 150 dpi so the raster
# still holds up when zoomed.
FIGURE_DPI = 150





# 2 PREPROCESSING
# 2.0 WHERE A SHEET'S HEADER IS, found rather than assumed.
#
# The two sheets do not have the same shape and are not meant to. 'energy_yearly' is an
# outlook, so it brackets every year with a low/medium/high band under each price heading.
# 'energy_daily' is one operating day: one series per price, no band - there is a single
# shape and the scenario decides the LEVEL it sits at, not the shape (disposition model
# 1.4b2) - and it carries a note row above the headings saying which day it is.
#
# Rather than keep a layout flag beside the file, or a `header=[0, 1]` that is right for
# one sheet and silently eats a row of the other, the header is located by what it
# contains. Two facts settle any of the layouts:
#
#   the heading row  the row carrying the COLUMN_GROUPS prefixes. Anything above it is a
#                    note - a date, a source, a comment - and is not data.
#   the band row     the row directly below it, when that row names low/medium/high.
#                    Otherwise there is no band and the one column feeds all three levels.
#
# Everything else follows: data starts after whichever of those is last, and the index
# column is whichever column carries the index name, in the heading row or in a note row
# above it. A sheet that grows another note row, or loses its band, keeps loading.
HEADER_SCAN_ROWS = 8          # how far down to look for the heading row


def _cell_text(value):
    """One sheet cell as comparable text; '' for an empty cell."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ''
    return str(value).strip()


def locate_sheet_header(raw, sheet_name, workbook_name):
    """Find (heading_row, band_row_or_None, first_data_row) in a raw-read sheet. See 2.0."""
    heading_row = None
    for row in range(min(HEADER_SCAN_ROWS, len(raw))):
        texts = [_cell_text(v) for v in raw.iloc[row].tolist()]
        if any(text.startswith(prefix) for text in texts for prefix in COLUMN_GROUPS):
            heading_row = row
            break
    if heading_row is None:
        wanted = sorted(f"'{prefix}...'" for prefix in COLUMN_GROUPS)
        raise ValueError(
            f"Sheet '{sheet_name}' of {workbook_name} has no price headings in its first "
            f"{min(HEADER_SCAN_ROWS, len(raw))} row(s). One row has to carry the column "
            f"groups {', '.join(wanted)}; rows above it are read as notes.")
    band_row = None
    if heading_row + 1 < len(raw):
        below = {_cell_text(v).lower() for v in raw.iloc[heading_row + 1].tolist()}
        if below & set(SCENARIO_LEVELS):
            band_row = heading_row + 1
    return heading_row, band_row, (band_row if band_row is not None else heading_row) + 1


def has_scenario_subheader(sheet_name, workbook=None):
    """True when this sheet brackets its prices with a low/medium/high band. See 2.0.

    workbook defaults to the energy dataset. It is a parameter because the web interface
    displays these sheets and has to read them the way the model does - one rule, called
    from both, rather than a second copy of it over there that could come to disagree with
    this one about what a sheet looks like.
    """
    workbook = workbook or ENERGY_DATASET
    raw = pd.read_excel(workbook, sheet_name, header=None, nrows=HEADER_SCAN_ROWS + 1)
    return locate_sheet_header(raw, sheet_name, Path(workbook).name)[1] is not None


def sheet_note(sheet_name, workbook=None):
    """Whatever the rows above the headings say, as one line - '' when there are none.

    'energy_daily' uses it to state the date its prices belong to. Nothing is parsed out
    of it and nothing is decided by it: the day a run plans is `date_disposition`, which
    also picks the trips, the depot load and the PV curve, and a note in a price sheet is
    in no position to overrule those. It is carried through to be shown, so that a curve
    labelled for one day and a run made for another is visible rather than discoverable.
    """
    workbook = workbook or ENERGY_DATASET
    raw = pd.read_excel(workbook, sheet_name, header=None, nrows=HEADER_SCAN_ROWS + 1)
    heading_row, _band, _data = locate_sheet_header(raw, sheet_name, Path(workbook).name)
    notes = []
    for row in range(heading_row):
        texts = [t for t in (_cell_text(v) for v in raw.iloc[row].tolist()) if t]
        notes.extend(texts)
    # the index name sits in the note row's first cell on the daily sheet ('hour'), and it
    # is a column label rather than a note about the sheet
    return ' | '.join(notes[1:] if len(notes) > 1 else notes)


def read_sheet_table(sheet_name, workbook=None):
    """The sheet as a flat table for display: index column first, one column per price.

    The same header scan the model reads through (2.0), so a page showing a sheet and a
    run reading it cannot disagree about where its data starts. Without this the display
    needed its own `header=` guess, and the guess that is right for the yearly sheet eats
    the first hour of the daily one.
    """
    workbook = workbook or ENERGY_DATASET
    raw = pd.read_excel(workbook, sheet_name, header=None)
    heading_row, band_row, first_data_row = locate_sheet_header(
        raw, sheet_name, Path(workbook).name)
    headings = list(pd.Series([_cell_text(v) for v in raw.iloc[heading_row].tolist()])
                    .replace('', pd.NA).ffill().fillna(''))
    labels = []
    for position in range(len(raw.columns)):
        heading = headings[position] if position < len(headings) else ''
        band = _cell_text(raw.iat[band_row, position]) if band_row is not None else ''
        above = next((_cell_text(raw.iat[row, position])
                      for row in range(heading_row) if _cell_text(raw.iat[row, position])),
                     '')
        name = heading or above or f'column {position + 1}'
        labels.append(f'{name} ({band})' if band else name)
    table = raw.iloc[first_data_row:].reset_index(drop=True)
    table.columns = labels
    return table


# 2.1 read one sheet into a flat {(group, level): Series} structure
def read_scenario_sheet(sheet_name, index_column):
    """Read one costs_dataset sheet, banded or not, into min/mean/max series.

    Returns (index_series, {group_prefix: {min|mean|max: Series}}).
    Group keys are matched by prefix so the '€' in the header is never relied on, and
    index_column is matched without regard to case, so 'Hour' and 'hour' both load.

    A sheet with no low/medium/high band (2.0) states ONE series per price, and that one
    series is returned as all three levels. It is the same curve whichever scenario a run
    is made under - which is what a single daily curve means: the shape of a day does not
    change between best case and worst case, only the level it is scaled to does, and that
    level comes from 'energy_yearly'. Returning it three times rather than teaching every
    caller about a fourth shape keeps the rest of this file, the derived CSVs and the
    disposition model on one structure.
    """
    raw = pd.read_excel(ENERGY_DATASET, sheet_name, header=None)
    heading_row, band_row, first_data_row = locate_sheet_header(
        raw, sheet_name, ENERGY_DATASET.name)
    banded = band_row is not None
    # merged cells leave the heading only in the first column of each group, so it is
    # carried forward across the band columns that belong to it
    headings = list(pd.Series([_cell_text(v) for v in raw.iloc[heading_row].tolist()])
                    .replace('', pd.NA).ffill().fillna(''))
    bands = ([_cell_text(v).lower() for v in raw.iloc[band_row].tolist()] if banded
             else [''] * len(headings))
    body = raw.iloc[first_data_row:].reset_index(drop=True)

    index_series = None
    groups = {}
    # which heading each group was actually taken from, so a sheet that still carries a
    # column under its old name *and* under the new one is caught rather than silently
    # resolved by column order - during a rename that is exactly the likely mistake
    seen_heading = {}
    for position in range(len(raw.columns)):
        header = headings[position] if position < len(headings) else ''
        level = bands[position] if position < len(bands) else ''
        # the index column is named in the heading row or in a note row above it - the
        # daily sheet puts 'hour' beside the date, one row up from the price headings
        column_names = {_cell_text(raw.iat[row, position]).lower()
                        for row in range(heading_row + 1)}
        if index_column.strip().lower() in column_names:
            index_series = pd.to_numeric(body[position], errors='coerce')
            continue
        if banded and level not in SCENARIO_LEVELS:
            continue
        # one column feeding one level, or one column feeding all three
        target_levels = [SCENARIO_LEVELS[level]] if banded else ['min', 'mean', 'max']
        for prefix, group in COLUMN_GROUPS.items():
            if header.startswith(prefix):
                for target in target_levels:
                    previous = seen_heading.setdefault((group, target), header)
                    if previous != header:
                        raise ValueError(
                            f"Sheet '{sheet_name}' of {ENERGY_DATASET.name} carries the "
                            f"same price under two headings: '{previous}' and '{header}' "
                            f"both read as '{group}'"
                            f"{f' ({level})' if banded else ''}. Keep one - the current "
                            f"name is '{CANONICAL_HEADING[group]}...' - and delete the "
                            f"other.")
                    # copied per level: without it one Series object would stand in for
                    # all three, and a caller scaling one band in place would move the
                    # other two with it
                    groups.setdefault(group, {})[target] = \
                        pd.to_numeric(body[position], errors='coerce').copy()
                break

    if index_series is None:
        raise ValueError(
            f"Sheet '{sheet_name}' of {ENERGY_DATASET.name} has no '{index_column}' "
            f"column. It is looked for in the price-heading row and in any note row above "
            f"it, without regard to case.")

    missing = set(COLUMN_GROUPS.values()) - set(groups)
    if missing:
        # named by the heading the sheet has to carry, not by the internal group name -
        # the reader has to go and fix a column in the Excel, so that is what is quoted.
        # The layout this was read as is named too: a sheet whose band row was deleted
        # reads as the other kind, and "missing column group" is a puzzling way to hear it.
        wanted = sorted(f"'{CANONICAL_HEADING[group]}...'" for group in missing)
        layout = ("over a low/medium/high band" if banded else
                  "as a single column each (no low/medium/high row under the headings, so "
                  "the sheet was read one column per price)")
        raise ValueError(
            f"Sheet '{sheet_name}' of {ENERGY_DATASET.name} is missing the column "
            f"group(s) {', '.join(wanted)} {layout}.")
    for group, levels in groups.items():
        absent = {'min', 'mean', 'max'} - set(levels)
        if absent:
            raise ValueError(f"Column group '{group}' in sheet '{sheet_name}' lacks the level(s): {sorted(absent)}")

    keep = index_series.notna()
    index_series = index_series[keep].reset_index(drop=True)
    groups = {g: {lvl: s[keep].reset_index(drop=True) for lvl, s in levels.items()}
              for g, levels in groups.items()}

    # 2.1b a blank cell is not a price, and it is the one way this file could be wrong
    #      quietly.
    #
    # Everything else about these sheets is checked by name and fails here, in the reader,
    # with a sentence saying what to edit. A blank or non-numeric cell did not: pd.to_numeric
    # coerces it to NaN, NaN is written to the derived CSV, the model reads it, and the run
    # dies hundreds of lines later inside the solver with
    #
    #     GurobiError: Multiplier is Nan or Inf
    #
    # which names neither the workbook, nor the sheet, nor the cell. The run never produced
    # a wrong answer - but it also never said where to look. So the check belongs here,
    # where the sheet, the column and the index value are all still in hand, and it names
    # all three.
    #
    # Checked after the index filter above, so trailing empty rows of the sheet - which
    # have no index value either - are not reported as missing prices. Every cell under a
    # price heading, on a row the sheet gives an index to, has to be a number.
    # One line per column of the sheet, not per level: without a band the three levels are
    # the same column read three times (2.1), and reporting one blank cell three times
    # reads as three faults.
    blanks = []
    for group, levels in sorted(groups.items()):
        for level in (('min', 'mean', 'max') if banded else ('min',)):
            where = levels[level].isna()
            if not where.any():
                continue
            at = [f'{index_column}={v:g}' for v in index_series[where].tolist()]
            band = f' ({level})' if banded else ''
            blanks.append(f"    {CANONICAL_HEADING[group]}...{band}: "
                          f"{int(where.sum())} blank or non-numeric cell(s) at "
                          f"{', '.join(at[:6])}{' ...' if len(at) > 6 else ''}")
    if blanks:
        raise ValueError(
            f"Sheet '{sheet_name}' of {ENERGY_DATASET.name} has cells with no number in "
            f"them:\n" + "\n".join(blanks) +
            f"\nEvery price is read per {index_column}, so there is nothing to put in "
            f"their place: a blank would reach the objective as NaN and fail the solve "
            f"with a message naming none of this. Fill them in.")

    return index_series, groups


# 2.2 report scenario columns that break the low <= medium <= high ordering
def check_scenario_ordering(sheet_name, index_series, groups):
    """The model reads 'low' as the min and 'high' as the max of each price band.

    A sheet where medium falls outside [low, high] still produces output, but the
    'best case' / 'worst case' scenarios then no longer bracket the medium case -
    so the offending column is reported instead of being silently accepted.
    """
    problems = []
    for group, levels in sorted(groups.items()):
        broken = ~((levels['min'] <= levels['mean']) & (levels['mean'] <= levels['max']))
        broken = broken & levels['min'].notna() & levels['mean'].notna() & levels['max'].notna()
        if broken.any():
            rows = index_series[broken].tolist()
            problems.append(f"    {group}: {int(broken.sum())} row(s) of '{sheet_name}', "
                            f"e.g. {index_series.name or 'index'}={rows[0]:g} "
                            f"(low={levels['min'][broken].iloc[0]:g}, "
                            f"medium={levels['mean'][broken].iloc[0]:g}, "
                            f"high={levels['max'][broken].iloc[0]:g})")
    if problems:
        print(f"WARNING: {ENERGY_DATASET.name} has scenario columns where "
              f"low <= medium <= high does not hold:")
        for problem in problems:
            print(problem)
    return problems


# 2.2b report daily curves whose mean does not match the year they describe
def check_daily_matches_yearly(curves_df, energy_cost_df):
    """What each scenario band does to the daily curve in the base year of the outlook.

    'energy_daily' states one curve and 'energy_yearly' brackets each year with three, so
    the one curve can sit on at most one of the three bands - the other two necessarily
    differ, and this is the factor between them. Not an error, and not avoidable: it is
    what choosing a scenario means once the shape is fixed.

    It matters because the two run types use it differently (model 1.4b2). A sizing run
    or a sweep rescales the curve by this factor, so the band it is run under sets the
    level. A disposition run does not rescale at all: it prices the day as written, and
    the band reaches nothing, the diesel and the public charger included. Where the factor
    is 1 the two runs agree exactly; everywhere else this is how far apart they sit. The
    base year is taken as the earliest year of the outlook.
    """
    base_year = int(energy_cost_df['Year'].min())
    yearly = energy_cost_df.set_index('Year')
    problems = []
    for channel, group in V2G_CHANNELS.items():
        for level in ('min', 'mean', 'max'):
            daily_mean = float(curves_df[f'{level}_{channel}_price_€/MWh'].mean())
            # the yearly columns keep their own units: €/kWh for the charging prices,
            # €/MWh for flexibility
            if group == 'flexibility_price':
                yearly_value = float(yearly.loc[base_year, f'{level}_flexibility_price_€/MWh'])
            else:
                yearly_value = float(yearly.loc[base_year, f'{level}_{group}_€/kWh']) * 1000.0
            if yearly_value <= 0 or abs(daily_mean - yearly_value) <= 1e-6:
                continue
            problems.append(
                f"    {channel} ({level}): 'energy_daily' averages {daily_mean:g} €/MWh, "
                f"'energy_yearly' says {yearly_value:g} €/MWh for {base_year} "
                f"- a sizing or sweep run of {base_year} rescales the curve by "
                f"{yearly_value / daily_mean:.3f}, a disposition run does not")
    if problems:
        print(f"NOTE: what each scenario band does to the daily curve in {base_year}. A "
              f"sizing run or a sweep rescales by the factor shown; a disposition run "
              f"prices the day as written and the band reaches nothing:")
        for problem in problems:
            print(problem)
    return problems


# 2.3 yearly energy cost parameters
def build_yearly_cost_parameters():
    """Carry the yearly price outlook over in the native units of the dataset.

    No distance-based (€/100km) columns are produced: converting a price into a cost
    per km needs a consumption, and the disposition model applies each vehicle's own
    consumption from fleet_dataset.xlsx rather than a fleet-wide reference figure.
    """
    years, groups = read_scenario_sheet('energy_yearly', 'year')
    years.name = 'Year'
    check_scenario_ordering('energy_yearly', years, groups)

    energy_cost_df = pd.DataFrame({'Year': years.astype(int)})
    for level in ('min', 'mean', 'max'):
        energy_cost_df[f'{level}_energy_spot_price_€/kWh'] = \
            groups['energy_spot_price'][level]
        energy_cost_df[f'{level}_public_charging_cost_€/kWh'] = \
            groups['public_charging_cost'][level]
        energy_cost_df[f'{level}_public_diesel_cost_€/l'] = \
            groups['public_diesel_cost'][level]
        # €/kWh -> €/MWh, the unit the V2G earnings term of the model works in
        energy_cost_df[f'{level}_flexibility_price_€/MWh'] = \
            groups['flexibility_price'][level] * 1000.0

    return energy_cost_df


# 2.4 intraday price curves for the two V2G channels
def build_intraday_price_curves():
    """Hourly price curve per V2G channel, exactly as 'energy_daily' states it.

    Kept at full hourly resolution rather than aggregated into 4h periods as before.
    Arbitrage lives on the intraday spread, and averaging the curve into six blocks
    flattens exactly the differences it trades on - in this dataset the electricity
    spot price moves by a factor of three across the day.

    'energy_daily' states absolute prices of one operating day, and they are carried
    over unchanged. What the model does with them is its energy_price_basis (1.4b2):
    a disposition run prices that day off this file as it stands, shape and level
    together, while a sizing run and a sweep keep the distribution and rescale it onto
    the scenario year of 'energy_yearly'. Either way the intraday shape comes from
    here, which is why it is written at full hourly resolution.

    The sheet carries one series per price and no scenario band (2.0), so the three
    min/mean/max columns written below are the same curve three times. They are written
    anyway: the derived CSV and the disposition model are one structure, and a band is
    what the yearly sheet applies to this curve, not something this file has to know.
    """
    hours, groups = read_scenario_sheet('energy_daily', 'hour')
    hours.name = 'Hour'
    # vacuous on a band-less sheet - min, mean and max are the same series, so the
    # ordering holds trivially. Called all the same, because whether this sheet has a
    # band is the workbook's business and not a branch to keep in step with it here.
    check_scenario_ordering('energy_daily', hours, groups)

    if sorted(hours.astype(int)) != list(range(1, 25)):
        raise ValueError(
            f"Sheet 'energy_daily' of {ENERGY_DATASET.name} must contain the hours "
            f"1...24, once each; got {len(hours)} row(s).")

    curves_df = pd.DataFrame({'Hour': hours.astype(int)})
    for channel, group in V2G_CHANNELS.items():
        for level in ('min', 'mean', 'max'):
            # €/kWh -> €/MWh, the unit the V2G earnings term of the model works in
            curves_df[f'{level}_{channel}_price_€/MWh'] = groups[group][level] * 1000.0

    # ... and the two prices that do not move across the day, carried over in their own
    # units. The model uses them as single numbers, not as curves, so there is no curve to
    # build - but a disposition run prices EVERYTHING off this sheet (model 1.4b2), so the
    # day's diesel and its public charger have to arrive with the day's energy prices
    # rather than being fetched from the yearly outlook behind them. Written per hour like
    # the rest so the file has one shape; the model averages them back to one number.
    for group, suffix in FLAT_DAILY_PRICES.items():
        for level in ('min', 'mean', 'max'):
            curves_df[f'{level}_{suffix}'] = groups[group][level]
    return curves_df.sort_values('Hour').reset_index(drop=True)



# 3 OUTPUT
# 3.1 intraday price curves of the two V2G channels
def plot_v2g_hourly(curves_df, output_dir):
    """Plot the two single daily V2G price curves in €/kWh."""
    use_figure_style(plt)
    hours = list(curves_df['Hour'] - 1) + [24]  # left edge of each hour bin, plus the 24:00 boundary
    time_labels = ['00:00', '04:00', '08:00', '12:00', '16:00', '20:00', '24:00']
    tick_hours = [0, 4, 8, 12, 16, 20, 24]
    styles = {
        'arbitrage': ('tab:blue', 'arbitrage - electricity spot price'),
        'flexibility': ('tab:orange', 'flexibility - flexibility spot price'),
    }

    plt.figure(figsize=(11, 7))
    ceiling = 0.0
    for channel, (color, label) in styles.items():
        values = list(curves_df[f'mean_{channel}_price_€/MWh'] / 1000.0)
        values.append(values[-1])
        ceiling = max(ceiling, max(values))
        plt.step(hours, values, color=color, linewidth=1.8, where='post', label=label)

    plt.title("Intraday price curves of the two vehicle-to-grid channels", y=1.05, fontweight="bold", fontsize=14)
    plt.suptitle("(one daily curve per channel, sheet 'energy_daily' of costs_dataset.xlsx)",
                 y=0.92, fontsize=14)
    plt.xlabel('\ntime of day', fontweight='bold', fontsize=14)
    plt.ylabel('price [€/kWh]\n', fontweight='bold', fontsize=14)
    plt.xticks(tick_hours, time_labels)
    plt.xlim(0, 24)
    plt.ylim(0, ceiling * 1.15)
    plt.legend()
    plt.grid(False)
    plt.savefig(output_dir / 'plot_cost_parameter_hourly.png', dpi=FIGURE_DPI)


# 3.2 yearly energy cost plot
def plot_energy_yearly(energy_cost_df, output_dir):
    """Yearly price bands in the native units of the dataset.

    The bands used to be drawn in €/100km, which required a reference consumption per
    drive train. The prices are now shown as they are stated, on a logarithmic axis so
    that €/kWh, €/l and €/MWh remain readable side by side.
    """
    use_figure_style(plt)
    bands = [
        ('public_diesel_cost_€/l', 'tab:red', '\\\\',
         'public diesel prices [€/l] (outlook, heavy duty vehicle pricing)'),
        ('flexibility_price_€/MWh', 'tab:orange', '||',
         'flexibility prices [€/MWh] (positive & negative power)'),
        ('public_charging_cost_€/kWh', 'tab:green', 'xx',
         'public charging prices [€/kWh] (outlook, averaged)'),
        ('energy_spot_price_€/kWh', 'tab:blue', '////',
         'electricity spot prices [€/kWh] (outlook; also the arbitrage price)'),
    ]

    plt.figure(figsize=(11, 7))
    years = energy_cost_df['Year']
    for suffix, color, hatch, label in bands:
        plt.fill_between(years, energy_cost_df[f'min_{suffix}'], energy_cost_df[f'max_{suffix}'],
                         color=color, alpha=0.2, edgecolor=color, hatch=hatch, label=label)
        plt.plot(years, energy_cost_df[f'min_{suffix}'], linestyle='--',
                 color=color, linewidth=0.8)
        plt.plot(years, energy_cost_df[f'mean_{suffix}'], linestyle='-',
                 color=color, linewidth=1.5)
        plt.plot(years, energy_cost_df[f'max_{suffix}'], linestyle='--',
                 color=color, linewidth=0.8)

    plt.title("Energy cost parameter for heavy-duty vehicles", y=1.05, fontweight="bold", fontsize=14)
    plt.suptitle(f"(yearly outlook {int(years.min())} - {int(years.max())}, Germany)", y=0.92, fontsize=14)
    plt.xlabel('\n year', fontweight='bold', fontsize=14)
    plt.ylabel('cost parameter [€/kWh, €/l, €/MWh] - log scale \n', fontweight='bold', fontsize=14)
    plt.xticks(range(int(years.min()), int(years.max()) + 1, 5))
    plt.xlim(int(years.min()), int(years.max()))
    plt.yscale('log')
    handles, labels = plt.gca().get_legend_handles_labels()
    plt.legend(handles[:4], labels[:4])
    plt.grid(False)
    plt.savefig(output_dir / 'plot_cost_parameter_yearly.png', dpi=FIGURE_DPI)


# 3.3 pipeline entry point
def generate_cost_parameters(make_plots=True):
    require_input(ENERGY_DATASET)
    ensure_working_data_dir()
    output_dir = ensure_working_data_dir()   # the figures below belong with the generated inputs

    energy_cost_df = build_yearly_cost_parameters()
    curves_df = build_intraday_price_curves()
    check_daily_matches_yearly(curves_df, energy_cost_df)

    energy_cost_df.to_csv(COST_PARAMETER_ENERGY_CSV, index=False, encoding=CSV_ENCODING)
    curves_df.to_csv(COST_PARAMETER_DAILY_CSV, index=False, encoding=CSV_ENCODING)

    if make_plots:
        plot_v2g_hourly(curves_df, output_dir)
        plot_energy_yearly(energy_cost_df, output_dir)

    base_year = int(energy_cost_df['Year'].min())
    print(f"cost parameters: {len(energy_cost_df)} years "
          f"({base_year}-{int(energy_cost_df['Year'].max())}) "
          f"in €/kWh, €/l and €/MWh; the disposition model converts them per vehicle")
    print(f"  -> {COST_PARAMETER_ENERGY_CSV.relative_to(PROJECT_ROOT)}")
    note = sheet_note('energy_daily')
    print(f"daily prices:    {len(curves_df)} hours, sheet 'energy_daily' as written"
          f"{' - ' + note if note else ''}. A disposition run is priced off these; a "
          f"sizing run and a sweep rescale the two curves onto the scenario year of the "
          f"outlook above and take the other two prices from it.")
    print(f"  -> {COST_PARAMETER_DAILY_CSV.relative_to(PROJECT_ROOT)}")
    return energy_cost_df, curves_df



# 4 MAIN
if __name__ == '__main__':
    generate_cost_parameters()
    plt.show()
