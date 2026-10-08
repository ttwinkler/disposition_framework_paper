# 1 SETUP
# 1.1 load modules
import os
import gc
import sys
import json
import math
import time
import tqdm
import importlib
import schedule
import threading
import itertools
import statistics
import contextlib
import multiprocessing
import numpy as np
import pandas as pd
import gurobipy as gp
from pathlib import Path
from datetime import datetime
# matplotlib is imported lazily by _pyplot() - see section 1.1b
# slack_sdk likewise, by send_slack_notification() - see section 1.5b. Both are optional
# features, and neither should be able to stop the model from importing when unused.

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 1.2 project paths
#
# Three directories, by what a file *is* rather than by when it was made:
#
#   inputs/     the four Excel datasets. Primary input, authored by an operator, and
#                  never written to by anything here.
#   data/  what the pipeline builds from them so the model can run - the routed
#                  trip set, the depot and price curves, the PVGIS cache. Every one of
#                  these is derived and regenerable: delete the directory and the next
#                  run rebuilds it (ensure_derived_inputs, 1.3).
#                  prefix because that is what they are to the model.
#   results/   the answers. Figures, the run summary CSV, and the schedules a run
#                  produces. Nothing in here is read back by a later run.
#
# They used to be two, with the derived inputs and the answers sharing `results/`. That
# made "can I clear this out?" unanswerable without knowing each filename - the trip set
# and the PVGIS cache cost an hour of geocoding and routing to rebuild, and they sat in
# the same listing as a plot from a run nobody kept.
PROJECT_ROOT              = Path(__file__).resolve().parent.parent
USER_DATA_DIR             = PROJECT_ROOT / 'inputs'
WORKING_DATA_DIR          = PROJECT_ROOT / 'data'
RESULT_DATA_DIR           = PROJECT_ROOT / 'results'
FLEET_DATASET             = USER_DATA_DIR / 'fleet_dataset.xlsx'
# the other primary datasets, named here so a derived file can be checked against the one
# it was built from rather than only for existence
ENERGY_DATASET            = USER_DATA_DIR / 'costs_dataset.xlsx'
DEPOT_DATASET             = USER_DATA_DIR / 'depot_dataset.xlsx'
TRIPS_CSV = WORKING_DATA_DIR / 'order_trips.csv'
COST_PARAMETER_ENERGY_CSV = WORKING_DATA_DIR / 'cost_parameter_yearly.csv'
COST_PARAMETER_DAILY_CSV    = WORKING_DATA_DIR / 'cost_parameter_hourly.csv'
DEPOT_LOAD_PROFILE_CSV    = WORKING_DATA_DIR / 'depot_load_profile.csv'
DEPOT_PV_PARAMETER_CSV    = WORKING_DATA_DIR / 'depot_pv_parameters.csv'
DEPOT_CHARGING_CSV        = WORKING_DATA_DIR / 'depot_charging_stations.csv'
PV_PROFILE_CACHE_JSON     = WORKING_DATA_DIR / 'cache_pv_profile.json'  # PVGIS answers, shared by all processes
CSV_ENCODING              = 'utf-8'
# below this the solver's answer is numerical noise, not a charge or a discharge. Used to
# decide whether a step really moved energy when the schedule is written.
ENERGY_TOLERANCE_KWH      = 1e-6
# below this achieved MIP gap a solve is called proved rather than accepted-at-the-target
# (5.5). Gurobi's own default MIPGap is 1e-4, so anything under it is the solver saying it
# found nothing left to close rather than the run having asked for less.
MIP_GAP_PROVEN            = 1e-4


def project_path(*parts):
    return PROJECT_ROOT.joinpath(*parts)


# 1.0b the run stamp, so one run's outputs do not overwrite the last one's
#
# Every figure and CSV a run writes used to land on a fixed name - disposition_schedule.csv,
# plot_SoC_charging_power.png and the rest - with only plot_suffix to separate the days of a
# design run. Two runs of anything therefore produced one set of files, and which run they
# described was whichever finished last. That is also why a sweep does not write them at
# all (sweep_solve, section 6): N solves were N writers of one filename.
#
# The stamp is taken once, when the run starts, and prefixed to every file that run writes,
# so a set of outputs is identifiable as one run's work and two runs can coexist. Format is
# YYMMDDHHMMSS-PPPPSSS_, sortable by start time and short enough to leave the descriptive
# part of the name readable. What the suffix is for is in new_run_stamp() below.
#
# Taken at the *start* rather than per file on purpose: a design run writes one set per day
# over what may be hours, and stamping each file as it is written would scatter one answer
# across a range of times instead of naming the run that produced it.
output_run_stamp = None


# 1.0a the time grid, which is an invariant and not a parameter
#
# The whole model runs on 30-minute steps, 48 of them in a day. That is not a setting that
# happens to be 30 minutes at the moment: it is baked into how the inputs are built and
# read, and the three constants below are the one place it is written down.
#
# It cannot be changed by assigning a different number here. The depot load profile and the
# PV profile are generated on this grid by their own modules and read back by step index;
# the trip generator snaps every order window onto it and writes trips.csv already
# quantised; the energy and flexibility curves arrive hourly and are expanded two steps per
# hour; the crew rules count breaks in steps. A different step length needs all of those
# changed together, and a model that silently accepted the number while the inputs stayed
# on the old grid would produce a complete, plausible, wrong answer - the worst failure
# this file can have.
#
# So it is stated as an invariant and checked once per build (2.1.1) rather than offered as
# a knob. Everything that needs a conversion derives it from these names; the literals 30,
# 2 and 48 should not appear anywhere else in the file.
STEP_MINUTES  = 30
STEPS_PER_HOUR = 60 // STEP_MINUTES          # 2
STEP_HOURS     = STEP_MINUTES / 60.0         # 0.5
STEPS_PER_DAY  = 24 * STEPS_PER_HOUR         # 48


# 1.0c setting a module parameter for the length of one call, and putting it back
#
# Several places here configure the model by assigning a module global and restoring it
# afterwards - the depot a design run charges against, the figures switch, the time limit
# of one solve. It is the calling convention this file grew, and it is what makes a 54
# parameter model_build possible without a 154 parameter one.
#
# It is also easy to get wrong in exactly one way: an early return or an exception between
# the assignment and the restore leaves the global changed, and the next thing to read it
# is planning against a depot or a time limit that belongs to a run that has already ended.
# Every site below used to hand-roll the save/restore, four of them with their own
# try/finally and one without.
#
# So there is one of them. Used as
#
#     with model_parameters(run_kind='sizing', optimization_time_limit_s=90):
#         ...
#
# it restores on any exit, including an exception, and it is the only place that has to be
# read to know the pattern is safe. It does not make the globals thread-safe or
# process-safe - nothing here can, which is why a pool worker is configured explicitly
# instead (3.5.8) - but it does make them exception-safe.
@contextlib.contextmanager
def model_parameters(**overrides):
    """Set module-level parameters for the body of the with-block, then put them back.

    A name that is not already a module global is refused rather than created. The
    save/restore is built on reading the previous value, so an unknown name used to die
    on a bare KeyError from the dict comprehension - correct in that nothing was left
    changed, but it named the key and not the mistake. A typo'd parameter is the likely
    cause and it deserves to say so, because the alternative reading ("this sets a new
    parameter") is one this context manager cannot support: there would be nothing to
    put back, and the name would leak into the module for every later call.
    """
    unknown = sorted(name for name in overrides if name not in globals())
    if unknown:
        raise KeyError(
            f"model_parameters() was given {unknown}, which {'is' if len(unknown) == 1 else 'are'} "
            f"not module parameter(s) of this file. Only existing globals can be overridden "
            f"for the length of a block - check the spelling against section 1.3/1.4.")
    previous = {name: globals()[name] for name in overrides}
    globals().update(overrides)
    try:
        yield
    finally:
        globals().update(previous)


RUN_STAMP_TIME_FORMAT = '%y%m%d%H%M%S'
_run_stamp_serial = itertools.count()


def new_run_stamp():
    """A stamp no other run can be wearing, sortable by when it started.

    The clock alone is not enough. Its resolution is one second and the things that start
    runs do not wait that long between them: the web interface answers requests as they
    come, a scenario batch starts the next solve the moment the last returns, and a test
    may call two entry points in a row. Two runs sharing a stamp share every output name
    they write - summary, plots, schedule CSV - so the second silently overwrites the
    first, or the two interleave and leave one file half from each.

    So the second-resolution clock is only the sortable prefix, and what makes the stamp
    unique is appended to it: the process id separates concurrent runs, and a counter
    separates runs started one after another inside the same process. Both are hex and
    fixed-width, which keeps the whole stamp lexicographically ordered by start time -
    the interface finds the newest output by sorting these names (its 3.7), and that has
    to keep working.
    """
    return (f"{datetime.now().strftime(RUN_STAMP_TIME_FORMAT)}"
            f"-{os.getpid() % 0x10000:04x}{next(_run_stamp_serial) % 0x1000:03x}")


def run_stamp_started_at(stamp):
    """The datetime a stamp names, or None if it is not one of ours."""
    try:
        return datetime.strptime(str(stamp)[:12], RUN_STAMP_TIME_FORMAT)
    except (ValueError, TypeError):
        return None


def begin_output_run(stamp=None):
    """Open a new output generation and return its stamp (see 1.0b)."""
    global output_run_stamp
    output_run_stamp = stamp or new_run_stamp()
    return output_run_stamp


def output_filename(name):
    """`name` prefixed with the current run stamp, or unchanged when no run has begun."""
    return f"{output_run_stamp}_{name}" if output_run_stamp else name


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


# 1.2b derived inputs: which generator produces which file in data/
#      Everything the model reads from data/ is derived from a dataset in inputs/, so a
#      missing file is not an error - it just has not been built yet. ensure_derived_inputs()
#      builds whatever is absent, which is why none of the entry points has to be primed
#      by hand. Only inputs/ is irreplaceable.
#      Each entry also names the dataset it is derived FROM, because "present" is not the
#      same as "current": a derived file built from a previous version of its Excel is
#      exactly as wrong as a missing one and far harder to notice. If the source is newer
#      than what was derived from it, the file is rebuilt.
#      Trip generation is exempt - its source is the order data, but rebuilding it
#      re-geocodes and re-routes every order, so a stale trips.csv is announced and left
#      for the operator to refresh deliberately.
#      The PVGIS year cache is the opposite: one seriescalc round trip covers every
#      day in trips.csv, so a stale cache is rebuilt here whenever order_dataset.xlsx
#      or depot_dataset.xlsx no longer match the fingerprint it stores.
DERIVED_INPUT_BUILDERS = (
    ((COST_PARAMETER_ENERGY_CSV, COST_PARAMETER_DAILY_CSV),
     'hdv_cost_parameter_generation', 'generate_cost_parameters', 'energy cost parameters',
     ENERGY_DATASET),
    ((DEPOT_LOAD_PROFILE_CSV, DEPOT_PV_PARAMETER_CSV, DEPOT_CHARGING_CSV),
     'hdv_depot_load_profile_generation', 'generate_depot_load_profile', 'depot load, PV plant and chargers',
     DEPOT_DATASET),
    ((TRIPS_CSV,),
     'hdv_trip_generation', 'generate_trips', 'routed trips',
     None),
)


def ensure_derived_inputs(announce=True):
    """Build every derived input in data/ that is not there yet.

    The cheap generators are imported only when something is actually missing: on the
    normal path - all files present - that costs a handful of stat() calls. Trip
    generation geocodes and routes every order, so a stale trips.csv is announced
    rather than rebuilt silently. The PVGIS year cache is checked every time: hashing
    the two Excel files is cheap, and a stale curve would be wrong in a way nobody
    would notice.
    """
    built = []
    for paths, module_name, function_name, label, source in DERIVED_INPUT_BUILDERS:
        absent = [Path(p) for p in paths if not Path(p).exists()]
        stale = []
        if source is not None and Path(source).exists():
            source_mtime = Path(source).stat().st_mtime
            stale = [Path(p) for p in paths
                     if Path(p).exists() and Path(p).stat().st_mtime < source_mtime]
        if not absent and not stale:
            continue
        if announce:
            reason = (f"{', '.join(p.name for p in absent)} missing" if absent
                      else f"{', '.join(p.name for p in stale)} older than {Path(source).name}")
            print(f"deriving {label} from inputs/ ({reason}) ...", flush=True)
        ensure_working_data_dir()
        module = importlib.import_module(module_name)
        generator = getattr(module, function_name)
        # generate_trips() takes no make_plots; the other two default it to True, and a
        # figure nobody asked for is not worth the time here
        if function_name == 'generate_trips':
            generator()
        else:
            generator(make_plots=False)
        built.append(label)
    # PVGIS cache is checked every time, not only when the JSON is missing: a file
    # that is present but was built from a previous order_dataset.xlsx or
    # depot_dataset.xlsx is exactly as wrong as a missing one. The generator
    # no-ops when the fingerprint still matches, so the normal path is a hash of
    # the two Excel files and a read of trips.csv.
    from hdv_pv_profile_generation import generate_pv_profile_cache
    generate_pv_profile_cache(announce=announce)
    announce_stale_trips(announce=announce)
    return built


def announce_stale_trips(announce=True):
    """Say so when data/order_trips.csv no longer matches the order data or the trip filters.

    Trip generation is the one derived input this module will not rebuild: it geocodes and
    routes every order, which costs minutes and an API budget, so refreshing it stays a
    deliberate act. But "will not rebuild" was implemented as "will not look", and those are
    not the same thing. The staleness test above is an mtime comparison against `source`,
    and the trips entry carries source=None precisely because its real test is a fingerprint
    rather than a timestamp - so `stale` was always empty for it, the branch that announced
    it was unreachable, and a trips.csv built under different filter constants was used in
    silence. Changing MIN_TRIP_DURATION_H and getting the old trip set is exactly that bug.

    The fingerprint covers the order rows *and* the generation parameters (trips-v4,
    routing mode, the duration bounds, the step length), so it catches a filter change that
    no timestamp could. It costs one read of order_dataset.xlsx and a hash, on a path that
    already reads several files.

    Warned about, never acted on: the rebuild is still the operator's to run.
    """
    if not announce:
        return False
    try:
        from hdv_trip_generation import load_orders, order_fingerprint, stored_fingerprint
        current = order_fingerprint(load_orders())
        stored = stored_fingerprint()
    except Exception as exc:                 # a broken check must not stop a run
        print(f"note: could not check whether {TRIPS_CSV.name} is current ({exc})",
              flush=True)
        return False
    if stored == current:
        return False
    reason = ("carries no fingerprint" if stored is None
              else f"was built from different order data or trip filters "
                   f"({stored[:12]} != {current[:12]})")
    print(f"WARNING: {TRIPS_CSV.name} {reason}. This run is planning the trips that are on "
          f"disk, not the ones inputs/order_dataset.xlsx and the filters in "
          f"src/hdv_trip_generation.py now describe. Refresh it with "
          f"'python main.py --prepare' (add --force-routing to re-route regardless).",
          flush=True)
    return True


# No os.chdir here. Importing this module used to change the process's working directory,
# which is action at a distance on anything that imported it - the web interface, the
# benchmark harness, and the pool bootstrap that imports it purely to resolve a function.
# Nothing here needs it: every path is built from PROJECT_ROOT through project_path(), and
# a grep for bare relative paths in this file finds none.
ensure_working_data_dir()
ensure_result_data_dir()


# every figure is written as PNG. A raster keeps the browser fast: the Streamlit
# page lays the figures out again on every interaction, and a vector plot of a
# dense schedule costs it thousands of DOM nodes each time. 150 dpi so the raster
# still holds up when zoomed.
FIGURE_DPI = 150



# 1.1b lazy matplotlib
_PYPLOT = None
# whether the backend _pyplot() settled on can actually open a window. Not the same as
# wanting one: displays_outputs() is the intent, this is what the machine allowed.
_PYPLOT_INTERACTIVE = False


def _pyplot():
    """Import pyplot and the legend patch artist on first use.

    Only the plotting paths of a run that writes_outputs() (1.4b1) need matplotlib.
    Keeping it out of the module header means a sweep worker (which spawns a fresh
    interpreter and imports this module) and any headless single run never pay for it.

    The backend is chosen here, once, from displays_outputs(): a terminal disposition run
    gets an interactive one so show_figures() below has a window to open, everything else
    gets Agg. Both write the same PNGs - the backend decides only whether anything is
    shown. An interactive backend that will not import is not an error and not worth one:
    a headless server is exactly the case --no-interface exists for, so it falls back to
    Agg and the run keeps its files.
    """
    global _PYPLOT, _PYPLOT_INTERACTIVE
    if _PYPLOT is None:
        import matplotlib
        # matplotlib.use() only records the name - a backend with no display behind it
        # fails later, when the first figure is drawn, which is the middle of a solve.
        # So the window toolkit is asked directly and now: a root window created and
        # destroyed is decisive where an import is not (tkinter imports fine on a Linux
        # box with no $DISPLAY and raises the moment it is used).
        if displays_outputs():
            try:
                import tkinter
                _probe = tkinter.Tk()
                _probe.withdraw()
                _probe.destroy()
                matplotlib.use('TkAgg')
                _PYPLOT_INTERACTIVE = True
            except Exception:
                matplotlib.use('Agg')
        else:
            matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
        from hdv_figure_style import use_figure_style
        _PYPLOT = (use_figure_style(plt), Patch)
    return _PYPLOT


def release_figure(plt, figure):
    """Close a figure once it is written - unless this run is going to show it (1.4b1).

    Every figure here used to be closed the moment it was saved, with a `plt.close('all')`
    after the big ones for good measure. That is right for a run nobody is watching and
    fatal for one that is: matplotlib can only show a figure it still holds, so a terminal
    run would have opened an empty window. Closing stays the default - a design run draws
    one set per day and has no reason to keep any of them.
    """
    if displays_outputs():
        return
    plt.close(figure)
    plt.close('all')


def show_figures():
    """Open this run's figures in a window. A no-op unless 1.4b1 says to show them.

    Called once, after the run has finished and written everything, so a blocking window
    cannot hold up a solve or leave the outputs half-written if it is closed. Guarded on
    the backend actually being interactive rather than on the intent, because the intent
    can have been overruled above by a machine with no display.
    """
    if not (displays_outputs() and _PYPLOT_INTERACTIVE):
        return
    plt, _Patch = _pyplot()
    if not plt.get_fignums():
        return
    print(f'showing {len(plt.get_fignums())} figure(s) - close the windows to finish',
          flush=True)
    plt.show()


# 1.2b primary fleet input
#      The fleet is never synthesized: inputs/fleet_dataset.xlsx is the roster, one row
#      per vehicle, and it is used verbatim. Size and drive-train mix are therefore a
#      property of the dataset, not a parameter of the sweep.
FLEET_COLUMNS = [
    'vehicle_id',
    'vehicle_type',
    'vehicle_consumption',
    'vehicle_energy_storage',
    'vehicle_charging_power',
    'vehicle_price',
    'vehicle_battery_warranty',
]

VEHICLE_TYPES = ('bev', 'ice')


def normalize_fleet(fleet_df):
    """Map input spellings ('vehicle_ID', 'BEV') onto the canonical schema.

    The whole project keys on 'vehicle_id' and compares vehicle_type against the
    lower-case labels 'bev'/'ice', so every fleet table goes through this function.
    """
    fleet_df = fleet_df.copy()
    fleet_df.columns = [str(c).strip() for c in fleet_df.columns]

    renames = {}
    for column in fleet_df.columns:
        lowered = column.lower()
        if lowered in FLEET_COLUMNS:
            renames[column] = lowered
    fleet_df = fleet_df.rename(columns=renames)

    missing = {'vehicle_id', 'vehicle_type', 'vehicle_consumption',
               'vehicle_energy_storage', 'vehicle_charging_power'} - set(fleet_df.columns)
    if missing:
        raise ValueError(f"Fleet table is missing required columns: {sorted(missing)}")

    fleet_df['vehicle_id'] = pd.to_numeric(fleet_df['vehicle_id'], errors='raise').astype(int)

    # vehicle_id is the key the whole model is built on, and nothing downstream survives a
    # repeat. The per-vehicle parameters become dicts (2.4), so a duplicate silently keeps
    # the *last* row and attaches its consumption, capacity and price to both trucks; the
    # vehicle list keeps both copies, so addVars is handed the same key twice and Gurobi
    # collapses it without complaint rather than raising. The result is a fleet that has
    # fewer variables than rows while the trips still demand covering - an infeasibility,
    # or a plausible answer computed with the wrong truck, and no message either way.
    # Checked here because this is the one gate every fleet table passes through.
    duplicate_ids = fleet_df.loc[fleet_df['vehicle_id'].duplicated(), 'vehicle_id'].tolist()
    if duplicate_ids:
        raise ValueError(
            f"vehicle_id must be unique - repeated id(s): {sorted(set(duplicate_ids))}. "
            f"Every per-vehicle parameter is looked up by this id, so a repeat would "
            f"silently give one truck another's consumption, battery and price.")

    fleet_df['vehicle_type'] = fleet_df['vehicle_type'].astype(str).str.strip().str.lower()

    unknown = set(fleet_df['vehicle_type']) - set(VEHICLE_TYPES)
    if unknown:
        raise ValueError(f"Unknown vehicle_type values in fleet input: {sorted(unknown)}")

    for column in ('vehicle_consumption', 'vehicle_energy_storage', 'vehicle_charging_power'):
        fleet_df[column] = pd.to_numeric(fleet_df[column], errors='coerce')

    # every vehicle is priced with its own consumption, so a blank cell here would
    # silently make that truck's energy free instead of raising
    blank_consumption = fleet_df.loc[fleet_df['vehicle_consumption'].isna(), 'vehicle_id'].tolist()
    if blank_consumption:
        raise ValueError(f"vehicle_consumption is missing or non-numeric for vehicle_id(s): "
                         f"{blank_consumption}. It prices the energy of each vehicle "
                         f"individually and cannot be defaulted.")

    # the acquisition price is the numerator of the €/EFC degradation price, so a
    # substituted figure would misprice that vehicle's aging exactly as a substituted
    # warranty would. Every row states its own.
    if 'vehicle_price' not in fleet_df.columns:
        raise ValueError(
            "The fleet input has no 'vehicle_price' column. It prices the battery share "
            "of each vehicle and through it the degradation, so it cannot be defaulted.")
    fleet_df['vehicle_price'] = pd.to_numeric(fleet_df['vehicle_price'], errors='coerce')
    bad_price = fleet_df.loc[~(fleet_df['vehicle_price'] > 0), 'vehicle_id'].tolist()
    if bad_price:
        raise ValueError(
            f"vehicle_price is missing, non-numeric or <= 0 for vehicle_id(s): "
            f"{bad_price}. It prices the battery share of that vehicle and through it "
            f"its degradation, so it cannot be defaulted.")

    # charging power is what a bev can take in a step and give back through V2G; a blank
    # cell used to become 0 kW, turning that truck into a parked asset without a word.
    # ice rows may leave it empty - nothing reads it for them.
    bev_rows = fleet_df['vehicle_type'] == 'bev'
    bad_power = fleet_df.loc[
        bev_rows & ~(fleet_df['vehicle_charging_power'] > 0), 'vehicle_id'].tolist()
    if bad_power:
        raise ValueError(
            f"vehicle_charging_power is missing, non-numeric or <= 0 for bev "
            f"vehicle_id(s): {bad_power}. It caps what that vehicle can charge and "
            f"discharge per step and cannot be defaulted.")

    # the usable battery is the whole SoC dimension of the model
    bad_storage = fleet_df.loc[
        bev_rows & ~(fleet_df['vehicle_energy_storage'] > 0), 'vehicle_id'].tolist()
    if bad_storage:
        raise ValueError(
            f"vehicle_energy_storage is missing, non-numeric or <= 0 for bev "
            f"vehicle_id(s): {bad_storage}. It is the capacity every SoC bound and the "
            f"degradation price are computed against and cannot be defaulted.")

    # battery warranty in equivalent full cycles; it is the denominator of the €/EFC
    # degradation price and stays blank for ice vehicles, which have no battery.
    # There is no default: every bev must state its own warranted cycle count, because
    # substituting a fleet-wide figure would misprice that vehicle's degradation.
    if 'vehicle_battery_warranty' not in fleet_df.columns:
        fleet_df['vehicle_battery_warranty'] = np.nan
    fleet_df['vehicle_battery_warranty'] = pd.to_numeric(
        fleet_df['vehicle_battery_warranty'], errors='coerce')
    bad_warranty = fleet_df.loc[
        bev_rows & ~(fleet_df['vehicle_battery_warranty'] > 0), 'vehicle_id'].tolist()
    if bad_warranty:
        raise ValueError(
            f"vehicle_battery_warranty is missing, non-numeric or <= 0 for bev "
            f"vehicle_id(s): {bad_warranty}. It states the warranted number of "
            f"equivalent full cycles and prices that vehicle's battery degradation, "
            f"so it cannot be defaulted."
        )

    return fleet_df[FLEET_COLUMNS].reset_index(drop=True)


# 1.2c which sheet of fleet_dataset.xlsx is the roster
#
#      The workbook carries two: the trucks the depot owns today, and a synthetic roster to
#      draw a design fleet from. A run with auto_sizing off is dispatching the fleet that
#      exists, so it reads that sheet by name rather than by position - a sheet added in
#      front of it would otherwise silently become the fleet.
#
#      The names below are the workbook's, exactly. Rename a sheet in Excel and the
#      constant has to be changed with it; there is no fuzzy matching.
FLEET_SHEET_EXISTING = 'existing_fleet'
FLEET_SHEET_SYNTHETIC = 'synthetic_fleet'


def load_fleet_dataset(filepath=None, sheet=FLEET_SHEET_EXISTING):
    """Read inputs/fleet_dataset.xlsx and return the roster in the canonical schema.

    sheet   the sheet to read, by its exact name; None for the workbook's first sheet
            whatever that is.
    """
    path = require_input(filepath or FLEET_DATASET)
    if path.suffix.lower() not in ('.xlsx', '.xls'):
        fleet_df = pd.read_csv(path, encoding=CSV_ENCODING)   # a CSV roster has no sheets
    elif sheet is None:
        fleet_df = pd.read_excel(path)
    else:
        available = pd.ExcelFile(path).sheet_names
        if sheet not in available:
            raise ValueError(
                f"{path.name} has no sheet named {sheet!r}. It carries "
                f"{', '.join(repr(s) for s in available)}. The name is matched exactly, "
                f"spaces included - rename the sheet back, or change FLEET_SHEET_EXISTING "
                f"/ FLEET_SHEET_SYNTHETIC in section 1.2c to match it.")
        fleet_df = pd.read_excel(path, sheet_name=sheet)
        if fleet_df.empty:
            raise ValueError(
                f"Sheet {sheet!r} of {path.name} is empty, so there is no fleet to run. "
                f"Fill it in, or switch auto-sizing the other way.")
    if fleet_df.empty:
        raise ValueError(f"Fleet input {path} contains no rows.")
    return normalize_fleet(fleet_df)


# 1.2d the candidate pool a design run sizes from
#
#      'synthetic_fleet' lists vehicle *types*, one per row, with no vehicle_id: these are
#      the trucks that could be bought, not trucks that exist. The design model may take as
#      many of each as it needs, so the pool below instantiates every type `copies` times
#      and hands the optimizer one ownership binary per copy.
#
#      The copy count is a cap on the answer, so it has to be generous enough not to be the
#      binding constraint and small enough to solve. It is derived from the work by default
#      (see fleet_pool_copies_for) rather than guessed: a truck serves one trip at a time,
#      so the most trips that ever overlap is the most trucks that can ever be driving.
def load_fleet_pool(filepath=None, copies=1, sheet=FLEET_SHEET_SYNTHETIC, bev_copies=None):
    """Instantiate `copies` of every vehicle type in the synthetic sheet as a roster.

    bev_copies overrides the count for battery types alone. They are capped separately
    because the cap is a property of the depot's chargers (3.4.0) and a diesel truck does
    not touch one - capping both by the charging limit would have thrown away ice
    candidates the depot has no reason to refuse.

    Returns (pool_df, type_of) where pool_df is in the canonical fleet schema with
    generated vehicle_ids, and type_of maps vehicle_id -> the row index of the type it is
    a copy of. Copies of one type are identical, which is what lets the design model break
    their symmetry (3.4.2) instead of searching every permutation of the same fleet.
    """
    # a copy count below one empties the pool without saying so: range(0) instantiates
    # nothing, normalize_fleet is handed an empty frame and the design run reports that no
    # feasible fleet exists. Refused here, where the number is still recognisable as the
    # argument somebody passed.
    for name, value in (('copies', copies), ('bev_copies', bev_copies)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                f"load_fleet_pool({name}={value!r}): the pool needs a whole number of "
                f"copies of at least one per type.")
    path = require_input(filepath or FLEET_DATASET)
    available = pd.ExcelFile(path).sheet_names
    if sheet not in available:
        raise ValueError(
            f"{path.name} has no sheet named {sheet!r}, so there is no pool to size from. "
            f"It carries {', '.join(repr(s) for s in available)}.")
    types_df = pd.read_excel(path, sheet_name=sheet)
    types_df.columns = [str(c).strip() for c in types_df.columns]
    types_df = types_df.dropna(how='all')
    if types_df.empty:
        raise ValueError(
            f"Sheet {sheet!r} of {path.name} lists no vehicle types, so a design run has "
            f"nothing to choose from. Add one row per truck you would consider buying.")
    if 'vehicle_id' in types_df.columns:
        # a type is not a vehicle; an id column here would be silently duplicated per copy
        types_df = types_df.drop(columns=['vehicle_id'])

    rows, type_of = [], {}
    next_id = 1
    for type_index, row in enumerate(types_df.to_dict('records')):
        is_bev_type = str(row.get('vehicle_type', '')).strip().lower() == 'bev'
        how_many = int(bev_copies if (is_bev_type and bev_copies is not None) else copies)
        for _copy in range(how_many):
            entry = dict(row)
            entry['vehicle_id'] = next_id
            type_of[next_id] = type_index
            rows.append(entry)
            next_id += 1
    pool = normalize_fleet(pd.DataFrame(rows))
    return pool, type_of


def fleet_pool_copies_for(day_trip_frames, headroom=2, ceiling=12, announce=True):
    """How many copies of each type the pool needs to not be the binding constraint.

    A HEURISTIC CAP, not a bound, and the distinction matters because the two are used in
    different directions. This sizes the pool generously - too few copies silently removes
    the optimum from the search space - while the design run's own min_vehicles (3.5) has
    to be a *sound* lower bound and is derived separately, by dividing the same driving by
    the whole day rather than by the working window.

    It used to claim to be a lower bound itself, on the reasoning that a day's driving
    hours over the hours the fleet may drive in is the fewest vehicles that day can need.
    That is not true here in either direction: a trip that does not fit inside the working
    hours keeps its own window instead (section 2.6, trips_outside_work_hours), so a truck
    may legally be busy for longer than the window and the true minimum can be smaller;
    and the empty running between trips is not in trip_duration_h at all, so the real
    occupancy can be larger. Both errors are acceptable in a cap and neither is acceptable
    in a bound, which is why the design run does not reuse this number.

    Deliberately not divided by the number of types: the answer may well be "all of them
    the same model", so copies of ONE type have to be able to cover the day alone. headroom
    covers empty running and the trucks a schedule holds back for charging; ceiling stops a
    heavy day turning this into a model that cannot solve - and says so when it binds,
    because a pool truncated in silence looks exactly like a pool that was big enough.
    """
    window_h = max(1.0, (work_end_step - work_start_step) * STEP_HOURS)
    busiest = 0
    for trips in day_trip_frames:
        if trips is None or len(trips) == 0 or 'trip_duration_h' not in trips.columns:
            continue
        driving_h = float(pd.to_numeric(trips['trip_duration_h'], errors='coerce').sum())
        busiest = max(busiest, int(math.ceil(driving_h / window_h)))
    if busiest == 0:                        # no usable durations; fall back on trip volume
        busiest = max((len(t) for t in day_trip_frames if t is not None), default=1)
    wanted = busiest + int(headroom)
    copies = max(1, min(int(ceiling), wanted))
    if announce and wanted > copies:
        print(f"note: the candidate pool is capped at {copies} copies per type, where the "
              f"busiest day's driving suggests {wanted}. The pool may therefore be the "
              f"binding constraint on the design answer rather than the economics - pass "
              f"pool_copies explicitly to run_design_optimization to raise it.", flush=True)
    return copies


# 1.2e what owning a truck costs per operating day
#
#      penalty_vehicle_use (1.5) is a tie-break, not a cost: one flat figure for every
#      vehicle, far below what a truck is worth, and identical for a 90 k diesel and a
#      250 k battery truck. It cannot size a fleet - it can only break ties between fleets
#      of the same size.
#
#      A design run prices ownership properly instead: the acquisition price of
#      fleet_dataset.xlsx spread over the vehicle's service life, as a straight-line
#      annuity with interest, divided by the days it is worked in a year. That is what an
#      extra truck has to earn back before the model will take it, and it is what stops the
#      optimizer buying a truck purely to sell its battery into the V2G spread: at the
#      figures below a bev costs well over a hundred euro a day to own, and a day of
#      arbitrage on one battery is worth a small fraction of that.
vehicle_service_life_years          = 8      # depreciation horizon of a truck [a]
vehicle_operating_days_per_year     = 250    # days a truck is worked in a year
vehicle_residual_value_share        = 0.20   # share of the price still recovered at the end
vehicle_capital_interest_rate       = 0.05   # nominal annual interest on the capital tied up


def daily_ownership_cost(vehicle_price):
    """€ per operating day of owning one vehicle of this price.

    Straight-line depreciation to the residual value, plus interest on the average capital
    tied up over the life. Both are annual, and both are spread over the days the truck is
    actually worked - a truck standing in the yard on a Sunday still costs its owner money,
    but it is the working days that have to carry it.
    """
    price = float(vehicle_price or 0.0)
    if price <= 0 or vehicle_service_life_years <= 0 or vehicle_operating_days_per_year <= 0:
        return 0.0
    residual = price * vehicle_residual_value_share
    depreciation_per_year = (price - residual) / vehicle_service_life_years
    # average capital employed over a straight line from price down to residual
    interest_per_year = vehicle_capital_interest_rate * (price + residual) / 2.0
    return (depreciation_per_year + interest_per_year) / vehicle_operating_days_per_year


# 1.3 variation parameters
date_disposition                    = '07.11.2025' # date of the disposition in format "DD.MM.YYYY"
work_hours_start                    = '06:00' # daily working hours: earliest time a trip may start (HH:MM on the 30-min grid)
work_hours_end                      = '18:00' # daily working hours: latest time a trip must be finished (HH:MM on the 30-min grid)
# the charging infrastructure is NOT listed here: every station of the depot brings its
# own row in depot_dataset.xlsx (sheet 'charging'), so the number of stations and the kW
# of each are properties of that dataset, never synthesized. build_runtime_context()
# reads them into charging_infrastructure (kW per station) and charging_station_ids.
# 1.2b the home depot: the one place the fleet is based, and the only place a truck can
# use the depot chargers or feed back through V2G. Given the way the trip locations are
# written in order_dataset.xlsx, so the two can be compared at all.
#
# Everything spatial follows from this one string. Trips whose start or end resolves to
# this place are "at home"; every other trip needs an empty run to reach it or to come
# back, unless it can be chained straight onto another trip that ends where it starts.
# src/hdv_route_chaining.py does that classification; section 3.3.16 turns it into
# constraints.
home_depot_location                 = '74635 Kupferzell Deutschland'
route_chaining_status               = 'on'   # 'off' restores the previous, geography-free model: every trip assignable after any other and depot charging available in any free step
# How close two locations have to be to count as one place, as a share of the *median
# trip distance* of the loaded trip set. A share rather than a distance because "the same
# place" scales with the journeys: 10 km apart is the same yard on a 200 km tour and two
# different towns on a 20 km one.
#
# Worth checking rather than trusting. On the shipped orders the median trip is ~109 km,
# so 0.10 is a ~11 km radius - wide enough that Untermuenkheim, Waldenburg and
# Niedernhall all merge into the Kupferzell depot, which takes the count of trips
# "starting at the depot" on day 2 from 11 to 17. That may be right for a yard with
# satellite sites and wrong for a single gate. The run prints every cluster it merged, so
# lower this until the merges are ones you would defend.
location_tolerance_share            = 0.10
# how many nearest non-depot trips a finishing trip may be connected to. 1 keeps the
# candidate set linear in the number of trips; the direct chains (a trip that starts
# exactly where another ends) are always offered on top of this and cost no empty running.
route_nearest_link_candidates       = 1
order_data_days                     = [2, 2] # start day in trip data set (0-127), end day in trip data set (0-127)
scenario_year                       = [2025] # 2025...2045
scenario                            = ['best case'] # worst case = bev high + ice low, best case = bev low + ice high
v2g_status                          = ['on'] # variations of V2G status

# 1.3 runtime parameters
optimization_MIPGap                 = 0.1  # MIPGap 0...1
# concurrent solver processes in a sweep. v41+ models are large, so this caps how many
# Gurobi sessions run at once (licence / NAS contention). The cores left over are given
# to each solver as threads, so a low limit does not waste them - raise it only if the
# licence allows more simultaneous sessions.
parallel_worker_limit               = 4
# wall-clock cap per solve [s]. None = no cap: the solver runs until it meets
# optimization_MIPGap, however long that takes. A number stops it early and returns the
# best incumbent found, which is a *different kind of answer* - optimization_status then
# reads 'incumbent' and mip_gap says how far off it might be, and a run that found nothing
# at all comes back with None in every result field. Prefer raising optimization_MIPGap
# over capping the clock: a converged 20% answer is at least a bounded one.
optimization_time_limit_s           = None
# where Gurobi spends its effort: 0 = balanced (default), 1 = find incumbents,
# 2 = prove the bound, 3 = bound only. Measured, not guessed - see benchmarks/README.md.
#
# This was 1 whenever the fleet was crewed, on the reasoning that under the crew rules
# (3.3.17) finding *any* feasible schedule is the hard part. That is true of the model
# those rules were added to and false of this one: with V2G on, the incumbent arrives in
# the first seconds and then barely moves while the dual bound crawls - on day 2 the
# incumbent sat at 1565.18 while the bound went 1389 -> 1394 over fifty seconds. Focusing
# on incumbents aimed the solver at the half of the gap that was already closed.
#
# Over two days and three seeds each, at the settings above:
#   day 2   256 s median -> 126 s median, and the run-to-run seed spread falls 72% -> 17%
#   day 95  0 of 3 seeds reached the 10% target within 1500 s -> 3 of 3 in ~450 s
# The price is a slightly dearer schedule, +0.6 to +1%: closing the gap from the bound
# side polishes the incumbent less. On the harder day the trade runs the other way, since
# the old setting's cheaper-looking incumbents were never certified within the target.
#
# Set 1 back if you run a crewed fleet with V2G *off*, where feasibility is the binding
# difficulty again - that case is not covered by the measurement above.
optimization_MIPFocus               = 0
# degenerate simplex moves in the node LPs: -1 = auto, 0 = off. Measured, see benchmarks/.
#
# A time-indexed schedule is massively degenerate. Swapping which of two interchangeable
# trucks takes a slot, or which of several equally-priced half hours a charge sits in,
# changes the basis without changing the schedule or its cost - so the simplex has many
# bases describing the same answer and can spend its iterations walking between them. The
# original log showed 125 simplex iterations per node, which is what that looks like.
# Switching the moves off stops the solver chasing those bases.
#
# Measured as an A/B with the two halves run back to back on the same seed, because this
# machine slows by about a factor of two under sustained load and a comparison whose halves
# are hours apart measures the thermal state as much as the parameter:
#   day 2   1.52x median over seeds 1/7/13 (1.21x - 2.04x), 3 of 3 in favour, objective +0.12 %
#   day 95  1.25x median over the same seeds
# The mechanism is *cheaper nodes, not fewer of them*. On day 2 the setting explores more
# nodes than the default (7078 against 5981) at roughly twice the rate (90 against 44 per
# second), which is what a degenerate LP getting cheaper looks like. Node counts are the
# only hardware-independent figure here, so that is the claim worth trusting.
#
# Do NOT pair this with ImproveStartGap. Together they were the fastest thing measured on
# day 2 (1.63x) and *slower than this alone* on day 95, because ImproveStartGap is a 0.89x
# regression there. The combination is the one that looks best on one instance.
optimization_DegenMoves             = 0
# Gurobi's presolve: -1 = auto (default), 0 = off, 1 = conservative, 2 = aggressive.
#
# Auto is what every runtime quoted in this file was measured at, and it should stay there
# for anything being reported: the one presolve variation that was measured, aggressive, is
# a 0.75x regression, and off has never been measured at all. It is a named
# parameter rather than the literal it used to be because the two questions presolve makes
# hard to answer are worth a model_parameters(optimization_presolve=0) block:
#   * a solve that ends INF_OR_UNBD, where presolve found there is no optimum but could not
#     say which kind of no - the design search resolves that case for itself with
#     DualReductions = 0 (3.5.3), but a single day has no such path
#   * a schedule that looks wrong, where the question is whether the reductions or the
#     formulation produced it - a model solved without them answers that directly
# Both are diagnostics on a run whose runtime does not matter. Turning it off for a run
# whose runtime does is trading a large multiple for nothing.
optimization_presolve               = -1
plot_degradation_weight_curve       = False  # optional diagnostic plot; disabled for ordinary runs
plot_run_cost_parameters             = False  # selected run's realized energy/V2G price curves
# 'on' reads the roster of fleet_dataset.xlsx as a POOL rather than as the fleet: the run
# reports the subset it actually used, and the vehicle-id tie-break is dropped from the
# objective so a bias towards low ids cannot distort which vehicles get selected.
#
# What does the sizing is penalty_vehicle_use (1.5), which is in the objective either way -
# a truck earns its place only if using it saves more than it costs. This flag does not add
# that pressure, it changes what is reported and removes the id bias from the choice.
#
# The charging infrastructure is NOT sized here, whatever this flag says. No station is a
# decision variable and none carries a cost: the model caps how many trucks may charge at
# once and how much they may draw together (3.3.12), and which station each truck stood at
# is reconstructed after the solve (5.1). 'chargers_used' is therefore a measurement of the
# schedule, not a recommendation - nothing in the objective prefers fewer stations.
auto_sizing                         = 'off'

# 1.3b how a design run is solved (3.5)
#
#      A design run solves one day at a time against a candidate fleet and searches the
#      fleet lattice. The days are independent once the fleet is fixed - the SoC returns to
#      its starting level at 24:00 (3.3.6b), so the only thing tying them together is which
#      trucks were bought - which is what makes this exact rather than a heuristic: every
#      fleet it reports on has been costed by solving every one of its days, at the same
#      optimization_MIPGap a single-day run would use.
#
#      There used to be a second solver here, a combined model that stated the whole range
#      as one MILP (the old section 3.4). It was the cleaner statement of the problem and
#      it is gone because it does not scale: measured on Gurobi 13.0.3 with a 1800 s cap it
#      reached the 10 % target in 304 s for one day, missed it for two, and from three days
#      on explored a single node and returned no feasible fleet at all. Node count *fell* as
#      days were added - 6639, 1218, 1 - because the spatial root relaxation over thousands
#      of bilinear terms costs more than the whole budget. Where both finished they agreed
#      on the fleet; past that the combined model had nothing to offer. See
#      benchmarks/results_design_scaling.jsonl and results_design_compare_half.jsonl.

# The three limits below all default to "no limit", and that is the point: a design run is
# asked for the best fleet, not for a fleet by a deadline, and every one of these caps buys
# time by discarding candidates. A run that hits one has not searched the space it was
# asked to search, so each is reported in the result (search_exhausted, search_limit_hit)
# rather than absorbed silently. Set them for an exploratory run; leave them off for one
# whose answer will be used.
#
# how many candidate fleets the lattice search may cost out. None = as many as it takes;
# the wall clock below is the better handle, because what a fleet costs to evaluate varies
# with the number of days.
design_max_fleet_evals              = None
# wall clock for the whole fleet search, in seconds. None = run until the answer is proved.
#
# Not None by default, and the reason is worth stating: on a three-day half-size instance
# the lattice is 7^4 = 2401 candidate fleets, and letting it run unbounded returned *no
# answer at all* rather than a better one. A search that never finishes is the worst
# outcome of the three - worse than a proven answer, and worse than an honest "best found
# so far". So the default is finite, and a run that hits it says so (search_exhausted
# False, search_limit_hit 'search seconds') instead of implying it proved something.
#
# Sized from a measurement rather than guessed. On that same three-day instance the
# ownership cutoff leaves 308 candidate fleets to cost once the minimum fleet size is
# known, and a candidate costs about 22 s - so proving the answer takes something near
# 7000 s. An earlier 3600 s default got through 156 of the 308 and stopped half-way, which
# is precisely the state that looks like an answer and is not one. Three hours covers the
# ranges measured here with room to spare; a longer horizon needs more, because every
# candidate costs one solve per day. The knob is offered under 4 - Run so it does not have
# to be guessed from here.
design_max_search_seconds           = 7200
# cap on a single *search* day-solve. A day that runs out of time makes its whole candidate
# fleet *unknown* rather than infeasible, and an unknown fleet is skipped - so this one can
# genuinely skip the optimum, and does it quietly except through fleets_timed_out. Kept
# only so a single pathological day cannot consume the whole search.
design_search_day_limit_s           = 300
# cap on each day of the *final* re-solve of the winning fleet (3.5.7). None = run it to
# optimization_MIPGap like any other day, which is the right default: that solve is the
# answer, and stopping it early would report a schedule the run had not finished proving.
# Named rather than left implicit because the exposure multiplies by the number of days -
# an uncapped re-solve is uncapped D times - and an operator who needs a bounded total has
# to be able to see that this is where the rest of it is.
design_final_day_limit_s            = None
# cap on the full-pool dual-bound solve that gives the search its floor (3.5.4). Only
# ObjBound is read off that solve and it is valid however early it is taken, so this is a
# genuine cap rather than a compromise. It is charged once per day before the search starts,
# so a large value delays every candidate, while a tighter floor prunes harder - the trade
# runs both ways and the balance was measured rather than argued.
#
# The dual bound on the hardest day of a three-day half-size range, by cap:
#
#     30 s   60 s   120 s   300 s   600 s   900 s   1200 s   1800 s
#   1002.1 1168.0  1168.0  1209.8  1235.4  1253.8   1413.6   1432.0
#
# It does improve, but slowly, and 1800 s still leaves about 15 % to the incumbent of 1687.
# Carried through to total time-to-proof - floor cost plus the candidates the resulting
# floor leaves to cost - the curve is shallow and bottoms out around a 300 s cap:
#
#     cap    120 s   300 s   600 s   900 s   1200 s   1800 s
#   total   7016 s  6646 s  6814 s  6872 s   5720 s   6122 s
#
# 300 s is the cheap end of that basin. Spending more buys a tighter floor and pays for it
# in wall clock; the search budget above is the lever that actually decides whether the
# answer gets proved.
design_bound_limit_s                = 300
# how many day-solves of one candidate fleet may run at once (3.5.8). 1 = sequential.
# 'auto' = one worker per day of the range, capped by the cores available and by
# parallel_worker_limit, because each worker is given a share of Gurobi's threads rather
# than all of them. Parallelism does not change what is being solved, but it does change
# the thread count of each day-solve and therefore which of several equally good schedules
# a day returns - so a parallel run can differ from a sequential one inside the MIP gap.
# The fleet it settles on should not; that is checked in benchmarks/bench_design_parallel.py
# rather than assumed.
design_parallel_days                = 'auto'

# 1.4 feature parameters
fleet_input_file                    = FLEET_DATASET  # roster used verbatim; size and bev share follow from this file
external_charging_status            = 'on'  # allow charging at public stations, priced at public_charging_price_€/kWh and penalised per minute (see below)
# Lenkzeitpause, the driver's mandatory rest break (EU Regulation 561/2006 as it applies
# in Germany). Charging at a public station costs the driver time, which is what
# penalty_charging_external_time prices - unless the truck has to stand still anyway
# because a break is due, in which case the charging is free of that penalty: the time was
# already lost to the break.
driving_time_before_break_minutes   = 270  # Fahrdauer after which a Lenkzeitpause is due [min]. 4.5 h is the statutory figure. Also sets how much break a long trip carries inside it (2.6b)
# How long that break lasts, in hours. 0.75 h is the statutory 45 minutes.
#
# One parameter for one break. It used to be stated twice - as
# driving_break_duration_minutes here and again, implicitly, in the gap between the
# working-time limit and the shift limit - and two numbers for one physical fact can only
# ever drift apart. This is the single source: the Lenkzeitpause inside long trips and legs
# (2.6b), the penalty-free window it opens at a public station (3.4), and the difference
# between what a driver may *work* and how long they are committed for (driver_max_shift_
# hours, derived in 2.1.1) are all the same 45 minutes, so they all come from here.
driver_mandatory_break_hours        = 0.75
# the same break in minutes, for the places that count it that way. Derived, and rebuilt
# by 2.1.1 whenever the hours above change - defined here only so the name exists before
# the runtime context is built.
driving_break_duration_minutes      = driver_mandatory_break_hours * 60.0
# depot/local and external charging prices are NOT set here: they come from
# costs_dataset.xlsx (electricity_spot_price_€/kWh and public_charging_price_€/kWh,
# sheet 'energy_yearly') via data/cost_parameter_yearly.csv, so the Excel stays
# the single source. energy_spot_price is the public spot price of electricity, not a
# retail tariff of this depot: it carries no grid fees, levies or taxes, and it is the
# same curve the arbitrage channel sells into.
# Battery aging is always priced. It used to be a switch, and the switch was a trap: with
# it off the objective paid nothing for wear while postprocess went on computing and
# reporting degradation_cost_€ from the solved discharge, so the results carried a cost
# the optimiser had never seen and a reader had no way to tell. Beyond the reporting, an
# unpriced battery makes V2G look like free money - discharging costs the schedule nothing
# and the solver will cycle the pack as hard as the SoC bounds allow, which is not a
# scenario anyone wants to compare against, it is just a wrong one. The physical cycle
# count is reported either way (v2g_equivalent_full_cycles), so the informational value
# the switch used to offer is still there without the accounting hole.
battery_price_share                 = 0.40  # share of vehicle price attributed to battery
# the equivalent-full-cycle warranty is NOT set here: every bev states its own in
# fleet_dataset.xlsx (vehicle_battery_warranty), so degradation is priced per vehicle
depot_load_profile_file             = DEPOT_LOAD_PROFILE_CSV  # site baseline load [kW], sheet 'consumption' of depot_dataset.xlsx via hdv_depot_load_profile_generation.py
depot_pv_parameter_file             = DEPOT_PV_PARAMETER_CSV  # PV plant of the site, sheet 'generation' of depot_dataset.xlsx via hdv_depot_load_profile_generation.py
depot_charging_station_file         = DEPOT_CHARGING_CSV  # charging stations of the site, sheet 'charging' of depot_dataset.xlsx via hdv_depot_load_profile_generation.py
pv_profile_retries                  = 3     # PVGIS round trips before the run gives up on the depot PV curve
pv_profile_retry_backoff_s          = 5.0   # linear backoff between those attempts [s]
pv_allow_synthetic_profile          = 'off' # 'on' accepts the simplified bell curve when PVGIS stays unreachable. Off by default: the curve sets how much own generation reaches the trucks, so a stand-in changes every energy figure while looking like a real one
site_peak_limit_kW                  = 2000  # peak shaving: hard cap on the grid import at the depot [kW]
# the PV plant is NOT configured here: location, peak power, tilt and azimuth are the
# user inputs of depot_dataset.xlsx (sheet 'generation'), so the Excel stays the single
# source. build_runtime_context() reads them into pv_peak_power_kW, depot_latitude,
# depot_longitude, pv_tilt_deg and pv_azimuth_deg and asks PVGIS for that plant's day.
# What the depot actually pays or earns per kWh is built from that spot price and two
# overheads set here. electricity_spot_price_€/kWh is the bare public market price; nothing
# this depot does transacts at it.
#
#   every kWh bought : spot[t] + grid_energy_overhead_eur_per_kWh
#   every kWh sold   : spot[t] - energy_selling_overhead_eur_per_kWh
#
# Two rules, applied in one place each, and everything follows from them:
#
#   depot charging from the grid  ->  the buy price
#   depot charging from own PV    ->  the sell price, because that kWh could have been
#                                     sold instead and the revenue given up is its cost
#   V2G into arbitrage            ->  the sell price
#   V2G into flexibility          ->  the bare flexibility_spot_price_€/kWh, NOT the sell
#                                     price: the selling overhead prices the marketing of
#                                     an energy sale, and flexibility is paid for a service
#
# Buying carries everything the market price does not: grid fees, levies, taxes, supplier
# margin. Selling loses whatever marketing it takes: direct-marketing or platform fees,
# EEG deductions.
#
# The gap between the two is the whole PV advantage in the energy bill:
#
#   grid_energy_overhead + energy_selling_overhead  =  0.15 + 0.00  =  0.15 €/kWh
#
# and it does not depend on the spot price at all - the spot term cancels. That is why
# self-consumption pays: not because sunlight is cheap, but because a kWh that never
# crosses the meter is never taxed or tariffed.
#
# The same gap is what a V2G round trip has to overcome, since the depot buys the kWh back
# at the buy price and sold it at the sell price. Together with the round-trip conversion
# loss (1.4a) that sets the intraday spread arbitrage needs before it earns anything.
#
# Both are per *metered* kWh, i.e. measured at the charger input, so the cost of a kWh
# actually stored is the buy price divided by charging_efficiency (1.4a).
grid_energy_overhead_eur_per_kWh    = 0.15  # € per kWh on top of the spot price when buying: grid fees, levies, taxes, supplier margin
energy_selling_overhead_eur_per_kWh = 0.00  # € per kWh deducted from the spot price when selling, own PV or V2G alike: platform/direct-marketing fees, EEG

# 1.4b1 WHAT KIND OF RUN THIS IS, and who is watching it.
#
# Neither of these is a setting. They are facts about the run, written down by whichever
# entry point started it, and two rules that used to be parameters are read off them.
#
#   run_kind   'disposition' one day planned against the fleet as it stands
#              'sizing'      one fleet designed against a range of days (auto_sizing)
#              'sweep'       many solves compared against each other
#   run_host   'terminal'    a command line - nobody is looking at a screen but this one
#              'interface'   the web interface, which renders the figures itself
#
# Set by: the interface in its 3.6 / 3.6b / 3.6c, and the command line in 6.0 / 6.2a.
#
# WHAT A RUN WRITES used to be the parameter `show_outputs`, and it was the wrong shape in
# both directions. It could be switched on for a sweep, where every solve writes its
# figures to the SAME filenames - plot_suffix (5.1) is only ever set by a design run - so
# they overwrite each other run sequentially and can be read half-written run through the
# Pool. And it could be switched off for a disposition run, which is the one run whose
# whole output is those figures. Neither is a choice worth offering, so neither is offered:
#
#   a disposition run  always generates and saves its figures and its terminal report
#   a sizing or sweep  never does. Every number they report is in the summary CSV (5.9),
#                      which is timestamped per run and is the output that is safe to write
#
# and SHOWING them - opening a window - is a separate question with a separate answer: the
# web interface displays the PNGs on its own Results tab, so popping windows out of the
# server process would be both useless and wrong. A terminal run has no such page, and
# someone is sitting in front of it, so it shows them.
run_kind                            = 'disposition'  # 'disposition' | 'sizing' | 'sweep'
run_host                            = 'terminal'     # 'terminal' | 'interface'


def writes_outputs():
    """True when this run generates and saves its figures and prints its report. See 1.4b1."""
    return run_kind == 'disposition'


def displays_outputs():
    """...and opens them in a window afterwards. Only a terminal run does. See 1.4b1."""
    return writes_outputs() and run_host == 'terminal'


# 1.4b2 which sheet of costs_dataset.xlsx sets the LEVEL of the two price curves.
#
# The intraday shape always comes from sheet 'energy_daily' - it is the only place in the
# workbook that has one, and arbitrage lives on the spread inside it. That sheet states ONE
# series per price and carries no low/medium/high band: there is a single shape for a day,
# and the scenario decides the level it sits at rather than the shape it has. What this
# chooses is where that level comes from:
#
#   'daily'   the 24 hourly prices of 'energy_daily' used exactly as they are stated,
#             shape and level together. One concrete operating day, priced as the workbook
#             writes it, which is what planning a single day is about: every number in the
#             result traces back to a cell somebody can point at.
#   'yearly'  the same distribution, rescaled so its mean over the day is the scenario
#             year's value in 'energy_yearly' - that year AND that scenario band. A sizing
#             run and a sweep move both, so both have to set the level - and the intraday
#             shape has to survive it, or arbitrage would have nothing to trade and V2G
#             would be understated for every year at once. Normalising by the curve's own
#             mean and multiplying by the year's value states the rule directly, so no
#             base year has to be named.
#   'auto'    'daily' on a disposition run, 'yearly' on a sizing run or a sweep. The
#             default, and the one worth leaving alone: it reads run_kind (1.4b1), which
#             the entry point already had to set, so the basis cannot come to disagree
#             with the kind of run it is the basis for.
#
# ALL FOUR PRICES FOLLOW THE BASIS, not only the two curves. 'energy_daily' states the
# public charging price and the diesel price as well, and a disposition run is priced off
# that sheet entire: the day's diesel, the day's public charger, the day's electricity and
# flexibility curves, all as written. The two that have no intraday shape are billed as a
# single number, averaged back from the sheet in 2.1.4b - the basis decides which sheet
# states them, not whether they are flat. They are flat either way.
#
# ONE CONSEQUENCE WORTH STATING OUTRIGHT. On the 'daily' basis the scenario year and the
# scenario band reach NOTHING: all four prices come from a sheet that has neither axis, so
# a best case 2025 and a worst case 2045 disposition run are priced identically and the
# two settings survive only as labels on the result. That is what pricing one concrete day
# off one concrete day's prices means. A run meant to compare years or scenarios is a
# sweep, and a sweep is on the 'yearly' basis, where both axes set the level.
#
# The two bases agree wherever the daily curve already averages out to the band it is run
# under - which the shipped electricity curve does for 2025 'low', and no other band.
# hdv_cost_parameter_generation.py prints the factor for every band on every rebuild,
# because that factor is exactly how far a sizing or sweep solve of the base year sits
# from a disposition solve of the same day.
energy_price_basis                  = 'auto'  # 'auto' | 'daily' | 'yearly'


# 1.4b3 that choice resolved, in one place
#
# Two readers need the answer and they have to agree: section 2.5, which builds the curves,
# and the run summary (5.9), which reports which prices the run was made against. A reader
# of the CSV who cannot tell a daily-basis solve from a yearly-basis one cannot compare two
# rows of it, so the basis is a column there rather than something to be inferred from the
# run mode - and this is the function both of them call.
def resolve_energy_price_basis(kind=None):
    """Which sheet sets the level of this run's price curves: 'daily' or 'yearly'. See 1.4b2.

    kind  the run kind to answer for, when the caller knows one the module global does not
          yet - the run summary (5.9) is handed its run_mode and reports the basis that
          went with it.
    """
    if energy_price_basis in ('daily', 'yearly'):
        return energy_price_basis
    if energy_price_basis != 'auto':
        raise ValueError(
            f"energy_price_basis is {energy_price_basis!r}; it names the sheet of "
            f"costs_dataset.xlsx that sets the level of the price curves and has to be "
            f"'daily', 'yearly' or 'auto' (see 1.4b2).")
    return 'daily' if (run_kind if kind is None else kind) == 'disposition' else 'yearly'


# 1.4c drivers. Their TIME is in the optimization; their PAY is not.
#
# The crew rules - Arbeitszeit, Lenkzeit, Lenkzeitpause - are constraints of the MILP
# (3.3.17). The wage is not in the objective at all, and that separation is deliberate.
#
# A driver costs about 20 EUR an hour and a truck is away for most of the day, so on the
# shipped fleet the wage bill is the largest single number in the objective - roughly
# 1900 EUR on day 2, against a few tens of euros of V2G earnings and a similar order of
# battery wear. Everything this model exists to measure was therefore a rounding error on
# the term it was measured beside: at a 10 % MIPGap the solver may leave 190 EUR on the
# table, which is several times the entire V2G business case, so a schedule that gives up
# all of V2G and one that takes all of it are indistinguishable to the search. Worse, the
# wage is nearly a constant - the same trips need roughly the same hours however they are
# arranged - so most of what it contributed was an offset that inflated the gap's
# denominator without steering anything.
#
# What the wage did steer is kept, and kept as constraints rather than as a price. A driver
# may not work longer than driver_max_working_hours, may not drive longer than
# driver_max_driving_hours, and must stop for driver_mandatory_break_hours - and since
# 3.3.17a the shift limit is a hard rule, so no absence the model returns is one nobody
# could lawfully cover. Those are the reasons a schedule brings a truck home, and they
# remain in force. What is gone is only the euro figure on top of them.
#
# The salary is still computed and still reported: hdv_driver_scheduling builds the roster
# from the finished schedule and driver_cost_€ is what the operator pays for it, inside
# operating_cost_€ like every other real cost of the day. It is a result, not an objective.
#
# Rostered after the optimization, not inside it (see src/hdv_driver_scheduling.py): which
# truck runs which trip is an energy and cost decision the MILP is built to make, while
# covering the movements that result is a rostering problem that follows from it.
#
# A driver is tied to a vehicle exactly while that vehicle is away from the home depot, so
# the day splits into indivisible duty blocks - one per absence - and a driver may change
# vehicles between them, which by construction happens at the depot. Gaps between one
# driver's blocks are their breaks, and they are spent at the depot for the same reason.
#
# The crew rules are IN the constraints (3.3.17), not only checked afterwards, and what
# brings a truck home is the shift limit rather than a meter running on somebody's hours:
# an absence may not be longer than one lawful shift, so a truck parked in a customer yard
# is using up the only span a driver has. src/hdv_driver_scheduling.py still builds the
# roster afterwards, but from a schedule that was shaped to be crewable.
# Who is behind the wheel - and whether there is a wheel to be behind. This is a scenario
# switch, not a feature flag: it is what makes "what would this fleet cost if it drove
# itself" a question the model can answer.
#
#   'crewed'      every vehicle carries a driver. The Lenkzeit and Arbeitszeit limits of
#                 3.3.17 apply, long trips and legs carry their Lenkzeitpause (2.6b), and
#                 trips no driver could run legally are removed before the solve (2.6c).
#                 The wage is reported, not optimised (see above).
#   'autonomous'  none of the above. No crew limits, no driver cost, no mandatory breaks,
#                 and nothing is removed - a truck that needs 13 h away and 11 h of driving
#                 is simply a truck that drives for 13 hours.
#
# The difference between the two runs is the value of autonomy for this fleet, and it is
# not only the wage bill: on day 2 the crewed run has to drop five Ruhr loads (1899 km)
# that an autonomous one serves without comment.
fleet_operation_mode                = 'crewed'  # 'crewed' or 'autonomous'
# € per hour a driver is away from the depot with a vehicle. REPORTING ONLY since the wage
# left the objective (1.4c): it prices the roster that hdv_driver_scheduling builds from the
# finished schedule, and nothing the solver decides is weighed against it. Changing it
# changes driver_cost_€ and operating_cost_€ and cannot change the schedule.
driver_hourly_rate_eur              = 20.0
driver_max_driving_hours            = 9.0   # Lenkzeit: hours actually driving in one driver's day, breaks and waiting excluded
# Arbeitszeit: the working time one driver may actually perform in a day, breaks excluded.
# 9 h. This is the limit an operator sets and the only one of the three stated here.
#
# Since 2.8c it is not a purely post-hoc figure: the MILP carries a lower bound on the
# driver count derived from it (3.3.17), and the roster feedback pass (2.8c) lets the
# packing it implies reach back into the schedule.
driver_max_working_hours            = 9.0
#
# driver_max_shift_hours - the longest continuous absence from the depot, driving plus the
# breaks and waiting inside it - is NOT set here. It is derived in 2.1.1 as
#
#     driver_max_shift_hours = driver_max_working_hours + driver_mandatory_break_hours
#
# because that is what it is: the span a driver is committed for is the work they may do
# plus the break they must take while doing it. Stating it separately made it a second
# opinion about the same day, and the two disagreed - the shift limit was 12 h while the
# working limit was 10 h, so the MILP accepted a 12 h absence that the roster could not
# give to anybody who did anything else. Deriving it means an absence the optimisation
# accepts is always one a single driver can lawfully cover, which is the premise the whole
# crew side rests on (3.3.17).
#
# Setting it directly has no effect: 2.1.1 recomputes it from the two inputs above on
# every build, exactly so that the three limits cannot drift apart again.
penalty_driver_use                  = 20.0  # € per driver on the day's peak roster; a tie-break towards fewer, longer shifts rather than many short ones, in the same spirit as penalty_vehicle_use
# One re-solve against a head price the roster agrees with, when the roster turns out to
# need more drivers than the objective's peak-concurrency proxy paid for (2.8c). Costs a
# second solve of the day and can only improve the answer - both schedules are scored
# against their own rosters and the better is kept. 'off' reports the seam without trying
# to close it. Design runs ignore this either way: a day is solved per candidate fleet
# there, and doubling that would buy a better reported cost rather than a better fleet.
driver_roster_feedback              = 'on'
# € per half-hour by which a day's driving or the Lenkzeitpause runs over the limits above.
# Those two are priced rather than hard because some trips cannot be crewed legally from one
# depot at all - see 3.3.17 - and a hard rule would answer that with a bare "infeasible"
# that names nothing. At this price the solver breaks them only where no legal schedule
# exists, and the run reports every half-hour it had to.
#
# The SHIFT limit is no longer among them; see driver_shift_limit below.
penalty_crew_rule_breach            = 500.0
# The one crew limit that is a rule and not a price: no absence from the depot may be longer
# than driver_max_shift_hours (3.3.17a).
#
#   'hard'    the window constraint carries no slack. Every duty block the run returns fits
#             inside one lawful shift, so driver_blocks_over_shift is 0 by construction and
#             a roster that cannot be crewed legally cannot be reported as an answer.
#   'priced'  the older behaviour - a slack at penalty_crew_rule_breach per half hour, so
#             the solver may buy its way past the limit where nothing else works.
#
# 'hard' is the default because the escape hatch the priced version was built for is
# already covered upstream, and covered better. 2.6c removes every trip whose *cheapest*
# shape - depot, trip, depot, nothing else interfering - is longer than a shift, so after
# that filter every trip still in the model can be run inside one absence by a single
# driver. A feasible schedule therefore always exists on the shift limit alone: one trip per
# absence. The priced version was not buying feasibility, it was buying the solver
# permission to return a day that no roster could staff, and the run then said so in a
# warning nobody could act on - the trips were already legal, the schedule was not.
#
# What it can still cost is a day where the shift limit and the fleet size collide: with
# too few trucks, serving every trip may need absences that chain several trips together
# and the model has no way to drop one (3.3.1 makes every trip mandatory). That comes back
# as INFEASIBLE, and 4.1 names this parameter when it does. 'priced' is the fallback, and a
# run that needs it is a run whose fleet or whose crew limits are the finding.
driver_shift_limit                  = 'hard'   # 'hard' or 'priced'
# The crew constraints tie every step of a vehicle to every earlier one, which leaves the
# LP relaxation a poor guide and makes *finding a first feasible schedule* the hard part
# rather than closing the last few percent. So the run solves twice: once without 3.3.17,
# which is quick, and then again with it, handed the first answer as a starting point. The
# relaxed schedule already places the trips and the routes sensibly; the second solve only
# has to repair it where the crew rules bite, which is a far smaller search than building
# one from nothing.
# Off, on the measurement below rather than on the argument above. On the shipped fleet -
# 10 bev and 1 ice - the relaxed solve never converges, so every run pays the stop-loss to
# re-learn what one day already established: 97 s with the budget, 80 s without the pass at
# all, and the same answer from both (0.481 gap, 676.51 EUR operating cost). Turn it back
# on for a fleet with enough diesel to absorb the hard trips, where the relaxed solve
# converges in a second or two and does buy something; the budget below makes that safe to
# try, which is the point of it.
driver_warm_start                   = 'off'  # 'on' solves the relaxed day first as a starting point
driver_warm_start_MIPGap            = 0.30   # the relaxed solve only has to be good enough to start from, not optimal
# The stop-loss on that bet, in seconds, and the reason it exists.
#
# The paragraph above is a claim about which half of the model is hard, and it is not true
# of every fleet. Dropping the crew rules does not only remove constraints - it removes the
# ones that throw out the trips no driver could run and bound how long a vehicle may be
# out, and on an all-electric fleet of near-identical trucks those are exactly what made
# the day tractable. Measured on one day at a 50% gap: with 5 bev and 5 ice the relaxed
# solve converged in 1.6 s and the run took 29 s; with 10 bev and 1 ice the same relaxed
# solve had not reached the gap after 300 s, while the constrained model it was meant to
# help solved at the root node. Uncapped - which is the default, optimization_time_limit_s
# being None - that is not a slow run but a hang.
#
# So the warm start gets a budget rather than the clock. 15 s is ten times the good case
# and a third of a cold solve, which is what a failed bet may cost; and because the two
# outcomes are this far apart, a cap anywhere between about 10 and 30 s picks the same
# winner. Set to None to restore the old unbounded behaviour.
driver_warm_start_max_seconds       = 15
# ... and having lost the bet once, it stops betting. A sweep or a design run solves the
# same shape of day over and over, so what the first day learns about this fleet holds for
# the rest of them: paying the cap on every solve of a 32-day sweep is the same waste
# thirty-two times. Reset per process, so a new run reconsiders from scratch.
_warm_start_gave_up                 = False

# Truck toll rates (fixed € per km driven, type-specific; added to objective)
diesel_truck_toll_eur_per_km        = 0.183   # diesel/ice HDV toll rate
bev_truck_toll_eur_per_km           = 0.0     # bev HDV toll rate (often reduced/exempt)
peak_power_price_eur_per_kW         = 17  # DSO demand charge [€/kW/year] billed on the highest power the depot draws from the public grid; charged to a single day via /365 on that day's maximum
v2v_status                          = 'on'   # vehicle-to-vehicle: energy passed straight from a discharging truck to a charging one at the depot, never crossing the meter and so never paying either overhead. 'off' bills both sides against the grid as before
v2g_price_mode                      = 'both'  # which channel a discharged kWh is sold into: 'arbitrage' (electricity spot price, the same curve the truck buys at, so the earning is the intraday spread), 'flexibility' (the flexibility spot price, paid for the service rather than for the energy, and the one price the selling overhead is not deducted from), or 'both' = whichever of the two pays more in that step. 'both' is a choice, not a sum: a kWh leaves the battery once and can only be sold once.
v2g_arbitrage_price_file            = None  # optional CSV with columns time_step, price_€/MWh; without one the curve comes from energy_spot_price in costs_dataset.xlsx
v2g_flexibility_price_file          = None  # optional CSV, same format; without one the curve comes from flexibility_spot_price in costs_dataset.xlsx
block_arbitrage_extreme_slots       = 'on'  # block trip starts in highest/lowest arbitrage windows
charging_curve_status               = 'on'  # derate charging power above 80% SoC
# May a depot charger be turned down? 'off' is the older rule of 3.3.11b - a plugged-in
# truck draws the lowest of its own power, the station's and what the battery can take, and
# nothing less. That is what a dumb charger does, and it was pinned rather than bounded
# because a free E_private let the optimizer sit at a charger drawing a trickle whenever
# that flattered the peak or the price curve.
#
# 'on' allows the trickle deliberately, because it is what a managed depot actually does:
# the demand charge is billed on the highest half hour the site ever draws
# (peak_power_price_eur_per_kW, 1.4), and charging four trucks at a quarter power for two
# hours costs a quarter of the peak that charging them flat out for half an hour does, for
# the same energy. Refusing to model that makes the site look more expensive than a real
# one with a load manager in it.
#
# What it gives up is the guarantee that an occupied slot is a busy slot, and two things
# take that guarantee's place. On every run, penalty_charging_use (1.5) charges per occupied
# half hour and charging_min_energy_kWh (below) forbids holding a cable at zero, so spreading
# a charge thinner is not free. On an ASSET-SIZING run, penalty_charger_use (1.5, 1.5a) adds
# a price on how many cables the day needs at once, because there the cable count is what is
# being decided; a disposition or a sweep is charging against a station list it cannot
# change, so it carries the first two and not the third.
#
# Cheaper as well as more realistic, in model terms: the pinning needs one binary per
# (bev, step) to state min(station, battery) exactly, and 'on' writes none of them.
charging_power_modulation           = 'on'   # 'off' = the dumb-charger rule of 3.3.11b
# smallest discharge a V2G slot has to deliver [kWh per 30-min step]. A slot that is
# claimed for V2G but delivers nothing is not V2G, so the model forbids it; this is the
# threshold below which taking the slot is not worth calling a discharge. Small against
# the ~175 kWh a 350 kW truck can deliver in a step, large against the solver's
# feasibility tolerance. Raise it to state a minimum bid size the plant has to meet.
v2g_min_discharge_kWh               = 1.0
# the smallest amount a truck may take in a step it is plugged in for [kWh per 30-min
# step]. Capped by what the truck can take, so it is never asked for more than that.
#
# It holds on BOTH sides now. External charging has always used it - a truck occupying a
# public charger has to be drawing something. Depot charging joined it with
# charging_power_modulation (1.4): while the power was pinned to the maximum the floor was
# redundant, and with the power free it is the only thing between "plugged in" and "holding
# a cable at zero".
#
# 20 kWh over half an hour is 40 kW, which is roughly where a truck-scale DC charger stops
# modulating and shuts off. The figure matters more than it looks: it sets how extreme the
# peak-flattening in 1.4 is allowed to get. At 1 kWh - 2 kW - the fleet can spread a day's
# charging so thinly that it disappears underneath the site's own baseline and the demand
# charge goes to zero, which is arithmetic rather than engineering. Set it to what your
# hardware will actually hold.
#
# A truck with less headroom left than this simply does not plug in that step: the floor is
# written against z_chg, so it forbids a tiny top-up rather than making the day infeasible.
charging_min_energy_kWh             = 20.0

# 1.4a conversion losses between the meter and the battery, one figure per direction.
# Everything the model *buys, sells and meters* (E_private, E_public, E_neg) is grid-side
# energy; everything it *stores* (x_m_SoC, x_m_t_E) is battery-side. These two constants
# are the only link between the two frames (3.3.11c):
#
#     into the battery  =  charging_efficiency    * energy taken from the meter
#     out of the meter  =  discharging_efficiency * energy taken from the battery
#
# so a kWh sold through V2G costs 1/discharging_efficiency kWh of charge, and putting that
# charge back costs another 1/charging_efficiency kWh at the meter. Round trip therefore
# 0.97 * 0.93 = 90.2 %, and that spread is what the arbitrage has to beat before a V2G slot
# earns anything.
#
# The two directions are NOT the same number, which is the whole reason they are two
# parameters. Every measurement of bidirectional hardware finds the inverting direction the
# worse of the two - grid-side power quality is demanded of it and it carries a conversion
# stage the rectifying direction does not:
#
#   Sevdari et al. (2025), Sustainable Energy Technologies and Assessments 83, 104654,
#   doi:10.1016/j.seta.2025.104654 - bidirectional DC charger, ISO 15118-20 CCS2 and
#   CHAdeMO, measured at the terminals: 97 % charging and 93 % discharging at rated
#   current, falling to 80 % / 60 % at 2 A. These are the defaults below.
#
#   Apostolaki-Iosifidou, Codani & Kempton (2017), Energy 127, 730-742,
#   doi:10.1016/j.energy.2017.03.015 - the first end-to-end measurement of V2G losses and
#   the source that established the asymmetry: 83.5-99.2 % charging against 78.2-91.7 %
#   discharging on the same vehicle. Its round trip of 53-62 % is not used here; it was
#   measured on a 2017 single-phase onboard charger run mostly far below its rating, which
#   is where both papers agree the efficiency collapses.
#
# Taking the *rated-point* figures rather than a part-load curve is a property of this
# model, not a convenience: 3.3.11b pins depot charging to full power, so a plugged-in
# truck is never in the part-load region where the curve matters. The exceptions are the
# 1 kWh floors on public charging and on a V2G slot, which are small enough that the error
# they carry is well below the 10 % MIPGap.
#
# Caveat worth stating in any write-up: both measurements are car-scale converters (11 kW
# and ~10 kW). No peer-reviewed measurement of a *bidirectional megawatt-class* truck
# charger exists yet. The values are defensible for one because a 150-350 kW DC charger
# runs at its rated point here, which is where converter efficiency is highest, and they
# are converter figures - the pack's own internal losses are not in them.
charging_efficiency                 = 0.97  # meter -> battery, share of a drawn kWh that arrives as charge
discharging_efficiency              = 0.93  # battery -> meter, share of a discharged kWh that reaches the grid

# 1.4b v43 runtime opt + unified trip model + advanced degradation parameters (open todos)
# How many candidate start times per trip survive into the model. 0 = exact, every start
# the trip's window allows; N > 0 = a seeded draw of N of them, which shrinks the z set and
# with it the build and solve time.
#
# 5 is measured, not guessed. Sweeping days 2 and 3 over three seeds each:
#
#   3 samples   infeasible in 4 of 6 runs
#   4 samples   infeasible in 1 of 6 runs
#   5 samples   never infeasible; the only count to reach a 10% gap inside 7 minutes
#   6-8         never infeasible, better chaining (11 chains against 8, ~50 km less empty
#               running), but slower - 8 was still 19% off after 7 minutes where 5 was 9.9%
#   12+         no feasible solution found at all inside 7 minutes
#
# Below 5 the draw does not merely degrade the answer, it *fabricates an impossible one*:
# every trip can always be run from the depot and back, so a day that comes back infeasible
# has had the start times that make a consistent timetable removed from under it. If a run
# ever reports infeasible, raise this before believing it.
#
# Raise it to 8 when the routing itself is the object of study and the time is affordable.
# Note the draws are not nested - sample(cands, 5) and sample(cands, 8) are different
# subsets, not one inside the other - so more samples is better in expectation, not
# monotonically.
monte_carlo_samples_per_trip          = 5
monte_carlo_seed                      = 20251107  # seed for that sampling. Not cosmetic: with N=5 the draw keeps roughly a quarter of the candidate start times, so which ones it keeps decides what the solver may even consider. Unseeded, two identical runs built different models and returned different costs, and any two scenarios differed by their draw as much as by the scenario. Change it to resample; keep it fixed to compare runs.
# Depot energy as explicit one-step events is NOT a parameter, and used to look like one.
# `use_virtual_trip_model_for_v2g_and_charging` sat here offering 'off', and 'off' was not
# a model - it was a crash. Turning it off left chg_virtual_trips and v2g_virtual_trips
# empty while 3.3.5, 3.3.11, 3.3.12, 3.3.16e and the slot penalty went on indexing
# z_m_f_s[m, f'CHG_t{t}', t] unconditionally, so the build died on a gurobipy KeyError that
# named a variable rather than a setting. The old x[m,t,station] formulation the flag
# claimed to restore had already been deleted (3.3.9, 3.3.13); nothing was left to switch
# back to.
#
# A switch that cannot be switched is worse than no switch: it invites a run that cannot
# happen, and it makes the surrounding code read as though the 'off' case were handled
# somewhere. So the parameter is gone and the formulation is stated as the invariant it is.
# See 2.7c for what the events are and why they replaced the station binaries.
#
# The aging model is cycle-count and SoC-window only, and the two stressors it does NOT
# carry are assumptions about BEV trucks rather than gaps:
#
#   a) C-rate is not modelled. A truck pack is 500-900 kWh and everything connected to it -
#      charger, V2G converter, traction motor - moves it at around 1C at most, which is the
#      flat part of every rate-dependent aging curve. The steep part lives at 2-4C and
#      belongs to small packs.
#   b) Temperature is not modelled. Every modern BEV truck has a battery thermal management
#      system, so the cells sit at their conditioned setpoint rather than at ambient, and an
#      aging term in a variable held constant by design is a constant.
#
# Both are stated at the expression they justify (3.4d) and in the README; this is the note
# that the switches below configure a model with those boundaries.
advanced_degradation_status           = 'on'  # per-vehicle EFC vars + max distribution penalty + drive consumption in EFC + simple SoC weighting on throughput
degradation_distribution_penalty      = 50.0  # extra € penalty per (kWh equiv) on the worst-case vehicle EFC to force aging distribution across fleet instead of concentrating on few
soc_weight_factor                     = 0.5   # advanced aging: the discharged energy counts 1.0x at half charge and (1 + this)x at either extreme, w = 1 + 4*factor*(SoC/cap - 0.5)^2; 0 switches the weighting off entirely
soc_weight_breakpoints                = 9     # tangents used to approximate that parabola piecewise-linearly (9 -> within 1.6% of it); more is closer and slower

# 1.5 other parameters
penalty_charging_use                = 1  # € per occupied 30-min slot at a charger or on V2G; keeps the model from occupying a vehicle for a flow it does not use; tune as needed
# € per depot-charging BLOCK - per time a truck starts charging, not per step it spends
# charging. penalty_charging_use above counts steps and is therefore indifferent to how
# they are arranged: three scattered half hours and three consecutive ones cost it the
# same, so a truck that needs three steps overnight may take them at 00:30, 03:00 and
# 05:30 with hours of standing in between. Nothing else separates those either - depot
# charging is priced per kWh at that step's rate, and the price curve holds one value for
# 28 steps of the day, so moving a kWh inside a price window costs exactly nothing and
# every arrangement is an alternative optimum. The solver returns whichever it reached
# first, which is why the figure looks scattered.
#
# This term is what says otherwise, and it is a real cost rather than a tidiness knob: a
# plug-in is somebody walking to the truck and connecting a cable, and doing that three
# times is three times the handling of doing it once.
#
# Measuring it found little to buy, which is why it is small rather than absent. On day 93
# the fleet already makes about one visit per truck, and pricing them does not move that:
#
#     0 EUR a visit -> 12 arrivals      10 EUR -> 12 arrivals
#     2 EUR a visit -> 17 arrivals      50 EUR -> 11 arrivals
#
# 17 at 2 EUR is above the unpriced baseline, which is the tell: that is seed noise across
# alternative optima, not a dose-response, and a term whose effect is smaller than the noise
# is not steering anything. The scattering that motivated this is mostly not scattering of
# *visits* at all - it is charge and discharge alternating inside one long connected period,
# which is the arbitrage round trip working as intended.
#
# 5 EUR is therefore set as a handling charge rather than as a lever: it is roughly what a
# few minutes of somebody walking to a truck and connecting a cable is worth, it is enough
# to separate two arrangements the price curve cannot separate, and it is small against the
# day's energy bill - pushed hard enough this term will hold a truck on a charger through a
# dearer step to avoid a second visit, which is a distortion and not a preference.
#
# It still costs nothing while it is zero: no variables and no rows are written, and the
# model is byte-identical to one built without it. Check `charging_blocks` in the run
# summary against one visit per truck as the yardstick before raising it.
penalty_charging_block              = 5  # € per visit to a depot charger; 0 = off
# € per charger the day needs AT ITS BUSIEST, not per charger-hour. The same shape as
# penalty_driver_use: what a depot buys is a number of cables, and that number is set by
# the worst half hour of the day, so the peak is the thing worth pricing and the hours
# around it are free once it is paid for.
#
# It is the counterweight to charging_power_modulation (1.4). With the power free a truck
# would rather charge slowly for six hours than quickly for one - same energy, lower peak,
# no cost - and a depot that plans that way needs a cable for every truck at once. This
# makes the sixth simultaneous connection cost something, so the fleet takes the slowest
# charge that does not need another cable rather than the slowest charge there is.
#
# Not a bill: the model does not cost charging hardware (3.3.12 has no station variables at
# all), so this is a steering term and is reported as one. A day-rate is the right scale -
# a 600 kW DC charger amortised over its life is tens of euros a day - and 20 EUR puts it
# beside penalty_driver_use, which prices the same kind of thing for people.
#
# ASSET SIZING ONLY. See charger_use_price() below for why, and for the one place the rule
# is written down.
penalty_charger_use                 = 20.0  # € per charger at peak concurrency, asset-sizing runs only; 0 = off
penalty_vehicle_use                 = 10  # € per used vehicle; tune as needed
penalty_charging_external_time      = 10  # Strafe pro Minute externes Laden [€/min]: what a minute spent charging at a public station costs beyond the energy - driver time, the detour, the tour not driven. Waived inside a Lenkzeitpause, where the truck stands still regardless. At 30 min per step this is 300 €/step, so it dominates the energy price; tune as needed
# day-boundary SoC of every bev, as a fraction of its own capacity: the level each one
# starts the day with at 00:00 and, at the same time, the level it has to have reached
# again at 24:00 (see 3.3.6b). One figure for both ends, so the day is repeatable.
initial_soc_fraction                = 0.5


# 1.5a which runs the charger-use penalty applies to, in one place
#
# penalty_charger_use above prices the number of cables the day needs at its busiest. That
# is a question about what the depot should BUY, and only one kind of run is asking it:
#
#   asset sizing (auto_sizing = 'on')  the depot is a design variable in all but name. The
#                                      run charges against an infrastructure built to fit
#                                      (3.5) and reports what the schedules turned out to
#                                      need, so a price on concurrency is what keeps the
#                                      answer from being "one cable per truck".
#   disposition / sweep (off)          the depot is given. Its stations are read verbatim
#                                      from the `charging` sheet, 3.3.12(a) already forbids
#                                      more trucks plugged in at once than there are
#                                      stations, and no decision in the run can change that
#                                      number. Pricing it there buys nothing and costs
#                                      something: it is a charge on using hardware the
#                                      operator has already paid for, so it pushes the
#                                      fleet into serial charging - and with
#                                      charging_power_modulation on, serial charging is the
#                                      one thing that raises the peak the demand charge is
#                                      billed on. It also sat in steering_penalties_€ on
#                                      every day of a sweep, where it moves the objective
#                                      and nothing else.
#
# Written as a function rather than as an `if` at each site because there are two sites -
# the objective (3.4c) and the reconciliation that checks it (5.8b) - and they have to
# agree or the run reports a residual that is really just the two of them disagreeing about
# whether the term was there. Both read the auto_sizing they were handed, not the module
# global: a design run builds and reports each day with auto_sizing = 'on' explicitly
# (3.5.0a, 3.5.7) while the module's own flag may still say 'off'.
def charger_use_price(sizing):
    """`penalty_charger_use` on an asset-sizing run, 0 on any other. See 1.5a."""
    return penalty_charger_use if str(sizing) == 'on' else 0.0


# 1.5b Slack notification when a run finishes. Off unless asked for - the interface has a
#      switch for it under "2 - Settings", and a sweep turns it on here.
#
#      The token is NOT a parameter of this model and is deliberately not one: it is read
#      from the SLACK_BOT_TOKEN environment variable at the moment a message is sent. A
#      token written into the source is a token in every copy of it - every backup, every
#      benchmark container, every clone - and rotating it then means finding all of them.
#      The environment is the one place it can sit that the code does not carry around.
#      On Windows: set it once as a user environment variable named SLACK_BOT_TOKEN and
#      restart the terminal, or run  setx SLACK_BOT_TOKEN "xoxb-..."
slack_notification_status           = 'off'
slack_notification_channel          = 'python-updates'
SLACK_TOKEN_ENV                     = 'SLACK_BOT_TOKEN'


def slack_token():
    """The bot token from the environment, or '' if it is not set there."""
    return os.environ.get(SLACK_TOKEN_ENV, '').strip()


def send_slack_notification(text, channel=None):
    """Post `text` to Slack. Returns (sent, detail) and never raises.

    A notification is the last thing a run does and the least important thing it does: a
    finished optimization that cannot announce itself is still a finished optimization.
    So a missing token, an unreachable Slack or a channel the bot was never invited to
    are all *reported* - the caller decides whether to print or display the reason - and
    none of them is allowed to throw away a result that took minutes to compute.
    """
    if slack_notification_status != 'on':
        return False, 'notification is switched off'
    token = slack_token()
    if not token:
        return False, f'{SLACK_TOKEN_ENV} is not set in the environment'
    target = (channel or slack_notification_channel or '').lstrip('#')
    if not target:
        return False, 'no channel set'
    try:
        from slack_sdk import WebClient    # only imported when a message is actually sent
        WebClient(token=token).chat_postMessage(
            channel=target, text=text, username='Python Notification')
    except ImportError:
        return False, 'slack_sdk is not installed (pip install slack_sdk)'
    except Exception as exc:
        # slack_sdk raises SlackApiError for a refused post - a bad token, a channel the
        # bot is not in - and the message inside it is the only thing that says which.
        # Folded onto one line: it arrives with the response dict on a line of its own,
        # and the interface shows this in a single-line warning box
        return False, f"{type(exc).__name__}: {' '.join(str(exc).split())}"
    return True, f'sent to #{target}'


# 1.6 parameter.py - the one file a run needs anyone to open
#
# Everything above is a default. parameter.py, beside main.py, carries the same names in a
# compact block and replaces whichever of them it sets, so a run is configured by editing
# one short file instead of reading through this one.
#
# Applied HERE, at import, and deliberately not from a __main__ block: the sweep starts its
# pool with 'spawn' (6.2), so every worker re-imports this module from scratch. A file read
# in the parent only would leave the parent reporting it as applied while the workers solved
# against these module-level defaults - the quietest way this model can be wrong. Import is
# the one point every process passes through.
#
# An unknown name raises rather than being dropped, for the reason model_parameters() gives:
# there would be nothing for it to configure, and a silently ignored line in a parameter
# file is a run that is not the run anyone asked for.
PARAMETER_FILE = PROJECT_ROOT / 'parameter.py'


def _apply_parameter_file():
    """Overlay PARAMETER_FILE onto the defaults above. Returns the names it set."""
    if not PARAMETER_FILE.is_file():
        return []
    import types
    import importlib.util
    spec = importlib.util.spec_from_file_location('hdv_parameter_file', PARAMETER_FILE)
    file_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(file_module)
    wanted = {name: value for name, value in vars(file_module).items()
              if not name.startswith('_') and not isinstance(value, types.ModuleType)}
    unknown = sorted(name for name in wanted if name not in globals())
    if unknown:
        raise KeyError(
            f"{PARAMETER_FILE.name} sets {unknown}, which "
            f"{'is not a parameter' if len(unknown) == 1 else 'are not parameters'} of this "
            f"module. Check the spelling against sections 1.3-1.5 - a name that configures "
            f"nothing here would otherwise be ignored without saying so.")
    globals().update(wanted)
    # the one parameter above that is computed from another (1.4c). Recomputed, or a file
    # that sets the break length would leave the two disagreeing for the rest of the run.
    if 'driver_mandatory_break_hours' in wanted:
        globals()['driving_break_duration_minutes'] = driver_mandatory_break_hours * 60.0
    return sorted(wanted)


parameter_file_applied = _apply_parameter_file()
# Said once, in the parent only: a spawned worker applies the same file and would otherwise
# repeat this line once per process, and the interface imports this module too.
if parameter_file_applied and multiprocessing.current_process().name == 'MainProcess':
    print(f"{PARAMETER_FILE.name}: {len(parameter_file_applied)} parameter(s) applied over "
          f"the defaults of {Path(__file__).name}")


# 1.7 every value derived from the parameters above is built by
#     build_runtime_context() once the helper functions are defined (section 2.1)

# 1.10 function to convert time to time-step
#
# One convention for the whole file. This used to floor (minutes // STEP_MINUTES) while
# parse_day_time below rounded to the nearest half hour, and the two were applied to
# different inputs of the same run: trip windows from the order data went through here,
# the working hours through there. An off-grid 18:45 therefore narrowed a trip window to
# step 37 (18:30) in one subsystem and widened the working day to step 38 (19:00) in the
# other - a silent, per-input disagreement about what the same clock time means.
#
# Rounding is the one that survives, because it is the one that can be stated: a time is
# placed on the nearest step of the grid, whichever side it falls. The trip generator
# already snaps every order window onto the grid (hdv_trip_generation), so on the shipped
# data nothing moves; this only decides what happens to an input that was never on it.
def time_to_step(time_str):
    """Time of day -> index on the 30-min grid, by the single convention of parse_day_time."""
    return parse_day_time(time_str, 'trip window')

def parse_day_time(value, label):
    """Time of day -> index on the 30-min grid (0 = 00:00 ... 48 = 24:00).

    Accepts 'HH:MM', a datetime.time (as the web interface produces) or a number of
    hours. Off-grid values are rounded to the nearest half hour.
    """
    if isinstance(value, str):
        try:
            hours, minutes = (int(part) for part in value.strip().split(':'))
        except ValueError:
            raise ValueError(f"{label} must look like 'HH:MM', got {value!r}.") from None
    elif hasattr(value, 'hour') and hasattr(value, 'minute'):
        hours, minutes = value.hour, value.minute
    elif isinstance(value, (int, float)):
        hours, minutes = int(value), round((value - int(value)) * 60)
    else:
        raise ValueError(f"{label} must be 'HH:MM', a time or a number of hours, got {value!r}.")

    step = round((hours * 60 + minutes) / STEP_MINUTES)
    if not 0 <= step <= STEPS_PER_DAY:
        raise ValueError(f"{label} must be within 00:00...24:00, got {value!r}.")
    return step


# 1.11 function to convert time-steps in time
def step_to_time(step):
    total_minutes = step * STEP_MINUTES
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


# 1.11b driving hours -> steps of the grid, always rounded up
#
# A router returns real hours and the model owns half-hour steps, so every duration has to
# be placed on the grid, and the direction that happens in is a modelling decision rather
# than a detail of arithmetic.
#
# It rounds up. A 1.24 h trip is booked as 1.5 h, not as 1.0 h. Rounding to the nearest
# step used to be the rule here and it is the wrong one: it hands the schedule up to 14
# minutes per trip that the truck does not have, and it does so silently and in the
# optimistic direction. The consequences are not small at the end of a chain - the vehicle
# is released early for the next trip, so a chain that does not fit passes the overlap
# constraint; the driving that the Lenkzeit counter sees is short by the same amount; and
# the distance-per-step is billed over fewer steps than the trip occupies.
#
# Rounding up costs the opposite error - at most one step of slack a real trip would not
# need - and that error is the safe one: a schedule that holds with it also holds without.
# It is also what the empty legs have always done (2.6a), so loaded trips and deadheading
# are now on one rule instead of two.
#
# The floor of one step is what stops a trip of a few minutes from occupying nothing at
# all: zero steps would give it no occupancy, no distance per step and no overlap
# protection while still satisfying "every trip is assigned once". The trip generator's
# MIN_TRIP_DURATION_H keeps such trips out of the data, and this keeps them harmless if
# one ever arrives anyway.
def duration_to_steps(hours):
    """Steps a stretch of driving occupies on the grid, rounded up (never below one)."""
    value = float(hours)
    if value <= 0:
        return 0
    return max(1, math.ceil(value / STEP_HOURS))


def load_depot_intraday_profile(filepath, steps, for_date=None):
    """Depot baseline load [kW] per 30-min step, for one calendar day.

    The day is matched on **day and month, ignoring the year**: sheet 'consumption' of
    depot_dataset.xlsx is a metered year (2021 in the shipped data) and the disposition
    date is whatever day is being planned, so the two can never share a year. What they can
    share is the date - 7 November is 7 November, and the load a depot draws on it is a
    property of the season and the weekday pattern, not of which year it was measured in.

    This matters more than it sounds. Averaging the whole year into one curve - which is
    what this did before - hands a January Sunday and a July Tuesday the same baseline, and
    the baseline is what the fleet's charging is stacked on top of. It sets the grid peak
    the demand charge is billed on, and it decides how much of the PV generation the site's
    own load has already eaten before a truck sees any of it. A flat annual average quietly
    removes every seasonal effect the disposition date was chosen to capture.

    With several years in the file the same date is averaged across them, which is the
    reading that treats extra years as repeat measurements of the same day rather than as
    more days.
    """
    path = Path(filepath)
    if not path.is_absolute():
        path = project_path(filepath)
    require_input(path, 'src/hdv_depot_load_profile_generation.py')
    df = pd.read_csv(path, parse_dates=['date'], encoding=CSV_ENCODING)
    df['step'] = (df['date'].dt.hour * STEPS_PER_HOUR
                  + (df['date'].dt.minute // STEP_MINUTES))

    scope = f"{path.name}"
    if for_date is not None:
        same_day = df[(df['date'].dt.month == for_date.month)
                      & (df['date'].dt.day == for_date.day)]
        if same_day.empty:
            available = df['date'].dt.strftime('%d.%m').nunique()
            raise ValueError(
                f"{path.name} has no reading for {for_date.strftime('%d.%m')} - the "
                f"disposition date is not a date the depot's own metering covers, though "
                f"it covers {available} other(s). Either set date_disposition to a day the "
                f"'consumption' sheet of depot_dataset.xlsx contains, or extend that sheet. "
                f"A 29 February disposition against a non-leap metering year fails here.")
        df = same_day
        scope = f"{path.name} on {for_date.strftime('%d.%m')}"

    grouped = df.groupby('step')['power_kW'].mean()
    # a step the profile never covers is a hole in the measurement, not an hour in which
    # the depot drew nothing. Reading it as 0 kW understates the grid peak and overstates
    # the PV surplus left for the trucks, so it is rejected instead.
    missing = [step_to_time(t) for t in steps if t not in grouped.index or pd.isna(grouped.get(t))]
    if missing:
        raise ValueError(
            f"{scope} has no depot load for the time(s) of day {', '.join(missing)}. "
            f"Every 30-min step of the day has to be covered; regenerate it from "
            f"depot_dataset.xlsx.")
    return [float(grouped.loc[t]) for t in steps]


def load_depot_pv_parameters(filepath=None):
    """PV plant of the depot [dict], from data/depot_pv_parameters.csv.

    The five numbers originate in sheet 'generation' of inputs/depot_dataset.xlsx: they
    are the user inputs the PVGIS query is built from. Reading them from the derived
    file rather than the Excel keeps the model on the same footing as the depot load
    and the energy prices - every primary dataset is converted by its own generator.
    """
    path = Path(filepath or depot_pv_parameter_file)
    if not path.is_absolute():
        path = project_path(path)
    require_input(path, 'src/hdv_depot_load_profile_generation.py')
    table = pd.read_csv(path, encoding=CSV_ENCODING)
    if table.empty:
        raise ValueError(f"{path.name} contains no row; regenerate it from depot_dataset.xlsx.")
    row = table.iloc[0]
    required = ('pv_latitude_deg', 'pv_longitude_deg', 'pv_peak_power_kW',
                'pv_tilt_deg', 'pv_azimuth_deg')
    missing = [column for column in required if column not in table.columns]
    if missing:
        raise ValueError(f"{path.name} lacks the column(s) {missing}; regenerate it with "
                         "src/hdv_depot_load_profile_generation.py.")
    return {column: float(row[column]) for column in required}


def load_depot_charging_stations(filepath=None):
    """Charging stations of the depot, from data/depot_charging_stations.csv.

    Returns (ids, powers_kW) in sheet order, both as plain lists. The rows originate in
    sheet 'charging' of inputs/depot_dataset.xlsx and are used verbatim: the depot has as
    many stations as that sheet has rows, each capped at its own kW. The station list is
    therefore a property of the dataset, not a parameter of the run.
    """
    path = Path(filepath or depot_charging_station_file)
    if not path.is_absolute():
        path = project_path(path)
    require_input(path, 'src/hdv_depot_load_profile_generation.py')
    table = pd.read_csv(path, encoding=CSV_ENCODING)
    if 'charger_power_kW' not in table.columns:
        raise ValueError(f"{path.name} lacks the column 'charger_power_kW'; regenerate it "
                         "with src/hdv_depot_load_profile_generation.py.")
    if table.empty:
        raise ValueError(f"{path.name} lists no charging station; the depot needs at least "
                         "one row in sheet 'charging' of depot_dataset.xlsx.")
    powers = [float(p) for p in table['charger_power_kW']]
    if any(p <= 0 for p in powers):
        raise ValueError(f"{path.name} contains a charging station with a power <= 0.")
    if 'charger_id' in table.columns:
        ids = [str(i) for i in table['charger_id']]
    else:
        ids = [str(i) for i in range(1, len(powers) + 1)]
    return ids, powers


# where the PV curve of the last build came from: 'pvgis', 'cache' or 'synthetic'.
# Reported with the results, so a run made without a real curve says so.
pv_profile_source = None


def pvgis_aspect(azimuth_deg):
    """Compass azimuth of the modules -> the 'aspect' PVGIS expects."""
    from hdv_pv_profile_generation import pvgis_aspect as _pvgis_aspect
    return _pvgis_aspect(azimuth_deg)


def generate_pv_intraday_profile(steps, peak_kW, date, lat, lon,
                                 tilt_deg=35.0, azimuth_deg=180.0):
    """Intraday PV power profile [kW] for the disposition date and the depot plant.

    peak_kW, lat, lon, tilt_deg and azimuth_deg are the user inputs of
    depot_dataset.xlsx (sheet 'generation'); the date is the disposition date, which
    selects the day of year the curve is taken from. The year itself is fetched once
    by hdv_pv_profile_generation and stored in data/cache_pv_profile.json under a
    fingerprint of order_dataset.xlsx and depot_dataset.xlsx, so every day in
    trips.csv is already there before a run asks for one.

    A PVGIS round trip that fails is retried; one that keeps failing raises. The curve
    decides how much of the depot's own generation reaches the trucks, so a synthetic
    stand-in would change every energy figure of the run while looking exactly like a real
    one. Set pv_allow_synthetic_profile = 'on' to accept the simplified bell curve anyway;
    the run then says so, and pv_profile_source records it in the result table.
    """
    global pv_profile_source
    from hdv_pv_profile_generation import generate_pv_intraday_profile as _generate_pv
    profile, source = _generate_pv(
        steps, peak_kW, date, lat, lon, tilt_deg, azimuth_deg,
        retries=pv_profile_retries,
        retry_backoff_s=pv_profile_retry_backoff_s,
        allow_synthetic=pv_allow_synthetic_profile)
    pv_profile_source = source
    return profile


def load_v2g_price_curve(filepath, steps, fallback_per_step):
    """Override a channel's price curve [€/MWh] from a CSV; else keep the given curve.

    fallback_per_step is already one price per time step - the curve build_runtime_context
    derived from costs_dataset.xlsx. It used to be six 4h-period values indexed here as
    fallback[t // 8]; keeping that indexing against a per-step list silently read only its
    first six entries and flattened the rest of the day onto them.
    """
    if len(fallback_per_step) != len(steps):
        raise ValueError(
            f"the V2G price curve has {len(fallback_per_step)} entries for "
            f"{len(steps)} time steps - it has to carry one price per step.")
    fallback = list(fallback_per_step)
    if filepath is None:
        return fallback
    path = Path(filepath)
    if not path.is_absolute():
        path = project_path(filepath)
    if not path.exists():
        return fallback
    df = pd.read_csv(path, encoding=CSV_ENCODING)
    if 'time_step' not in df.columns:
        raise ValueError(
            f"{path.name} has no 'time_step' column, so there is no way to tell which step "
            f"each price belongs to. The file has to carry the columns 'time_step' and "
            f"'price_€/MWh'; it carries {list(df.columns)}.")
    # named, not positional. This used to take df.iloc[:, -1] - "the last column" - which
    # reads whatever happens to sit at the right-hand end of the file: a units column, a
    # source note, a comment somebody added in Excel. The run then priced every V2G kWh on
    # that column, silently and with no type error, because a text column coerced through
    # float() only fails on the rows it actually reaches. Every other loader in this file
    # validates its columns by name and this one is now no exception.
    price_columns = [c for c in df.columns if c != 'time_step']
    named = [c for c in price_columns if 'price' in str(c).lower()]
    if len(named) == 1:
        price_column = named[0]
    elif len(price_columns) == 1:
        price_column = price_columns[0]
    else:
        raise ValueError(
            f"{path.name} does not say which of its columns is the price: it carries "
            f"{price_columns} beside 'time_step'. Name exactly one of them 'price_€/MWh' "
            f"(any column whose name contains 'price' is accepted), or leave the file with "
            f"the two documented columns only.")
    price_map = dict(zip(df['time_step'].astype(int),
                         pd.to_numeric(df[price_column], errors='raise')))
    return [float(price_map.get(t, fallback[i])) for i, t in enumerate(steps)]


# how long one arbitrage window is. The dearest and the cheapest window of the day are the
# ones a trip start is kept out of, so the truck is free to sell into the first and to buy
# into the second. Four hours is the block length the intraday market is usually read in.
ARBITRAGE_BLOCK_HOURS = 4


def get_arbitrage_blocked_steps(arbitrage_prices, steps):
    """Identify highest and lowest 4h arbitrage windows; block trip starts there.

    Written against the step grid rather than against the literals it happens to produce.
    This used to say `t // 8` and `range(6)` - the 8 and the 6 being 4 h of 30-min steps
    and the 6 such blocks a 24 h day holds - which is the one place in the file that broke
    the 1.0a rule that step-grid literals live only in the STEP_* constants. It also
    indexed `arbitrage_prices[t]` by the step *value* while everything around it is written
    against the abstract `steps` list, so the two agreed only because `steps` happens to be
    range(48). Both are now derived, and a price series that does not match the step list
    is refused instead of read off the end.
    """
    if len(arbitrage_prices) != len(steps):
        raise ValueError(
            f"the arbitrage curve has {len(arbitrage_prices)} entries for {len(steps)} time "
            f"steps - it has to carry one price per step.")
    block_steps = max(1, int(ARBITRAGE_BLOCK_HOURS * STEPS_PER_HOUR))
    # positional, so the prices and the steps are read through the same index
    blocks = {}
    for position, step in enumerate(steps):
        blocks.setdefault(position // block_steps, []).append((step, arbitrage_prices[position]))
    if not blocks:
        return set()
    means = {block: sum(price for _s, price in entries) / len(entries)
             for block, entries in blocks.items()}
    high_block = max(means, key=means.get)
    low_block = min(means, key=means.get)
    # A flat curve has no extreme window to keep clear, and max() and min() would both
    # answer "the first block" - closing the day's first four hours to trip starts for no
    # reason anybody could name. It takes a band of energy_daily that does not move across
    # the day to get here (a pure scaling cannot flatten one that does, 1.4b2), which is
    # not how the shipped workbook is written - but it is one line to be right about.
    if means[high_block] == means[low_block]:
        return set()
    return {step for block in {high_block, low_block} for step, _p in blocks[block]}


def soc_aging_weight_tangents(weight_factor=None, breakpoints=None):
    """Tangent lines of the quadratic aging weight, as (x, w, slope) triples.

    The weight itself is
        w(x) = 1 + 4 * soc_weight_factor * (x - 0.5)^2,   x = SoC / capacity
    1.0 at half charge and 1 + soc_weight_factor at either extreme. It is convex, so the
    maximum of its tangents is a piecewise-linear approximation from below that needs
    nothing but linear constraints - no quadratic constraint, no SOS2, no extra binaries.
    That matters: carrying the square itself made the model quadratically constrained and
    cost far more solver time than the shape is worth (see 3.4).
    """
    weight_factor = soc_weight_factor if weight_factor is None else weight_factor
    breakpoints = soc_weight_breakpoints if breakpoints is None else breakpoints
    breakpoints = max(2, int(breakpoints))
    tangents = []
    for index in range(breakpoints):
        x = index / (breakpoints - 1)
        w = 1.0 + 4.0 * weight_factor * (x - 0.5) ** 2
        slope = 8.0 * weight_factor * (x - 0.5)
        tangents.append((x, w, slope))
    return tangents


def soc_aging_weight(soc_fraction, weight_factor=None, breakpoints=None):
    """The aging weight the model actually charges at a given SoC fraction.

    Evaluates the same piecewise-linear approximation the objective is built from, so the
    reported degradation cost is the one that was optimised against rather than the exact
    parabola the approximation stands for.
    """
    return max(w + slope * (soc_fraction - x)
               for x, w, slope in soc_aging_weight_tangents(weight_factor, breakpoints))


def degradation_cost_per_efc(vehicle_price, warranty_cycles):
    """€ per equivalent full cycle for one vehicle.

    The battery's share of the acquisition price is amortized over the equivalent full
    cycles the manufacturer warrants for that specific vehicle
    (vehicle_battery_warranty in fleet_dataset.xlsx). Only the battery share is
    amortized, not the whole truck: reaching the warranty limit retires the battery,
    not the vehicle. There is no fleet-wide fallback - normalize_fleet rejects a roster
    that leaves the warranty of a bev unset.
    """
    if warranty_cycles is None or pd.isna(warranty_cycles) or warranty_cycles <= 0:
        raise ValueError(f"A positive vehicle_battery_warranty [equivalent full cycles] "
                         f"is required to price degradation, got {warranty_cycles!r}.")
    return (vehicle_price * battery_price_share) / warranty_cycles


# 2.1 build every value derived from the parameter block above
def build_runtime_context():
    """(Re)compute all derived module globals from the current parameter values.

    Called once at import time. Any caller that changes a parameter afterwards
    (for example the web interface) calls this again so that the derived state
    can never drift out of sync with the configuration it came from.
    """
    global time_steps, driving_break_duration_minutes, driver_max_shift_hours, driving_break_duration_steps, driving_time_before_break_steps, work_start_step, work_end_step, fleet_dataset, all_trips, trips_dataset_amount, day_dates, date_disposition, cost_parameters_energy, cost_parameters_v2g, depot_baseline_load_kW, pv_generation_kW, pv_charging_available_kWh, pv_site_parameters, pv_peak_power_kW, depot_latitude, depot_longitude, pv_tilt_deg, pv_azimuth_deg, charging_infrastructure, charging_station_ids, total_iterations, max_parallel_workers, multiprocessing_status, gurobi_threads, v2g_by_hour, v2g_channel_curves, daily_flat_prices

    # the context is not built until this function returns. Set here as well as at the
    # bottom, so an exception on the way through leaves the module marked UNbuilt rather
    # than half-initialised and marked built - see the note at the end of this function.
    global _runtime_context_built
    _runtime_context_built = False

    # 2.1.0 whatever this model reads from data/ is derived from inputs/, so build any
    #       part of it that is not on disk instead of demanding it from the caller
    ensure_derived_inputs()

    # 2.1.1 time grid, fleet roster and trip set
    #
    # The grid is the invariant of 1.0a, not a derived value, so it is checked rather than
    # recomputed. The check exists because this function is what the web interface calls
    # after applying its overrides: an override of STEP_MINUTES or STEP_HOURS used to be
    # accepted by the namespace and then quietly overwritten here, so a caller could ask
    # for a different step length, see no complaint, and get a run on the old grid. Now it
    # is refused in as many words.
    if (STEP_MINUTES != 30 or STEP_HOURS != 0.5
            or STEPS_PER_HOUR != 2 or STEPS_PER_DAY != 48):
        raise ValueError(
            f"The 30-minute time grid is an invariant of this model, not a parameter "
            f"(1.0a), and it cannot be overridden: got STEP_MINUTES={STEP_MINUTES}, "
            f"STEP_HOURS={STEP_HOURS}, STEPS_PER_HOUR={STEPS_PER_HOUR}, "
            f"STEPS_PER_DAY={STEPS_PER_DAY}. The depot load profile, the PV profile and "
            f"trips.csv are all generated on this grid by their own modules; changing the "
            f"step length here alone would read them against the wrong clock and return a "
            f"complete, plausible, wrong answer.")
    time_steps                          = list(range(0, STEPS_PER_DAY))
    # the mandatory break in minutes, for the places that count it that way
    driving_break_duration_minutes      = driver_mandatory_break_hours * 60.0
    # Lenkzeitpause on the 30-min grid. The break window is rounded up, so a 45-min break
    # covers two steps rather than being clipped to one; the driving time that triggers it
    # is rounded down, so a trip is never denied a break it has legally earned.
    driving_break_duration_steps        = max(1, math.ceil(driving_break_duration_minutes / STEP_MINUTES))
    driving_time_before_break_steps     = max(1, int(driving_time_before_break_minutes // STEP_MINUTES))
    # 2.1.1b the shift span, derived rather than stated (see the parameter block).
    #
    # The span a driver is committed for is the work they may do plus the break they must
    # take while doing it. Deriving it is what keeps the three crew limits consistent:
    # span >= duty >= wheel holds by construction, and an absence the optimisation accepts
    # is always one a single driver can lawfully cover.
    #
    # Plus the break AS THE MODEL IMPOSES IT, not as the statute states it. The break is
    # rounded up onto the 30-min grid a line above - 45 minutes becomes two steps, a full
    # hour - because half a step of rest is not a thing this grid can express and rounding
    # down would grant less rest than the law. Deriving the span from the unrounded 0.75 h
    # then contradicted that: a day at the full 9 h Lenkzeit costs 18 steps of driving plus
    # the 2 steps of rest it obliges, which is 20 steps against a span of floor(9.75/0.5)
    # = 19. Every maximum-driving day was a shift breach, and a trip that needs one was
    # dropped as uncrewable, on nothing but the quarter hour between the two roundings.
    # One rounding, used by both.
    driver_max_shift_hours              = (driver_max_working_hours
                                           + driving_break_duration_steps * STEP_HOURS)
    if driver_max_working_hours <= 0:
        raise ValueError(
            f"driver_max_working_hours must be positive, got {driver_max_working_hours!r}. "
            f"It is the working time one driver may perform in a day and the shift span is "
            f"derived from it.")
    if driver_mandatory_break_hours < 0:
        raise ValueError(
            f"driver_mandatory_break_hours must not be negative, got "
            f"{driver_mandatory_break_hours!r}.")
    # the Lenkzeit is driving alone, so it cannot exceed the working time that contains it
    if driver_max_driving_hours > driver_max_working_hours:
        raise ValueError(
            f"driver_max_driving_hours ({driver_max_driving_hours:g} h) exceeds "
            f"driver_max_working_hours ({driver_max_working_hours:g} h). Driving is part of "
            f"the working time, not additional to it, so the Lenkzeit cannot be the larger "
            f"of the two - with these values the driving limit could never bind and the "
            f"run would silently plan days no driver may work.")
    # daily working hours: the outer window every trip has to start and finish within.
    # It only constrains driving - depot charging and V2G stay available around the clock.
    work_start_step                     = parse_day_time(work_hours_start, 'work_hours_start')
    work_end_step                       = parse_day_time(work_hours_end, 'work_hours_end')
    if work_start_step >= work_end_step:
        raise ValueError(f"work_hours_start ({work_hours_start}) must be earlier than "
                         f"work_hours_end ({work_hours_end}).")
    # conversion losses (1.4a). Checked here rather than in the model build: a value of 0
    # divides by zero in the V2G branch, and a value above 1 is a battery that returns more
    # than it was given - the solver would happily find that free energy and the run would
    # come back with a plausible-looking schedule paid for by it.
    for name, value in (('charging_efficiency', charging_efficiency),
                        ('discharging_efficiency', discharging_efficiency)):
        if not (0.0 < float(value) <= 1.0):
            raise ValueError(f"{name} is {value}; it is the share of a kWh that survives "
                             f"the conversion and has to lie in (0, 1]. 1.0 switches that "
                             f"direction's losses off.")
    # 2.1.1c the three remaining physical parameters that had no domain check.
    #
    # None of them would raise on a bad value - each one simply changes the physics and
    # lets the run report the result as though it meant it. A SoC fraction outside [0, 1]
    # starts every battery above its own capacity or below empty and makes 3.3.6b demand
    # the same at 24:00; a battery share outside (0, 1] prices degradation from a fraction
    # of the truck that does not exist, or from none of it; a peak limit at or below zero
    # forbids the depot to draw anything at all and makes every day infeasible with no
    # cause anywhere in the data. Checked here, where the three are read from, so the
    # message names the parameter instead of the constraint it eventually broke.
    if not (0.0 <= float(initial_soc_fraction) <= 1.0):
        raise ValueError(
            f"initial_soc_fraction is {initial_soc_fraction}; it is the share of its own "
            f"capacity every bev starts and ends the day at (3.3.6b) and has to lie in "
            f"[0, 1].")
    if not (0.0 < float(battery_price_share) <= 1.0):
        raise ValueError(
            f"battery_price_share is {battery_price_share}; it is the share of the "
            f"acquisition price attributed to the battery and amortized over the warranted "
            f"cycles, so it has to lie in (0, 1]. At 0 degradation is free and V2G looks "
            f"like free money.")
    if float(site_peak_limit_kW) <= 0.0:
        raise ValueError(
            f"site_peak_limit_kW is {site_peak_limit_kW}; it is the hard cap on the depot's "
            f"grid import (3.3.15) and has to be positive. At or below zero the site may "
            f"not draw its own baseline load and every day is infeasible.")
    # The roster is read once and used verbatim - there is no fleet size / mix sweep.
    # With auto_sizing off the run dispatches the fleet that exists, so it reads the
    # 'existing_fleet' sheet by name (1.2c); with auto_sizing on the roster is a pool the
    # optimizer draws from and the workbook is read as it was before, first sheet first, so
    # that turning sizing on cannot fail on a sheet name.
    fleet_dataset                       = load_fleet_dataset(
        fleet_input_file,
        sheet=None if auto_sizing == 'on' else FLEET_SHEET_EXISTING)
    # ... and so is the charging infrastructure: one station per row of the 'charging'
    # sheet, each with its own kW. Nothing here decides how many stations there are.
    charging_station_ids, charging_infrastructure = load_depot_charging_stations(depot_charging_station_file)
    # derived inputs come from data/; they are regenerated from the Excel datasets in inputs/
    require_input(TRIPS_CSV, 'src/hdv_trip_generation.py')
    all_trips                           = pd.read_csv(TRIPS_CSV, encoding=CSV_ENCODING)
    all_trips                           = all_trips[(all_trips['day_ID'] >= order_data_days[0]) & (all_trips['day_ID'] <= order_data_days[1])]
    if all_trips.empty:
        raise ValueError(f"No trips in day range {order_data_days} of {TRIPS_CSV.name}; adjust order_data_days.")
    all_trips['day_ID']                 = all_trips['day_ID'] - order_data_days[0] + 1
    trips_dataset_amount                = order_data_days[1] - order_data_days[0] + 1
    if isinstance(date_disposition, str):
        date_disposition                = datetime.strptime(date_disposition, '%d.%m.%Y')

    # 2.1.1d which calendar date each day of the range falls on.
    #
    # The depot's own load curve and its PV curve both belong to a date (2.1.5), and the
    # module derives them once, for date_disposition. A design run already gives every day
    # its own (day_curves_for, 2.9) - and the single-day path did not, so a sweep over
    # order_data_days = [2, 96] planned all ninety-five days against the sunshine and the
    # site load of one arbitrary day. Nothing failed and nothing said so: the curves are
    # module globals, they were the right shape, and only the date was wrong.
    #
    # trips.csv carries trip_date per row, which is where the interface already takes its
    # day labels from, so the mapping is read rather than assumed - no "day 1 is the
    # disposition date and the rest follow" convention that a gap in the order data would
    # quietly break.
    day_dates = {}
    if 'trip_date' in all_trips.columns:
        dated = pd.to_datetime(all_trips['trip_date'], errors='coerce')
        for day_id, when in zip(all_trips['day_ID'], dated):
            if pd.isna(when):
                continue
            day_dates.setdefault(int(day_id), when.to_pydatetime())

    # 2.1.2 import cost parameters
    require_input(COST_PARAMETER_ENERGY_CSV, 'src/hdv_cost_parameter_generation.py')
    require_input(COST_PARAMETER_DAILY_CSV, 'src/hdv_cost_parameter_generation.py')
    cost_parameters_energy              = pd.read_csv(COST_PARAMETER_ENERGY_CSV, encoding=CSV_ENCODING)
    cost_parameters_v2g                 = pd.read_csv(COST_PARAMETER_DAILY_CSV, encoding=CSV_ENCODING)
    if not set(scenario_year).issubset(set(cost_parameters_energy['Year'])):
        raise ValueError(f"scenario_year {scenario_year} is outside the years covered by "
                         f"{COST_PARAMETER_ENERGY_CSV.name} "
                         f"({int(cost_parameters_energy['Year'].min())}-{int(cost_parameters_energy['Year'].max())}).")

    # 2.1.3 define multiprocessing status, parameters, and gurobi thread usage
    total_iterations = len(scenario_year) * len(scenario) * len(v2g_status) * trips_dataset_amount
    cpu_count = multiprocessing.cpu_count()
    max_parallel_workers = max(1, min(parallel_worker_limit, cpu_count))
    if total_iterations > 4:
        multiprocessing_status = 'on'
        # Hand the cores the workers are not using to Gurobi instead of leaving them
        # idle: 4 workers x 4 threads saturates a 16-core box just as 16 workers would,
        # but the number of concurrent Gurobi sessions - and thus licence and NAS
        # pressure - stays at max_parallel_workers.
        gurobi_threads = max(1, cpu_count // max_parallel_workers)
    else:
        multiprocessing_status = 'off'
        gurobi_threads = cpu_count

    # 2.1.4 intraday price curve of each V2G channel, hour by hour
    # One row per hour, as produced by hdv_cost_parameter_generation.py from sheet
    # 'energy_daily'. Hourly rather than in six 4h blocks as before: arbitrage earns the
    # intraday spread, and averaging the curve into blocks flattens the very differences
    # it trades on.
    # These are absolute prices. A run on the 'daily' basis (1.4b2) uses them as they
    # stand; a run on the 'yearly' basis keeps their distribution and levels it onto the
    # scenario year and band. Either way the intraday shape comes from here and nowhere
    # else - which basis applies is decided per solve in 2.5, not here.
    #
    # The 'min' and 'max' columns hold the same series: 'energy_daily' states one curve
    # per price and carries no scenario band, and the generator writes it under all three
    # level names so that this file and the CSV keep one structure. Both are loaded all
    # the same - a workbook that does band the daily sheet still works, and nothing here
    # has to know which kind it was.
    if sorted(cost_parameters_v2g['Hour'].astype(int)) != list(range(1, 25)):
        raise ValueError(f"{COST_PARAMETER_DAILY_CSV.name} must hold the hours 1...24, once each.")
    v2g_by_hour = cost_parameters_v2g.sort_values('Hour').reset_index(drop=True)
    v2g_channel_curves = {}
    for channel in ('arbitrage', 'flexibility'):
        for level in ('min', 'max'):
            column = f'{level}_{channel}_price_€/MWh'
            if column not in v2g_by_hour.columns:
                raise ValueError(f"{COST_PARAMETER_DAILY_CSV.name} lacks the column '{column}'.")
            v2g_channel_curves[channel, level] = [float(v) for v in v2g_by_hour[column]]

    # 2.1.4b the day's two prices that are not curves: the public charger and the diesel
    #        pump. The model bills each as a single number, so each is averaged back to
    #        one here - the sheet states them per hour because the sheet has one shape,
    #        not because either moves. Averaging rather than taking the first hour: if a
    #        workbook ever does vary one of them, the mean is the honest single number for
    #        a model that has no per-step diesel price, and the first hour is just the
    #        first hour.
    #
    #        Only a disposition run reads these (2.5). A sizing run and a sweep price both
    #        off the scenario year of 'energy_yearly' instead - see 1.4b2.
    daily_flat_prices = {}
    for suffix in ('public_charging_cost_€/kWh', 'public_diesel_cost_€/l'):
        for level in ('min', 'max'):
            column = f'{level}_{suffix}'
            if column not in v2g_by_hour.columns:
                raise ValueError(
                    f"{COST_PARAMETER_DAILY_CSV.name} lacks the column '{column}'. It is "
                    f"written by src/hdv_cost_parameter_generation.py from sheet "
                    f"'energy_daily'; delete the CSV and let it rebuild if it predates "
                    f"that column.")
            values = [float(v) for v in v2g_by_hour[column]]
            daily_flat_prices[suffix, level] = sum(values) / len(values)

    # 2.1.5 intraday depot baseline load and PV generation for the disposition date
    #       both come from depot_dataset.xlsx: the load from sheet 'consumption', the PV
    #       plant from sheet 'generation'. The disposition date picks the day of year for
    #       both of them - the PVGIS curve, and now the metered load as well, matched on
    #       day and month with the year ignored. That is what makes this a winter or a
    #       summer day on both sides of the meter rather than only on the generation side.
    depot_baseline_load_kW = load_depot_intraday_profile(
        depot_load_profile_file, time_steps, for_date=date_disposition)
    pv_site_parameters = load_depot_pv_parameters(depot_pv_parameter_file)
    pv_peak_power_kW = pv_site_parameters['pv_peak_power_kW']
    depot_latitude = pv_site_parameters['pv_latitude_deg']
    depot_longitude = pv_site_parameters['pv_longitude_deg']
    pv_tilt_deg = pv_site_parameters['pv_tilt_deg']
    pv_azimuth_deg = pv_site_parameters['pv_azimuth_deg']
    pv_generation_kW = generate_pv_intraday_profile(time_steps, pv_peak_power_kW,
                                                    date_disposition, depot_latitude, depot_longitude,
                                                    pv_tilt_deg, pv_azimuth_deg)

    # 2.1.6 own PV generation that can end up in the trucks [kWh per step]
    #       The depot's own load is inelastic and sits behind the same meter, so it is
    #       served by the plant first; only what is left over can charge a truck, and
    #       that share is billed at spot minus the selling overhead instead of at spot
    #       plus the buying overhead - see 1.4.
    pv_charging_available_kWh = [max(0.0, pv_generation_kW[t] - depot_baseline_load_kW[t]) * STEP_HOURS
                                 for t in time_steps]

    # 2.1.7 built, and not before.
    #
    # This flag used to be set in 2.1.3, in the middle of the function, with the V2G
    # curves, the depot load and the PV profile still to come. Every one of those can
    # raise - a cost_parameter_hourly.csv that does not hold the hours 1...24, a disposition
    # date the depot's metering does not cover, a PVGIS round trip that keeps failing with
    # pv_allow_synthetic_profile off - and when one did, the module was left half
    # initialised and marked built. ensure_runtime_context() then declined to rebuild it,
    # so the next run read whatever had made it in and planned against the rest of a
    # previous build or against nothing at all.
    #
    # Set last, so it means what it says, and cleared at the top of this function, so a
    # failed rebuild leaves the module UNbuilt rather than stale. The next
    # ensure_runtime_context() will try again and fail in the same place, loudly, which is
    # the behaviour an unbuildable configuration should have.
    _runtime_context_built = True


_runtime_context_built = False


def ensure_runtime_context():
    """Build the runtime context once, the first time something actually needs it.

    It used to run at import. That made importing this module read four Excel files, the
    trip set, the price curves and the PVGIS cache before the caller had said what it
    wanted - and every caller that has an opinion (the interface, every benchmark) sets its
    parameters and calls build_runtime_context() again straight afterwards, so the work was
    done twice and the first time was with the wrong settings.

    Callers that configure the module keep calling build_runtime_context() explicitly; this
    only covers the one that does not, so `import` stays cheap and `run_optimization` still
    works without ceremony.
    """
    if not _runtime_context_built:
        build_runtime_context()


# 2 PREPROCESSING
# 2.1 subfunction for setup multicompute framework and parameters for iterations
def run_optimization(params, fleet_override=None, day_curves=None, build_only=False):
    """Plan one operating day: prepare it, build the MILP, solve it, report it.

    fleet_override  a roster to use instead of fleet_dataset - the candidate pool of a
                    design run (1.2d), which is not the fleet anyone owns.
    day_curves      {'depot_baseline_load_kW', 'pv_generation_kW', 'pv_charging_available_kWh'}
                    for this day's own date. build_runtime_context() builds those for one
                    date only, and a design run spans several, so it passes each day's in.
    build_only      return (build_arguments, context) after the preparation instead of
                    solving. This is the seam a design run composes its days through.
    """
    scenario_iterations, scenario_year_iterations, v2g_status_iteration, trips_dataset_iteration = params

    ensure_runtime_context()

    # a run that will write its own outputs opens its own generation (1.0b). Not when
    # build_only: that path returns before solving and is how a design run assembles its
    # days, which share the one stamp the design run opened for all of them.
    if not build_only:
        begin_output_run()

    # 2.2 load trip set
    trips = all_trips[all_trips['day_ID'] == trips_dataset_iteration]

    # 2.3 load fleet - the roster of inputs/fleet_dataset.xlsx, used verbatim
    fleet = (fleet_dataset if fleet_override is None else fleet_override).copy()

    # 2.3b the depot's own load and its PV belong to a date, and every day of a range gets
    #      the curves of its own date.
    #
    # A design run passes them in explicitly (day_curves, 2.9). The single-day path used to
    # take the module globals unconditionally - and those are built once, for
    # date_disposition - so a sweep over several days planned every one of them against one
    # arbitrary day's sunshine and site load. A run over order_data_days = [2, 96] costed
    # day 95 with the PV curve of 7 November and said nothing about it, because the curves
    # were the right shape and only the date was wrong. That is the quietest way this model
    # can be incorrect: it changes the demand charge, the grid peak, the PV share and the
    # whole energy bill of every day but the first, and no figure in the output looks odd.
    #
    # So the day's own date is looked up (day_dates, 2.1.1d) and its curves built. Only the
    # day that IS the disposition date keeps the module's own curves, which costs nothing
    # and keeps a plain one-day run byte-identical to what it was.
    day_date = date_disposition
    if day_curves is None:
        this_day = day_dates.get(int(trips_dataset_iteration))
        same_day_of_year = (this_day is not None and date_disposition is not None
                            and this_day.month == date_disposition.month
                            and this_day.day == date_disposition.day)
        if this_day is None or same_day_of_year:
            depot_baseline_load_kW_day = depot_baseline_load_kW
            pv_generation_kW_day = pv_generation_kW
            pv_charging_available_kWh_day = pv_charging_available_kWh
        else:
            day_date = this_day
            day_curves = day_curves_for(this_day)
            depot_baseline_load_kW_day = day_curves['depot_baseline_load_kW']
            pv_generation_kW_day = day_curves['pv_generation_kW']
            pv_charging_available_kWh_day = day_curves['pv_charging_available_kWh']
    else:
        depot_baseline_load_kW_day = day_curves['depot_baseline_load_kW']
        pv_generation_kW_day = day_curves['pv_generation_kW']
        pv_charging_available_kWh_day = day_curves['pv_charging_available_kWh']

    # 2.4 define vehicle and location sets
    vehicles = fleet['vehicle_id'].tolist()
    vehicle_types = fleet['vehicle_type'].tolist()
    bev_vehicles = fleet[fleet['vehicle_type'] == 'bev']['vehicle_id'].tolist()
    ice_vehicles = [m for m in vehicles if m not in bev_vehicles]
    day_trips_list = trips['trip_ID'].tolist()
    day_trips_distances = trips['trip_distance_km'].tolist()
    trip_distances = dict(zip(day_trips_list, day_trips_distances))
    is_bev = {m: 1 if m in bev_vehicles else 0 for m in vehicles}
    # v43: locations reduced: internal depot (parking/charging stations) now represented by explicit virtual CHG/V2G "trips"/events (no x_m_t_l needed).
    # Only external_charging (special loc with time penalty) remains as x assignment.
    locations = []
    if external_charging_status == 'on':
        locations.append('external_charging')

    # 2.5 define vehicle driving costs and the price curve of each V2G channel
    # best case = bev energy cheap + diesel expensive, worst case = the other way round.
    # diesel_band is named rather than reused from flexibility_band, which it happens to
    # equal: the two say different things and only one of them is about electricity.
    yearly = cost_parameters_energy.set_index('Year')
    if scenario_iterations == 'best case':
        price_band, flexibility_band, diesel_band = 'min', 'max', 'max'
        costs_diesel = yearly['max_public_diesel_cost_€/l']
        costs_energy_spot = yearly['min_energy_spot_price_€/kWh']
        costs_public = yearly['min_public_charging_cost_€/kWh']
    else:
        price_band, flexibility_band, diesel_band = 'max', 'min', 'min'
        costs_diesel = yearly['min_public_diesel_cost_€/l']
        costs_energy_spot = yearly['max_energy_spot_price_€/kWh']
        costs_public = yearly['max_public_charging_cost_€/kWh']

    # 2.5a which sheet this run is priced from (1.4b2). Decided here, before the first
    # price is read, because it decides ALL FOUR of them and not only the two curves.
    price_basis = resolve_energy_price_basis()

    # the public charger and the diesel pump, as single numbers. Neither is a curve - the
    # model bills one price per run for each - so the choice is only which sheet states it:
    #
    #   'daily'   the day's own, out of 'energy_daily' (averaged back to one number in
    #             2.1.4b). A disposition run plans one concrete day and is priced off that
    #             day entire, diesel included.
    #   'yearly'  the scenario year and band of 'energy_yearly', as a sizing run and a
    #             sweep need, since those move the year.
    #
    # The diesel price is flat either way. Which sheet it comes from is not a question
    # about its shape.
    if price_basis == 'daily':
        public_charging_cost_eur_per_kWh = daily_flat_prices[
            'public_charging_cost_€/kWh', price_band]
        diesel_cost_eur_per_l = daily_flat_prices['public_diesel_cost_€/l', diesel_band]
    else:
        public_charging_cost_eur_per_kWh = float(costs_public.loc[scenario_year_iterations])
        diesel_cost_eur_per_l = float(costs_diesel.loc[scenario_year_iterations])
    # the electricity and flexibility levels the yearly outlook puts on this scenario year.
    # On the 'yearly' basis these are what the daily curves are levelled onto; on the
    # 'daily' basis hourly_curve() below ignores both. Looked up either way, and cheaply:
    # the lookup also keeps the scenario year honest on every path, since a year the
    # outlook does not cover raises here rather than further down.
    energy_spot_yearly_eur_per_kWh = float(costs_energy_spot.loc[scenario_year_iterations])
    flexibility_yearly_eur_per_MWh = float(
        yearly.loc[scenario_year_iterations, f'{flexibility_band}_flexibility_price_€/MWh'])

    # 2.5b and the two that ARE curves: the shape from energy_daily either way, the level
    #      from whichever sheet the basis names (1.4b2).
    #
    # 'daily'  the 24 hourly prices of energy_daily as they are written - absolute prices
    #          of one operating day, shape and level together. A disposition run plans
    #          that day, so it is priced at that day's own numbers.
    # 'yearly' the same distribution, rescaled so its mean over the day is the scenario
    #          year's value in the band this run is made under. A sizing run and a sweep
    #          move year and band, so both set the level - and the shape has to come
    #          through it, because arbitrage earns the intraday spread and a flat curve
    #          would price the channel at zero for every year at once. A pure scaling
    #          keeps whatever each curve is anchored on intact without this having to
    #          know which.
    #
    # energy_daily carries no scenario band, so v2g_channel_curves holds the same series
    # under 'min' and 'max' (cost parameter generation 2.0/2.1). The band below therefore
    # does nothing on the 'daily' basis and everything on the 'yearly' one - see 1.4b2 for
    # what that means for a best case / worst case comparison made on a disposition run.
    def hourly_curve(channel, band, yearly_eur_per_MWh):
        """The 24 hourly prices this run buys and sells that channel at [€/MWh]."""
        base = v2g_channel_curves[channel, band]
        if price_basis == 'daily':
            return list(base)
        base_mean = sum(base) / len(base)
        if base_mean <= 0:
            raise ValueError(
                f"the {band} {channel} curve in {COST_PARAMETER_DAILY_CSV.name} averages "
                f"{base_mean:g} €/MWh over the day, so there is no distribution to "
                f"rescale onto {yearly_eur_per_MWh:g} €/MWh. Give that band non-zero "
                f"prices in sheet 'energy_daily', or run on the 'daily' basis (1.4b2), "
                f"which needs no distribution.")
        factor = yearly_eur_per_MWh / base_mean
        return [price * factor for price in base]

    # Arbitrage buys and sells the same commodity at the same meter, so it takes the same
    # band as the charging price - buying cheap in one band and selling dear in another
    # would be two different electricity markets. Its profit is the spread within the
    # curve, which is why the intraday shape has to survive both bases (2.5a).
    arbitrage_eur_per_MWh_hourly = hourly_curve(
        'arbitrage', price_band, energy_spot_yearly_eur_per_kWh * 1000.0)
    flexibility_eur_per_MWh_hourly = hourly_curve(
        'flexibility', flexibility_band, flexibility_yearly_eur_per_MWh)
    # 30-min steps, two per hour. These are still the *bare market* curves; the overheads
    # of 1.4 turn them into the prices the depot actually transacts at, below.
    spot_arbitrage_eur_per_MWh = [arbitrage_eur_per_MWh_hourly[t // STEPS_PER_HOUR]
                                  for t in time_steps]
    spot_flexibility_eur_per_MWh = [flexibility_eur_per_MWh_hourly[t // STEPS_PER_HOUR]
                                    for t in time_steps]
    # the electricity spot curve in €/kWh, which is what both overheads are measured from.
    # Taken before any of them is applied - it is a market price, not a price of this depot.
    energy_spot_eur_per_kWh_t = [p / 1000.0 for p in spot_arbitrage_eur_per_MWh]
    # the day's average spot price, read off the curve the run actually uses rather than
    # out of the yearly sheet. On the 'yearly' basis the rescaling makes the two the same
    # number by construction; on the 'daily' basis they need not be, and the reference
    # €/100km of 2.7a has to describe the run that was made rather than a year the run
    # never priced anything at.
    energy_spot_mean_eur_per_kWh = (sum(energy_spot_eur_per_kWh_t)
                                    / len(energy_spot_eur_per_kWh_t))

    # Every kWh the depot moves is priced by the same two rules (1.4), whichever direction
    # it goes and whichever channel it uses:
    #
    #   bought  ->  spot + grid_energy_overhead        (fees, levies, taxes, margin)
    #   sold    ->  spot - energy_selling_overhead     (marketing/platform fees, EEG)
    #
    # so buying, charging from own PV, discharging into arbitrage and discharging into
    # flexibility all settle on one consistent set of prices. Before this, V2G sold at the
    # bare spot curve while PV export was valued net of the selling overhead - the same
    # kWh leaving the same meter at two different prices depending on where it came from.
    depot_buy_eur_per_kWh_t = [price + grid_energy_overhead_eur_per_kWh
                               for price in energy_spot_eur_per_kWh_t]
    # charging from own PV costs the export revenue given up, which is the sell price
    pv_charging_eur_per_kWh_t = [price - energy_selling_overhead_eur_per_kWh
                                 for price in energy_spot_eur_per_kWh_t]
    # ... and a discharged kWh earns that same sell price. In €/MWh here, so the overhead
    # is scaled to match. Applied before the file overrides below, which therefore state a
    # realized price and are not deducted from twice.
    selling_overhead_eur_per_MWh = energy_selling_overhead_eur_per_kWh * 1000.0
    costs_v2g_arbitrage = [p - selling_overhead_eur_per_MWh for p in spot_arbitrage_eur_per_MWh]
    # The flexibility channel is deliberately NOT deducted from: it settles at the bare
    # flexibility_spot_price_€/kWh. energy_selling_overhead prices the marketing of an
    # *energy* sale, and flexibility is paid for a service rather than for energy, so that
    # deduction does not describe it. Its own marketing cost, if it has one, belongs in the
    # flexibility curve of costs_dataset.xlsx.
    costs_v2g_flexibility = list(spot_flexibility_eur_per_MWh)
    # Nothing is floored at zero. A spot price below the selling overhead means a kWh sold
    # into arbitrage earns less than it costs to place, which is real on a negative-price
    # hour - and the model simply declines those slots, since claiming one is optional.
    # The same curve makes own PV a credit rather than a cost, so it is worth seeing.
    if writes_outputs() and min(pv_charging_eur_per_kWh_t) < 0.0:
        print(f"note: selling is worth less than nothing in "
              f"{sum(1 for v in pv_charging_eur_per_kWh_t if v < 0)} step(s) - the spot "
              f"price falls below energy_selling_overhead_eur_per_kWh "
              f"({energy_selling_overhead_eur_per_kWh:.3f} €/kWh) there, so own PV is "
              f"credited rather than charged and V2G is not worth taking.")

    if v2g_status_iteration != 'on':
        costs_v2g_arbitrage = [0.0 for _ in time_steps]
        costs_v2g_flexibility = [0.0 for _ in time_steps]
    # optional overrides; without a file each channel keeps the curve derived above
    costs_v2g_arbitrage = load_v2g_price_curve(v2g_arbitrage_price_file, time_steps, costs_v2g_arbitrage)
    costs_v2g_flexibility = load_v2g_price_curve(v2g_flexibility_price_file, time_steps, costs_v2g_flexibility)
    # A kWh leaves the battery once, so it is sold into one channel, not both. 'both'
    # therefore takes whichever pays more in that step - not the sum, which used to let
    # the same discharge earn twice over. Both prices are known constants per step, so
    # the better one is settled here; leaving the choice to a binary per step would put
    # 48 more of them in front of the solver for an answer that cannot differ.
    #
    # The channel label has to come off the *same* decision. It used to be derived on its
    # own, as whichever of the two raw curves was higher in that step, which is only the
    # channel actually used when the mode is 'both'. Pin the mode to one channel and the
    # label went on naming the other one whenever the other one happened to pay more, so a
    # run made strictly on the arbitrage curve reported flexibility energy and flexibility
    # earnings - a split that described a sale that had not taken place. The totals were
    # never wrong, because they are summed from costs_v2g; only the attribution was. So it
    # is built here, from the branch taken, and cannot disagree with the price charged.
    if v2g_price_mode == 'arbitrage':
        costs_v2g = costs_v2g_arbitrage
        v2g_channel_at_step = ['arbitrage' for _ in time_steps]
    elif v2g_price_mode == 'flexibility':
        costs_v2g = costs_v2g_flexibility
        v2g_channel_at_step = ['flexibility' for _ in time_steps]
    else:
        costs_v2g = [max(a, f) for a, f in zip(costs_v2g_arbitrage, costs_v2g_flexibility)]
        v2g_channel_at_step = [
            'flexibility' if costs_v2g_flexibility[t] > costs_v2g_arbitrage[t] else 'arbitrage'
            for t in time_steps]
    arbitrage_blocked_steps = get_arbitrage_blocked_steps(costs_v2g_arbitrage, time_steps) if block_arbitrage_extreme_slots == 'on' and v2g_status_iteration == 'on' else set()

    # 2.6b geography: which trips touch the home depot, and which may be chained
    # The MILP has no map. This turns the trip locations into the handful of facts it
    # needs - does this trip start at home, does it end at home, may this trip follow
    # that one and what does the empty run between them cost - and hands them over as
    # constants. src/hdv_route_chaining.py does the geocoding and the clustering.
    #
    # Distances come back in km and hours; the model works in 30-min steps, so every leg
    # is rounded *up* to a whole step. A repositioning run never takes less time than it
    # takes, and rounding down would let a truck arrive before it could.
    day_routing = None
    approach_steps = {}
    return_steps = {}
    link_steps = {}
    approach_driving_steps = {}
    return_driving_steps = {}
    link_driving_steps = {}
    if route_chaining_status == 'on':
        import hdv_route_chaining as route_chaining
        from hdv_trip_generation import load_routing_cache, save_routing_cache

        routing_cache = load_routing_cache()
        tolerance_radius_km = route_chaining.tolerance_radius_km(
            all_trips['trip_distance_km'], location_tolerance_share)
        day_routing = route_chaining.build_day_routing(
            trips, home_depot_location, routing_cache,
            radius_km=tolerance_radius_km,
            nearest_links=route_nearest_link_candidates)
        save_routing_cache(routing_cache)

        def breaks_obliged_by(driving_steps):
            """Lenkzeitpausen a continuous stretch of that much driving obliges."""
            if fleet_operation_mode != 'crewed' or driving_steps <= 0:
                return 0
            return max(0, (driving_steps - 1) // driving_time_before_break_steps)

        def leg_steps(hours):
            """Steps a leg occupies, including any Lenkzeitpause it is long enough to need.

            An empty run is driving like any other, so a 5 h repositioning leg obliges the
            same 45-minute stop a 5 h loaded trip does (2.6b).
            """
            driving = duration_to_steps(hours)
            if driving == 0 or fleet_operation_mode != 'crewed':
                return driving
            return driving + breaks_obliged_by(driving) * driving_break_duration_steps

        # 2.6b-boundary the rest a route boundary obliges, which neither half obliges alone.
        #
        # An approach leg and the trip it leads into are rigidly contiguous: 3.3.16a anchors
        # the leg at st - approach_steps and 3.3.16g occupies every step between, so there
        # is no half hour anywhere in the sequence for the model to put a rest in. The same
        # holds for a trip and its return leg. Breaks were counted per item, so a 3 h
        # approach into a 3 h trip reached the 4.5 h threshold on neither side and carried
        # no rest at all - six hours of continuous driving that 3.3.17(c-ii) can only report
        # as a breach, because the timetable leaves it nowhere to place the fix.
        #
        # A chain is different and is deliberately left alone: 3.3.16c only requires the
        # successor to start after the connection has been driven, so a chained pair has
        # slack between its trips and the model can put a rest in it. That is the case
        # (c-ii) exists to enforce.
        #
        # So the rest the *sequence* obliges beyond what its two halves oblige separately is
        # added to the leg, where it becomes standing time exactly as a trip's own break is.
        # 3.3.17(c-ii) reads it back as the trailing steps of the leg's occupancy, so on an
        # approach it lands between the empty run and the job - "drive out, rest, run it".
        trip_drive_steps = {row['trip_ID']: duration_to_steps(row['trip_duration_h'])
                            for _, row in trips.iterrows()}

        def boundary_leg_steps(hours, trip_steps):
            driving = duration_to_steps(hours)
            if driving == 0 or fleet_operation_mode != 'crewed':
                return driving
            shortfall = max(0, breaks_obliged_by(driving + trip_steps)
                            - breaks_obliged_by(driving)
                            - breaks_obliged_by(trip_steps))
            breaks = breaks_obliged_by(driving) + shortfall
            return driving + breaks * driving_break_duration_steps

        def leg_driving_steps(hours):
            return duration_to_steps(hours)

        approach_steps = {f: boundary_leg_steps(hours, trip_drive_steps.get(f, 0))
                          for f, (_km, hours) in day_routing.approach.items()}
        return_steps = {f: boundary_leg_steps(hours, trip_drive_steps.get(f, 0))
                        for f, (_km, hours) in day_routing.ret.items()}
        link_steps = {pair: leg_steps(hours) for pair, (_km, hours) in day_routing.links.items()}
        # the driving inside those legs, which is what the Lenkzeit cap counts
        approach_driving_steps = {f: leg_driving_steps(hours)
                                  for f, (_km, hours) in day_routing.approach.items()}
        return_driving_steps = {f: leg_driving_steps(hours)
                                for f, (_km, hours) in day_routing.ret.items()}
        link_driving_steps = {pair: leg_driving_steps(hours)
                              for pair, (_km, hours) in day_routing.links.items()}

        if writes_outputs():
            print(f"home depot {home_depot_location}: {day_routing.summary()}")
            print(f"  same-place radius {tolerance_radius_km:.1f} km "
                  f"({location_tolerance_share:.0%} of the median trip distance)")
            merged = {rep: members for rep, members in day_routing.index.clusters().items()
                      if len(members) > 1}
            for rep, members in list(merged.items())[:6]:
                others = [m for m in members if m != rep]
                print(f"  '{rep}' also covers {others}")
            if len(merged) > 6:
                print(f"  ... and {len(merged) - 6} more merged cluster(s)")
            for note in day_routing.notes:
                print(f"  note: {note}")

    # 2.6c trips no driver could legally run, dropped before they poison the model
    #
    # A trip's cheapest possible shape is the one where nothing else interferes: drive out
    # from the depot, run it, drive home. If even *that* breaks the Lenkzeit or the
    # Arbeitszeit, no schedule can fix it - the trip is not a hard one to place, it is
    # undrivable from this depot in one day, and every route through it will break the law
    # whatever else the fleet does.
    #
    # Keeping such a trip has a cost beyond being wrong. Because the crew limits are
    # priced (3.3.17), one undrivable trip puts a fixed, unavoidable penalty into the
    # objective and the solver spends the whole run pushing on a wall that cannot move -
    # which is what made a day-2 run sit at a 78% gap after 25 minutes. Dropping them is
    # what lets the rest of the day be planned properly.
    #
    # They are never dropped silently: every one is named at the end of the run with the
    # figure that disqualified it, so the answer to "why is this order missing" is in the
    # output and not in the code.
    #
    # Applied whenever the fleet is crewed, with or without geography. It used to be gated
    # on `day_routing is not None` as well, which made a crewed run with
    # route_chaining_status = 'off' keep every trip however illegal - and since the crew
    # rules themselves were gated the same way (3.3.17), such a run planned with no driver
    # cost, no Lenkzeit, no Arbeitszeit and no filtering at all while still calling itself
    # crewed. Without chaining there are simply no approach and return legs to count, so
    # `absence` is the trip's own occupancy and `driving` its own driving; the test is
    # weaker, because it cannot see the empty running, but it is the same test and it is
    # the one the crew limits are enforced against in that mode.
    dropped_trips = []
    if fleet_operation_mode == 'crewed':
        shift_limit_steps = driver_max_shift_hours / STEP_HOURS
        drive_limit_steps = driver_max_driving_hours / STEP_HOURS
        for _, row in trips.iterrows():
            f = row['trip_ID']
            drive_steps = duration_to_steps(row['trip_duration_h'])
            breaks_inside = max(0, (drive_steps - 1) // driving_time_before_break_steps)
            occupied = drive_steps + breaks_inside * driving_break_duration_steps
            approach = approach_steps.get(f, 0)
            back = return_steps.get(f, 0)
            absence = approach + occupied + back
            driving = (approach_driving_steps.get(f, 0) + drive_steps
                       + return_driving_steps.get(f, 0))
            reasons = []
            if driving > drive_limit_steps:
                reasons.append(f"{driving * STEP_HOURS:g} h driving against a "
                               f"{driver_max_driving_hours:g} h Lenkzeit")
            if absence > shift_limit_steps:
                reasons.append(f"{absence * STEP_HOURS:g} h away against a "
                               f"{driver_max_shift_hours:g} h Arbeitszeit")
            if absence > len(time_steps):
                reasons.append(f"{absence * STEP_HOURS:g} h needed in a 24 h day")
            if reasons:
                dropped_trips.append({
                    'trip_ID': f,
                    'trip_distance_km': float(row['trip_distance_km']),
                    'trip_duration_h': float(row['trip_duration_h']),
                    'start': row['trip_start_location'],
                    'end': row['trip_end_location'],
                    'approach_h': approach * STEP_HOURS,
                    'return_h': back * STEP_HOURS,
                    'absence_h': absence * STEP_HOURS,
                    'driving_h': driving * STEP_HOURS,
                    'reason': ' and '.join(reasons)})

    if dropped_trips:
        removed = {d['trip_ID'] for d in dropped_trips}
        trips = trips[~trips['trip_ID'].isin(removed)].copy()
        if trips.empty:
            context = (f"once the run out from {home_depot_location!r} and back is counted"
                       if day_routing is not None
                       else "on their own driving and occupancy alone, before any empty "
                            "running (route_chaining_status is off, so there is none to count)")
            raise ValueError(
                f"all {len(removed)} trip(s) of this day need more driving or more time "
                f"away than the crew limits allow ({driver_max_driving_hours:g} h Lenkzeit, "
                f"{driver_max_shift_hours:g} h Arbeitszeit) {context}. Either the depot is "
                f"in the wrong place for this order set, or the limits are.")
        day_trips_list = [f for f in day_trips_list if f not in removed]
        trip_distances = {f: km for f, km in trip_distances.items() if f not in removed}
        # the geography has to forget them too, or the chain graph still offers routes
        # through trips that are no longer in the day. Only when there IS geography: the
        # filter above now runs in the chaining-free mode as well, where day_routing is
        # None and there is nothing to prune.
        if day_routing is not None:
            day_routing.starts_at_depot = {f: v for f, v in day_routing.starts_at_depot.items()
                                           if f not in removed}
            day_routing.ends_at_depot = {f: v for f, v in day_routing.ends_at_depot.items()
                                         if f not in removed}
            day_routing.approach = {f: v for f, v in day_routing.approach.items()
                                    if f not in removed}
            day_routing.ret = {f: v for f, v in day_routing.ret.items() if f not in removed}
            day_routing.links = {(a, b): v for (a, b), v in day_routing.links.items()
                                 if a not in removed and b not in removed}
        approach_steps = {f: v for f, v in approach_steps.items() if f not in removed}
        return_steps = {f: v for f, v in return_steps.items() if f not in removed}
        link_steps = {(a, b): v for (a, b), v in link_steps.items()
                      if a not in removed and b not in removed}
        approach_driving_steps = {f: v for f, v in approach_driving_steps.items()
                                  if f not in removed}
        return_driving_steps = {f: v for f, v in return_driving_steps.items()
                                if f not in removed}
        link_driving_steps = {(a, b): v for (a, b), v in link_driving_steps.items()
                              if a not in removed and b not in removed}
        if writes_outputs():
            print(f"  dropped {len(dropped_trips)} trip(s) no driver could run legally; "
                  f"{len(trips)} left")

    # 2.6 calculation of trips time-windows and durations
    #     The daily working hours narrow each trip's own window from the order data, but
    #     only where that still leaves the trip somewhere to run. A trip that cannot be
    #     fitted inside the working hours keeps its own window instead of making the day
    #     infeasible: the working hours are a preference for the trips they can hold, not
    #     a hard curfew that overrides the order data. Whichever trips fall back are
    #     counted and reported, so the exception is never silent.
    trips_duration_steps = {}
    trips_driving_steps = {}
    trips_distance_per_step = {}
    possible_start_times = {}
    trips_outside_work_hours = []
    trips_beyond_own_window = []
    for _, row in trips.iterrows():
        f = row['trip_ID']
        trip_window = (row['trip_window_start_time_hhmm'], row['trip_window_end_time_hhmm'])
        own_start_step = time_to_step(trip_window[0])
        own_end_step = time_to_step(trip_window[1])
        trip_duration_h = row['trip_duration_h']
        drive_steps = duration_to_steps(trip_duration_h)
        # 2.6b the Lenkzeitpause a long trip carries inside it.
        #
        # The router returns pure driving time, and a driver may not drive more than
        # driving_time_before_break_minutes without stopping for
        # driving_break_duration_minutes. A 6 h run is therefore a 6 h 45 min job, and
        # booking it as 6 h would let the fleet schedule driving no driver may legally do.
        # The break is added to the *occupancy* - the truck is standing at a rest area, so
        # it is unavailable - but not to the driving, which is what 3.3.17 caps at
        # driver_max_driving_hours.
        #
        # Counted per trip rather than across the day because this is the break the trip
        # cannot avoid; the breaks between trips are the model's to place.
        breaks_inside = (max(0, (drive_steps - 1) // driving_time_before_break_steps)
                         if fleet_operation_mode == 'crewed' else 0)
        dur_steps = drive_steps + breaks_inside * driving_break_duration_steps
        trips_duration_steps[f] = dur_steps
        trips_driving_steps[f] = drive_steps
        # the distance is spread over the whole occupancy, break included. Smearing it
        # slightly understates the power drawn while actually moving, but it keeps the
        # *total* exactly right, which is what the SoC balance and the energy bill read.
        # Charging it over the driving steps alone would bill dur_steps/drive_steps of the
        # trip's energy, because the consumption term runs over every step the trip covers.
        trips_distance_per_step[f] = row['trip_distance_km'] / dur_steps if dur_steps > 0 else 0

        # first choice: the trip's own window, narrowed to the working hours
        window_start_step = max(own_start_step, work_start_step)
        window_end_step = min(own_end_step, work_end_step)
        window_starts = list(range(window_start_step, window_end_step - dur_steps + 1))
        if not window_starts:
            # it does not fit in there - drop the working hours for this trip and let it
            # start or end outside them, anywhere its own order window allows
            window_starts = list(range(own_start_step, own_end_step - dur_steps + 1))
            if window_starts:
                trips_outside_work_hours.append((f, trip_duration_h, trip_window))
            else:
                # not the working hours' fault: the trip is longer than its own window
                trips_beyond_own_window.append((f, trip_duration_h, trip_window))
                continue
        possible_start_times[f] = [s for s in window_starts if s not in arbitrage_blocked_steps]
        if not possible_start_times[f]:
            possible_start_times[f] = window_starts

    # a trip that does not even fit its own order window is a data problem, and no choice
    # of working hours can fix it - fail with the offending trips named instead of letting
    # the solver report a bare "infeasible" that gives no hint where to look
    if trips_beyond_own_window:
        listing = '; '.join(
            f"trip {f} needs {duration:.2f} h within {window[0]}-{window[1]}"
            for f, duration, window in trips_beyond_own_window[:5]
        )
        more = (f" (and {len(trips_beyond_own_window) - 5} more)"
                if len(trips_beyond_own_window) > 5 else '')
        raise ValueError(
            f"{len(trips_beyond_own_window)} of {len(trips)} trip(s) are longer than their "
            f"own time window in the order data: {listing}{more}. Correct the window or the "
            f"duration in order_dataset.xlsx - the working hours are not the cause."
        )
    if writes_outputs() and fleet_operation_mode == 'crewed':
        with_break = {f: trips_duration_steps[f] - trips_driving_steps[f]
                      for f in trips_duration_steps
                      if trips_duration_steps[f] > trips_driving_steps.get(f, 0)}
        if with_break:
            added_h = sum(with_break.values()) * STEP_HOURS
            print(f"Lenkzeitpause: {len(with_break)} of {len(trips_duration_steps)} trip(s) "
                  f"drive longer than {driving_time_before_break_minutes / 60:g} h and carry "
                  f"a mandatory break, adding {added_h:g} h of standing time. The break is "
                  f"occupancy, not driving, so it does not count against the "
                  f"{driver_max_driving_hours:g} h Lenkzeit.")
    if trips_outside_work_hours and writes_outputs():
        named = ', '.join(str(f) for f, _duration, _window in trips_outside_work_hours[:8])
        more = (f" (and {len(trips_outside_work_hours) - 8} more)"
                if len(trips_outside_work_hours) > 8 else '')
        print(f"working hours {work_hours_start}-{work_hours_end}: "
              f"{len(trips_outside_work_hours)} of {len(trips)} trip(s) do not fit and keep "
              f"their own window from the order data - trip {named}{more}")

    # active_start_times is built once, AFTER the sampling below, and not here. It used to
    # be computed at this point as well and then thrown away and recomputed - |trips| x 48
    # list comprehensions for nothing. Worse than the waste is what it invited: two
    # bindings of one name, the first of them referring to start times that may no longer
    # have a z variable after the draw, and nothing to stop a later reader picking the
    # wrong one and building constraints over phantom starts.

    # v43: MonteCarlo sampling of candidate start times (runtime optimization via reduced binaries / constraint matrix size)
    if monte_carlo_samples_per_trip > 0:
        import random
        # own generator seeded from monte_carlo_seed, not the global random state: the draw
        # decides which start times exist as z variables at all, so an unseeded one makes the
        # model - and the cost it reports - different on every run of the same inputs.
        # Sorting the keys keeps the draw independent of dict insertion order too.
        rng = random.Random(monte_carlo_seed)
        for f in sorted(possible_start_times.keys(), key=str):
            cands = possible_start_times[f]
            if len(cands) > monte_carlo_samples_per_trip:
                possible_start_times[f] = sorted(rng.sample(cands, monte_carlo_samples_per_trip))

    # 2.6c the working hours bound the empty legs too, not only the loaded trip.
    #
    #      2.6 narrows each trip's own window to the working hours, but the loaded trip is
    #      only the middle of what the truck actually drives. It pulls out of the depot an
    #      approach leg *before* the trip starts and gets home a return leg *after* it
    #      ends, and both of those were bounded by the calendar day alone. A 06:30 pick-up
    #      five and a half hours out therefore had the truck leaving at 01:00 and still
    #      counted as a 06:00-18:00 day, which is not a day any depot runs: nobody is there
    #      to hand the keys over.
    #
    #      So the legs are held to the same hours, by the same rule and with the same
    #      fallback 2.6 uses - the working hours where the whole depot-to-depot excursion
    #      still fits inside them, the calendar day where it does not. The bound is decided
    #      here, per trip, and read in 3.3.16a, which is the only place a leg can be placed
    #      at all: route_start anchors the approach leg at st - approach_steps and
    #      route_end the return leg after the trip, so offering those two binaries only
    #      where the legs land inside the bound is what keeps them there. Everything
    #      downstream - the deadhead occupancy (3.3.16f-g), at_depot (3.3.16d) and the plot
    #      - reads the legs back off those binaries and follows without being told.
    #
    #      The test a trip has to pass to be held to the hours is that it can still be run
    #      as a route of its own inside them - one start time where the approach leg, the
    #      trip and the return leg all fit between work_start_step and work_end_step. That
    #      is exactly the guarantee the day bound used to give, since depot -> trip ->
    #      depot always fitted between 00:00 and 24:00, so no trip loses an option it had:
    #      it either keeps a whole in-hours excursion or it keeps the whole day. Chaining
    #      is deliberately not part of the test. Letting a chainable trip keep the hours on
    #      the strength of a partner would rest the trip's only way out on a chain the
    #      solver may not be able to use, and turn a preference into an infeasible day.
    #
    #      Computed after the Monte Carlo draw above, not before: the draw decides which
    #      start times still exist, and a trip judged to fit on one that is then sampled
    #      away would lose its last route boundary and fail 3.3.16a-check as "no way in".
    day_leg_bounds = (0, time_steps[-1] + 1)
    fell_back_in_2_6 = {f for f, _duration, _window in trips_outside_work_hours}
    leg_bounds = {}
    legs_outside_work_hours = []
    for f in sorted(possible_start_times, key=str):
        app = approach_steps.get(f, 0)
        ret = return_steps.get(f, 0)
        dur = trips_duration_steps[f]
        if any(st - app >= work_start_step and st + dur + ret <= work_end_step
               for st in possible_start_times[f]):
            leg_bounds[f] = (work_start_step, work_end_step)
        else:
            # the legs cannot be fitted inside the working hours: the trip keeps the whole
            # day for them, exactly as 2.6 lets a trip keep its own window. The working
            # hours are a preference here too, not a curfew that makes the day infeasible.
            leg_bounds[f] = day_leg_bounds
            # a trip 2.6 has already let out of the hours is not news here - its legs sit
            # outside them because the trip does, and it has been named once already
            if f not in fell_back_in_2_6:
                legs_outside_work_hours.append(f)
    if legs_outside_work_hours and writes_outputs() and route_chaining_status == 'on':
        named = ', '.join(str(f) for f in legs_outside_work_hours[:8])
        more = (f" (and {len(legs_outside_work_hours) - 8} more)"
                if len(legs_outside_work_hours) > 8 else '')
        print(f"working hours {work_hours_start}-{work_hours_end}: "
              f"{len(legs_outside_work_hours)} of {len(possible_start_times)} trip(s) fit "
              f"inside them but their empty legs do not, and keep the whole day for the "
              f"legs - trip {named}{more}")

    # 2.7c depot energy as explicit one-step events, which is how this model is built and
    #      not a mode it can be put into.
    #
    # Charging and V2G are 1-step "trips": CHG_t{t} for taking energy at a depot station,
    # V2G_t{t} for giving it back. They carry the same z_m_f_s assignment as a real trip,
    # the same occupancy rule (3.3.4), and the same SoC coupling - which is the whole point
    # of stating them this way rather than as a separate mechanism, because one set of
    # constraints then covers driving, charging and discharging alike.
    #
    # One charging event per step, not one per station. Which station a truck takes is no
    # longer a decision of the model: it plugs into whichever free station has the most
    # power (see 3.3.12), so the only thing to decide is whether it charges at all. That
    # turns 48 x stations x vehicles binaries into 48 x vehicles, and it is why the station
    # each truck stood at is reconstructed after the solve (5.1) rather than read off a
    # variable.
    #
    # These lists are always built. There is no 'off': every constraint below that touches
    # depot energy indexes CHG_t{t} directly, and the x[m,t,station] formulation this
    # replaced is gone (3.3.9, 3.3.13). v2g_virtual_trips stays empty when V2G is off for
    # the iteration, which is a real case and the only conditional here.
    v2g_virtual_trips = []
    chg_virtual_trips = []
    virtual_trip_durations = {}
    virtual_trip_possible_starts = {}
    for t in time_steps:
        ctg = f'CHG_t{t}'
        chg_virtual_trips.append(ctg)
        virtual_trip_durations[ctg] = 1
        virtual_trip_possible_starts[ctg] = [t]
    if v2g_status_iteration == 'on':
        for t in time_steps:
            vtg = f'V2G_t{t}'
            v2g_virtual_trips.append(vtg)
            virtual_trip_durations[vtg] = 1
            virtual_trip_possible_starts[vtg] = [t]
    virtual_trips = v2g_virtual_trips + chg_virtual_trips

    # v43: rebuild active_start_times AFTER possible MC sampling of possible_start_times (so active only references s that have z vars)
    active_start_times = {
        (f, t): [s for s in possible_start_times[f] if s <= t < s + trips_duration_steps[f]]
        for f in day_trips_list for t in time_steps
    }

    # extend active_start_times for 1-step virtual events (for any future uniform covering sums)
    for vt in virtual_trips:
        for tt in time_steps:
            active_start_times[(vt, tt)] = [s for s in virtual_trip_possible_starts.get(vt, []) if s <= tt < s + 1]

    # v43 unified event accessors (real driving + virtual v2g/chg)
    event_durations = dict(trips_duration_steps)
    event_durations.update(virtual_trip_durations)
    event_possible_starts = dict(possible_start_times)
    event_possible_starts.update(virtual_trip_possible_starts)

    # 2.7 set and calculate additional parameters
    vehicle_consumption = dict(zip(fleet['vehicle_id'], fleet['vehicle_consumption']))  # kWh/100km for bev, l/100km for ice

    # 2.7a per-vehicle driving energy cost [€/100km], from that vehicle's own consumption
    #      ice: €/l * l/100km, bev: €/kWh * kWh/100km. Only the ice entries reach the
    #      objective (see driving_cost in model_build) - bev energy is priced per kWh on
    #      the charging variables, again with the vehicle's own consumption. The bev entry
    #      is a reference figure at the average of the spot curve this run uses (2.5); what
    #      a bev is actually billed follows that curve and depends on when it charges.
    cost_vehicle_100km = {}
    for m in vehicles:
        if m in bev_vehicles:
            cost_vehicle_100km[m] = ((energy_spot_mean_eur_per_kWh
                                     + grid_energy_overhead_eur_per_kWh)
                                    * vehicle_consumption[m])
        else:
            cost_vehicle_100km[m] = diesel_cost_eur_per_l * vehicle_consumption[m]

    # no substitution here: normalize_fleet has already rejected a roster that leaves any
    # of these blank for a bev, so a NaN reaching this point would be a bug, not a default
    vehicle_charging_power = dict(zip(fleet['vehicle_id'], fleet['vehicle_charging_power']))  # kW
    vehicle_v2g_power = dict(zip(fleet['vehicle_id'], fleet['vehicle_charging_power']))  # kW, assumed same as charging (can be adjusted)
    vehicle_energy_storage = dict(zip(fleet['vehicle_id'], fleet['vehicle_energy_storage']))  # kWh
    vehicle_price = dict(zip(fleet['vehicle_id'], fleet['vehicle_price']))
    vehicle_battery_warranty = dict(zip(fleet['vehicle_id'], fleet['vehicle_battery_warranty']))  # warranted EFC
    # €/EFC differs per vehicle: a battery warranted for fewer cycles is more expensive
    # to cycle, so the optimizer prefers to age the better-warranted trucks
    degradation_cost_efc = {m: degradation_cost_per_efc(vehicle_price[m], vehicle_battery_warranty.get(m))
                            for m in bev_vehicles}

    # 2.7b truck toll rates (€/km) - fixed input rates differentiated by powertrain
    toll_rate_per_km = {}
    for m in vehicles:
        vtype = fleet.loc[fleet['vehicle_id'] == m, 'vehicle_type'].values[0]
        toll_rate_per_km[m] = bev_truck_toll_eur_per_km if vtype == 'bev' else diesel_truck_toll_eur_per_km

    # 2.8 call subfunctions
    # the arguments are identical for both builds, so the relaxed twin and the real model
    # have the same variables under the same names - which is what makes the warm start a
    # straight copy rather than a translation
    # a dict, not a tuple: model_build takes 54 parameters and this used to be 53
    # of them in order, matched to the signature by position alone. One transposed
    # pair of same-typed arguments - two step counts, two kWh series - would have
    # been silent, well-typed and wrong. Named, a mismatch is a TypeError at the
    # call instead of a schedule nobody can explain.
    build_arguments = {
        'vehicles': vehicles,
        'bev_vehicles': bev_vehicles,
        'ice_vehicles': ice_vehicles,
        'day_trips_list': day_trips_list,
        'locations': locations,
        'trips_duration_steps': trips_duration_steps,
        'trips_distance_per_step': trips_distance_per_step,
        'possible_start_times': possible_start_times,
        'active_start_times': active_start_times,
        'is_bev': is_bev,
        'vehicle_consumption': vehicle_consumption,
        'vehicle_energy_storage': vehicle_energy_storage,
        'vehicle_charging_power': vehicle_charging_power,
        'vehicle_v2g_power': vehicle_v2g_power,
        'costs_v2g': costs_v2g,
        'cost_vehicle_100km': cost_vehicle_100km,
        'v2g_status_iteration': v2g_status_iteration,
        'write_outputs': writes_outputs(),
        'auto_sizing': auto_sizing,
        'penalty_vehicle_use': penalty_vehicle_use,
        'initial_soc_fraction': initial_soc_fraction,
        'charging_infrastructure': charging_infrastructure,
        'trip_distances': trip_distances,
        'degradation_cost_efc': degradation_cost_efc,
        'depot_baseline_load_kW': depot_baseline_load_kW_day,
        'pv_generation_kW': pv_generation_kW_day,
        'depot_buy_eur_per_kWh_t': depot_buy_eur_per_kWh_t,
        'public_charging_cost_eur_per_kWh': public_charging_cost_eur_per_kWh,
        'driving_break_duration_steps': driving_break_duration_steps,
        'driving_time_before_break_steps': driving_time_before_break_steps,
        'event_durations': event_durations,
        'event_possible_starts': event_possible_starts,
        'virtual_trips': virtual_trips,
        'v2g_virtual_trips': v2g_virtual_trips,
        'chg_virtual_trips': chg_virtual_trips,
        'monte_carlo_samples_per_trip': monte_carlo_samples_per_trip,
        'advanced_degradation_status': advanced_degradation_status,
        'degradation_distribution_penalty': degradation_distribution_penalty,
        'soc_weight_factor': soc_weight_factor,
        'toll_rate_per_km': toll_rate_per_km,
        'pv_charging_available_kWh': pv_charging_available_kWh_day,
        'pv_charging_eur_per_kWh_t': pv_charging_eur_per_kWh_t,
        'day_routing': day_routing,
        'approach_steps': approach_steps,
        'return_steps': return_steps,
        'link_steps': link_steps,
        'trips_driving_steps': trips_driving_steps,
        'approach_driving_steps': approach_driving_steps,
        'return_driving_steps': return_driving_steps,
        'link_driving_steps': link_driving_steps,
        'leg_bounds': leg_bounds,
    }

    # 2.8-0 a design run (3.4) stops here. Everything above is this day's preparation -
    #       its trips, its routes, its prices, its depot curves - and a multi-day model
    #       needs exactly that for each of its days without solving any of them on its own.
    #       Returning the same tuple the single-day path is about to use is what keeps the
    #       two from drifting: there is one preparation, read twice.
    if build_only:
        return build_arguments, {
            'vehicles': vehicles, 'vehicle_types': vehicle_types,
            'bev_vehicles': bev_vehicles, 'ice_vehicles': ice_vehicles,
            'day_trips_list': day_trips_list, 'fleet': fleet, 'trips': trips,
            'locations': locations, 'trip_distances': trip_distances,
            'possible_start_times': possible_start_times,
            'trips_duration_steps': trips_duration_steps,
            'costs_v2g': costs_v2g, 'cost_vehicle_100km': cost_vehicle_100km,
            'depot_buy_eur_per_kWh_t': depot_buy_eur_per_kWh_t,
            'public_charging_cost_eur_per_kWh': public_charging_cost_eur_per_kWh,
            'event_durations': event_durations,
            'event_possible_starts': event_possible_starts,
            'virtual_trips': virtual_trips, 'v2g_virtual_trips': v2g_virtual_trips,
            'chg_virtual_trips': chg_virtual_trips,
            'toll_rate_per_km': toll_rate_per_km,
            'pv_charging_available_kWh': pv_charging_available_kWh_day,
            'pv_charging_eur_per_kWh_t': pv_charging_eur_per_kWh_t,
            'day_routing': day_routing, 'approach_steps': approach_steps,
            'return_steps': return_steps, 'link_steps': link_steps,
            'dropped_trips': dropped_trips,
            'trips_outside_work_hours': [f for f, _d, _w in trips_outside_work_hours],
            'v2g_channel_at_step': v2g_channel_at_step,
            'costs_v2g_arbitrage': costs_v2g_arbitrage,
            'costs_v2g_flexibility': costs_v2g_flexibility,
        }

    # 2.8a warm start: solve the same day once without the crew rules, then hand that
    # schedule to the constrained solve as a starting point. The relaxed answer already
    # places the trips and the routes; the second solve only has to repair it where the
    # crew rules bite, which is a much smaller search than building one from nothing.
    global _warm_start_gave_up
    warm_values = None
    warm_start_used = False
    if (fleet_operation_mode == 'crewed' and driver_warm_start == 'on'
            and not _warm_start_gave_up):
        warm_gap = max(optimization_MIPGap, driver_warm_start_MIPGap)
        relaxed_model, _relaxed_E, _relaxed_y, _relaxed_cost = model_build(
            **build_arguments, crew_rules=False)
        solve_model(relaxed_model, warm_gap, gurobi_threads,
                    time_limit_s=driver_warm_start_max_seconds)
        # Out of budget rather than out of work: the relaxed model is the easier one to
        # state and was not the easier one to solve, so on this fleet the premise behind
        # the whole pass does not hold.
        #
        # What it found is dropped rather than handed over, which is the part worth
        # justifying because passing it on would have been free. A schedule stopped at an
        # 80% gap by a model that was not told about the crew rules is not a head start: it
        # places trips the constrained model then has to move, and the constrained solve
        # measurably ran *slower* from it than from nothing - 48 s against 43 s here, 44 s
        # against 40 s on the run before. Small and twice, but both the same way, and in
        # the same direction as the reasoning. Having concluded the premise is wrong, the
        # honest thing is not to half-believe its answer.
        if relaxed_model.Status == gp.GRB.TIME_LIMIT:
            _warm_start_gave_up = True
            if writes_outputs():
                print(f"warm start: the relaxed solve did not reach {warm_gap:.0%} within "
                      f"its {driver_warm_start_max_seconds:g} s budget (it stopped at "
                      f"{relaxed_model.MIPGap:.0%}). On this fleet the crew rules are what "
                      f"makes the day tractable, so dropping them for a head start does not "
                      f"pay - this day is solved cold and the warm start is off for the "
                      f"rest of this run.")
        elif relaxed_model.SolCount > 0:
            warm_values = {v.VarName: v.X for v in relaxed_model.getVars()}
            if writes_outputs():
                print(f"warm start: relaxed solve found a schedule at "
                      f"{relaxed_model.MIPGap:.1%} gap in {relaxed_model.Runtime:.0f} s; "
                      f"handing it to the crew-constrained solve")
        elif writes_outputs():
            print("warm start: the relaxed solve found nothing to start from; "
                  "the constrained solve begins cold")
        relaxed_model.dispose()

    model, E_neg, y_m, _day_cost = model_build(**build_arguments)
    if warm_values:
        # a variable the relaxed model did not have (the crew slacks, the driving counters)
        # simply has no start value; Gurobi completes the rest itself
        for variable in model.getVars():
            value = warm_values.get(variable.VarName)
            if value is not None:
                variable.Start = value
        warm_start_used = True
    solve_model(model, optimization_MIPGap, gurobi_threads)

    # 2.8c one look back from the roster at the schedule that produced it
    #
    # Everything up to here optimised the vehicles and priced the crew by proxy: hours a
    # vehicle is away, plus a flat figure on the day's peak concurrency. The roster is
    # then fitted to the finished schedule under limits the MILP never saw - duty and
    # driving per *person* - and can need more drivers than the proxy was charged for. On
    # the shipped data that is not hypothetical: day 1 rosters four drivers against a peak
    # of two, so the schedule was chosen against a crew that cost half what it costs.
    #
    # This gives the model one chance to answer. If the roster needs heads the objective
    # did not pay for, the head figure is re-priced to what those heads actually cost and
    # the day is solved once more. Then both schedules are costed the *same* way - each
    # one's objective with its own driver proxy taken out and its own roster put in - and
    # the cheaper one is kept.
    #
    # Two things this is and is not. It is a heuristic: one iteration, no convergence
    # argument, and re-pricing the peak is a lever on the packing rather than a model of
    # it. It cannot make the answer worse, because the comparison is on the measure that
    # matters and the first schedule is one of the two candidates. What it is not is a
    # substitute for modelling drivers properly - that needs driver-indexed variables,
    # which multiply the model by the fleet size, and 3.5 already shows there is no room.
    #
    # Design runs skip it. They solve a day per candidate fleet and the pass would double
    # the whole lattice search for a refinement of the reported cost rather than of the
    # fleet choice.
    head_price_in_objective = penalty_driver_use
    if (fleet_operation_mode == 'crewed' and driver_roster_feedback == 'on'
            and model.SolCount > 0):
        model, E_neg, y_m, head_price_in_objective = driver_roster_feedback_pass(
            model, E_neg, y_m, build_arguments, vehicles, time_steps, day_trips_list,
            possible_start_times, trips_duration_steps, day_routing)

    # postprocess reads penalty_driver_use to work out what the objective charged for
    # driver heads (5.8b). If the feedback pass above kept a schedule solved against a
    # re-priced head charge, that - and not the configured one - is the number inside
    # this model's ObjVal, so it is the one the accounting has to use or the
    # reconciliation reports a residual that is really just the two prices disagreeing.
    #
    # The depot curves go the same way and for the same reason. postprocess reads
    # depot_baseline_load_kW and pv_generation_kW as module globals - they are not in its
    # signature - and 2.3b may have built this day its own pair. Without this the objective
    # would hold one day's sunshine and the report another's, which is exactly the seam
    # 3.5.7 already closes on the design path and the objective_residual of 5.8b catches.
    # date_disposition travels with them, so the 'disposition_date' column of the result
    # names the day whose curves were actually used rather than the one the module was
    # configured for - on a sweep those are the same only for one day of the range.
    with model_parameters(penalty_driver_use=head_price_in_objective,
                          date_disposition=day_date,
                          depot_baseline_load_kW=depot_baseline_load_kW_day,
                          pv_generation_kW=pv_generation_kW_day):
        results = postprocess(
            model=model,
            vehicles=vehicles,
            vehicle_types=vehicle_types,
            bev_vehicles=bev_vehicles,
            ice_vehicles=ice_vehicles,
            day_trips_list=day_trips_list,
            fleet=fleet,
            trips=trips,
            charging_infrastructure=charging_infrastructure,
            write_outputs=writes_outputs(),
            auto_sizing=auto_sizing,
            v2g_status_iteration=v2g_status_iteration,
            costs_v2g=costs_v2g,
            penalty_charging_use=penalty_charging_use,
            penalty_charging_block=penalty_charging_block,
            penalty_vehicle_use=penalty_vehicle_use,
            penalty_charging_external_time=penalty_charging_external_time,
            time_steps=time_steps,
            locations=locations,
            trip_distances=trip_distances,
            possible_start_times=possible_start_times,
            trips_duration_steps=trips_duration_steps,
            scenario_iterations=scenario_iterations,
            scenario_year_iterations=scenario_year_iterations,
            E_neg=E_neg,
            y_m=y_m,
            cost_vehicle_100km=cost_vehicle_100km,
            year_iteration=scenario_year_iterations,
            depot_buy_eur_per_kWh_t=depot_buy_eur_per_kWh_t,
            public_charging_cost_eur_per_kWh=public_charging_cost_eur_per_kWh,
            event_durations=event_durations,
            event_possible_starts=event_possible_starts,
            virtual_trips=virtual_trips,
            v2g_virtual_trips=v2g_virtual_trips,
            chg_virtual_trips=chg_virtual_trips,
            toll_rate_per_km=toll_rate_per_km,
            pv_charging_available_kWh=pv_charging_available_kWh_day,
            pv_charging_eur_per_kWh_t=pv_charging_eur_per_kWh_t,
            day_routing=day_routing,
            approach_steps=approach_steps,
            return_steps=return_steps,
            link_steps=link_steps,
            dropped_trips=dropped_trips,
            warm_start_used=warm_start_used,
            charging_station_ids=charging_station_ids,
            trips_outside_work_hours=[f for f, _duration, _window in trips_outside_work_hours],
            v2g_channel_at_step=v2g_channel_at_step,
            costs_v2g_arbitrage=costs_v2g_arbitrage,
            costs_v2g_flexibility=costs_v2g_flexibility,
        )

    return (results, vehicle_types)



# 2.9 the depot's own curves for one date
def day_curves_for(date_text):
    """Baseline load, PV generation and PV surplus for one calendar date.

    build_runtime_context() builds these once, for date_disposition. A design run spans
    several days and each has to be read against its own date - a January day sized under
    July sunshine is the one way this model can be quietly wrong - so each day asks for
    its own set here. PVGIS answers for every day in trips.csv are already on disk in
    cache_pv_profile.json, so a date asked for twice costs nothing the second time.
    """
    for_date = (datetime.strptime(date_text, '%d.%m.%Y')
                if isinstance(date_text, str) else date_text)
    baseline = load_depot_intraday_profile(depot_load_profile_file, time_steps,
                                           for_date=for_date)
    site = load_depot_pv_parameters(depot_pv_parameter_file)
    generation = generate_pv_intraday_profile(
        time_steps, site['pv_peak_power_kW'], for_date,
        site['pv_latitude_deg'], site['pv_longitude_deg'],
        site['pv_tilt_deg'], site['pv_azimuth_deg'])
    return {
        'depot_baseline_load_kW': baseline,
        'pv_generation_kW': generation,
        'pv_charging_available_kWh': [max(0.0, generation[t] - baseline[t]) * STEP_HOURS
                                      for t in time_steps],
    }


# 3 MODELSETUP
# 3.1 subfunction for building the MILP model
def model_build(vehicles, bev_vehicles, ice_vehicles, day_trips_list, locations, trips_duration_steps, trips_distance_per_step, possible_start_times, active_start_times, is_bev, vehicle_consumption, vehicle_energy_storage, vehicle_charging_power, vehicle_v2g_power, costs_v2g, cost_vehicle_100km, v2g_status_iteration, write_outputs, auto_sizing, penalty_vehicle_use, initial_soc_fraction, charging_infrastructure, trip_distances, degradation_cost_efc, depot_baseline_load_kW, pv_generation_kW, depot_buy_eur_per_kWh_t, public_charging_cost_eur_per_kWh, driving_break_duration_steps, driving_time_before_break_steps, event_durations, event_possible_starts, virtual_trips, v2g_virtual_trips, chg_virtual_trips, monte_carlo_samples_per_trip, advanced_degradation_status, degradation_distribution_penalty, soc_weight_factor, toll_rate_per_km, pv_charging_available_kWh, pv_charging_eur_per_kWh_t, day_routing=None, approach_steps=None, return_steps=None, link_steps=None, trips_driving_steps=None, approach_driving_steps=None, return_driving_steps=None, link_driving_steps=None, leg_bounds=None, crew_rules=None, penalty_charging_block=None, model=None, tag=''):
    """Build one operating day. Returns (model, E_neg, y_m, day_cost).

    model / tag are how a design run (3.4) puts several days into one MILP. Passing a model
    adds this day's variables and constraints to it instead of creating one, and tag is
    appended to every variable name so the days do not collide - postprocess reads the
    solution back through getVarByName and would otherwise find the wrong day's answer.
    With the defaults the function behaves exactly as it did before: its own model, untagged
    names, and the objective set here.

    day_cost is returned either way. A single-day run has it set as the objective below; a
    design run sums it over the days and adds the ownership term.
    """
    # 3.1.1 define mixed integer linear program and suppress license information output
    composing       = model is not None
    if not composing:
        # The only thing being silenced is the licence banner Gurobi prints when an
        # environment is created. It has to be done around the constructor itself, which
        # is also the call most likely to raise here - an expired or missing licence, a
        # token server that cannot be reached, a size-limited licence against a model this
        # big. Redirecting by hand made that failure far worse than it is: the restore
        # lines never ran, so stdout and stderr stayed pointed at the null device for the
        # rest of the process and the traceback explaining the licence problem went there
        # too. The run looked like it had died of nothing.
        #
        # contextlib.redirect_* restore on the way out however the block is left, and the
        # `with` on the devnull handles closes them instead of leaking a pair of file
        # descriptors per solve - which a sweep of a few hundred days would notice.
        with open(os.devnull, 'w') as devnull_out, open(os.devnull, 'w') as devnull_err:
            with contextlib.redirect_stdout(devnull_out), \
                 contextlib.redirect_stderr(devnull_err):
                model = gp.Model("fleet_disposition")

    # Grid charging prices per kWh, from costs_dataset.xlsx. They price grid electricity
    # only - the share of the depot charging that the site's own PV plant covers is priced
    # separately, see 3.3.15b.
    # Depot charging follows the intraday electricity curve, step by step: it is the same
    # price the arbitrage channel sells back into, so a kWh cannot be bought cheaper than
    # it is resold in the same half hour. External charging keeps one rate - the sheet
    # states it flat over the day.
    public_charging_eur_per_kwh = {m: public_charging_cost_eur_per_kWh for m in bev_vehicles}

    # 3.2 define optimization variables
    # 3.2.1 assignments and activity
    # v43: z now over real driving trips + explicit virtual V2G/CHG "trips" (events). Same logic for all: assignment z, occupation, SoC impact.
    all_events_for_z = day_trips_list + virtual_trips
    z_m_f_s         = model.addVars(
        [(m, f, s) for m in vehicles for f in all_events_for_z for s in event_possible_starts.get(f, possible_start_times.get(f, []))],
        vtype=gp.GRB.BINARY,
        name=f"z_m_f_s{tag}"
    )
    x_m_t_l         = model.addVars(vehicles, time_steps, locations, vtype=gp.GRB.BINARY, name=f"x_m_t_l{tag}")  # only external now (if enabled)
    # v43: removed x_m_t_l_bev (was proxy for "at depot for energy"); now use dedicated v2g_z / chg_z event indicators for gating E flows (unified with trip assignment)
    y_m             = model.addVars(vehicles, vtype=gp.GRB.BINARY, name=f"y_m{tag}")  # 1 if vehicle m is used on any trip
    x_m_t_E         = model.addVars(bev_vehicles, time_steps, lb=-gp.GRB.INFINITY, name=f"x_m_t_E{tag}") #bev-only: energy flow E (kWh per step, can be positive (charging) or negative (V2G)),
    x_m_SoC         = model.addVars(bev_vehicles, time_steps, lb=0, name=f"x_m_SoC{tag}") #bev-only: SoC (kWh)
    E_neg           = model.addVars(bev_vehicles, time_steps, lb=0, name=f"E_neg{tag}") #bev-only: E_neg (kWh) = max(-E, 0)
    E_pos           = model.addVars(bev_vehicles, time_steps, lb=0, name=f"E_pos{tag}") #bev-only: E_posg (kWh) = max(0, E)
    # split for public (external) vs private (depot/internal) charging energy costs from cost_parameters_energy
    E_private       = model.addVars(bev_vehicles, time_steps, lb=0, name=f"E_private{tag}")
    E_public        = model.addVars(bev_vehicles, time_steps, lb=0, name=f"E_public{tag}")

    # 3.3 define optimization constraints
    # 3.3.1 every (real driving) trip has exactly one assigned vehicle and start time. Virtual V2G/CHG "trips" are optional opportunities (no ==1).
    for f in day_trips_list:
        model.addLConstr(gp.quicksum(z_m_f_s[m, f, s] for m in vehicles for s in event_possible_starts.get(f, [])) == 1)

    # 3.3.4 v43: vehicle activity: driving OR virtual V2G/CHG event OR external loc (if any). 
    # <=1 (reform): allows pure idle/parking without forcing dummy location assignment. Virtual events use same z logic as real trips.
    # (old ==1 + full internal x_l removed; constraint matrix smaller + unified)
    for m in vehicles:
        for t in time_steps:
            covering_real = gp.quicksum(z_m_f_s[m, f, s] for f in day_trips_list for s in active_start_times.get((f, t), []))
            covering_virtual = gp.quicksum(
                z_m_f_s[m, f, t] for f in virtual_trips if t in event_possible_starts.get(f, [])
            )
            ext_x = (x_m_t_l[m, t, 'external_charging'] if 'external_charging' in locations else 0)
            model.addLConstr(covering_real + covering_virtual + ext_x <= 1)

    # 3.3.16 routes: where each truck is, and which trips may follow which
    #
    # Without this block the model has no geography at all: any trip may follow any other
    # and a truck may plug in whenever it is not driving, wherever it happens to be. This
    # turns the day into *routes* - a route leaves the home depot, runs one or more trips,
    # and comes back - and makes the depot chargers and V2G available only while the truck
    # is actually home.
    #
    # Three families of variable, all pruned to the candidates hdv_route_chaining found:
    #
    #   route_start[m,f,s]  m does f from s, and f is the first trip of a route, so the
    #                       truck drives the approach leg depot -> start(f) before it
    #   route_end[m,f,s]    ... and the last, so it drives the return leg back afterwards
    #   chain[m,f,g]        m does g directly after f, paying the empty run between them
    #                       (zero when g starts where f ends - the direct chain)
    #
    # Every assignment gets exactly one predecessor and one successor, so a route is a
    # path from the depot back to the depot and cannot be left open at either end.
    at_depot = {}
    route_start = {}
    route_end = {}
    chain = {}
    chaining_on = day_routing is not None
    # decided here rather than at 3.3.17, because 3.3.16d-off has to know as well: the
    # chaining-free model only builds an at_depot state when something is going to read it.
    # crew_rules exists for one caller - the warm start (2.8a), which builds this same model
    # without 3.3.17 so it can hand the result over as a starting point. Every other caller
    # gets the rules, because a schedule no driver may legally run is not a schedule.
    crew_rules_on = ((fleet_operation_mode == 'crewed') if crew_rules is None
                     else bool(crew_rules))
    if chaining_on:
        approach_steps = approach_steps or {}
        return_steps = return_steps or {}
        link_steps = link_steps or {}
        last_step = time_steps[-1]

        # 3.3.16a a route boundary is an assignment that also drives a leg. Only offered
        # where the leg fits inside the hours that trip is allowed to be driven in: the
        # approach leg has to fit before the trip and the return leg after it, so this is
        # the one place that decides where an empty leg may be placed at all. Where
        # neither is possible at any start time the trip has to be chained instead, which
        # the flow balance below then forces.
        #
        # The bound is per trip and comes from 2.6c: the working hours for a trip whose
        # whole depot-to-depot excursion fits inside them, the calendar day for one whose
        # does not. Without that the legs were bounded by the day alone and a truck could
        # leave at 01:00 for an 06:30 pick-up inside a 06:00-18:00 day. A missing entry
        # means the day, which is the behaviour of every caller that plans no geography.
        leg_bounds = leg_bounds or {}
        day_leg_bounds = (0, last_step + 1)
        for m in vehicles:
            for f in day_trips_list:
                app = approach_steps.get(f, 0)
                ret = return_steps.get(f, 0)
                earliest, latest = leg_bounds.get(f, day_leg_bounds)
                for st in possible_start_times.get(f, []):
                    if st - app >= earliest:
                        route_start[m, f, st] = model.addVar(
                            vtype=gp.GRB.BINARY, name=f"route_start{tag}[{m},{f},{st}]")
                        model.addLConstr(route_start[m, f, st] <= z_m_f_s[m, f, st])
                    if st + trips_duration_steps[f] + ret <= latest:
                        route_end[m, f, st] = model.addVar(
                            vtype=gp.GRB.BINARY, name=f"route_end{tag}[{m},{f},{st}]")
                        model.addLConstr(route_end[m, f, st] <= z_m_f_s[m, f, st])
                for (pf, pg) in link_steps:
                    if pf == f:
                        chain[m, f, pg] = model.addVar(
                            vtype=gp.GRB.BINARY, name=f"chain{tag}[{m},{f},{pg}]")

        # 3.3.16a-check every trip needs at least one way in and one way out, or the day
        # is infeasible and the solver can only say so as a bare "infeasible". The two ways
        # in are an approach leg that fits before some start time, or a chain from a trip
        # that ends where this one starts; likewise out. Named here, while it is still
        # possible to say which trip and why.
        unreachable = []
        for f in day_trips_list:
            has_in = (any((m, f, st) in route_start for m in vehicles
                          for st in possible_start_times.get(f, []))
                      or any(g == f for (_pf, g) in link_steps))
            has_out = (any((m, f, st) in route_end for m in vehicles
                           for st in possible_start_times.get(f, []))
                       or any(pf == f for (pf, _g) in link_steps))
            if not has_in or not has_out:
                unreachable.append((f, has_in, has_out))
        if unreachable:
            listing = '; '.join(
                f"trip {f} has no way "
                + (' and no way '.join(x for x in
                   ([] if has_in else ['in']) + ([] if has_out else ['out'])))
                for f, has_in, has_out in unreachable[:5])
            more = f" (and {len(unreachable) - 5} more)" if len(unreachable) > 5 else ''
            raise ValueError(
                f"{len(unreachable)} trip(s) cannot be fitted into any route: {listing}"
                f"{more}. A trip needs either a leg to or from the home depot that fits "
                f"inside its time window, or another trip to chain with. Widen the working "
                f"hours, raise monte_carlo_samples_per_trip so more start times survive the "
                f"draw, raise route_nearest_link_candidates, or check that "
                f"home_depot_location is the right place.")

        # 3.3.16b flow balance: every trip a vehicle runs is entered once and left once
        for m in vehicles:
            for f in day_trips_list:
                assigned = gp.quicksum(z_m_f_s[m, f, st]
                                       for st in possible_start_times.get(f, []))
                inbound = (gp.quicksum(v for (vm, vf, _s), v in route_start.items()
                                       if vm == m and vf == f)
                           + gp.quicksum(v for (vm, _vf, vg), v in chain.items()
                                         if vm == m and vg == f))
                outbound = (gp.quicksum(v for (vm, vf, _s), v in route_end.items()
                                        if vm == m and vf == f)
                            + gp.quicksum(v for (vm, vf, _vg), v in chain.items()
                                          if vm == m and vf == f))
                model.addLConstr(inbound == assigned)
                model.addLConstr(outbound == assigned)

        # 3.3.16c a chain only holds if the second trip starts after the first has
        # finished and the empty run between them has been driven.
        #
        # This is also what rules out closed loops. Flow balance alone is happy with a
        # cycle f -> g -> h -> f that never touches the depot: each trip has exactly one
        # predecessor and one successor, and no route ever starts or ends, so the vehicle
        # would show as standing at home all day while driving all three. Summing this
        # bound around such a cycle gives 0 >= sum of the durations and empty runs, which
        # is impossible for any real trip. No subtour-elimination constraints are needed:
        # the timetable does it, because time does not run in circles. Written on the assigned
        # start times rather than on a position index: sum(s*z) is the start time of
        # whichever assignment was chosen, and is 0 when the trip is not on this vehicle -
        # which the flow balance above already rules out whenever the chain is set, so the
        # bound is only ever applied to a real pair of start times.
        big_m_time = last_step + max(trips_duration_steps.values(), default=0) + 2
        for (m, f, g), link in chain.items():
            start_f = gp.quicksum(st * z_m_f_s[m, f, st]
                                  for st in possible_start_times.get(f, []))
            start_g = gp.quicksum(st * z_m_f_s[m, g, st]
                                  for st in possible_start_times.get(g, []))
            gap = trips_duration_steps[f] + link_steps.get((f, g), 0)
            model.addLConstr(start_g >= start_f + gap - big_m_time * (1 - link))

        # 3.3.16d where the truck is. at_depot[m,t] is 1 while the vehicle stands at the
        # home depot and 0 from the moment it pulls out until it is back - which includes
        # the empty legs and any waiting between two chained trips, because that waiting
        # happens at a customer yard and not at home.
        #
        # It is a balance, not a lookup: the vehicle leaves when a route starts and returns
        # when a route ends, and nothing else moves it.
        #
        #   at_depot[m,t] = at_depot[m,t-1] - departures at t + arrivals at t
        #
        # Continuous rather than binary on purpose: departures and arrivals are binaries
        # and a vehicle is only ever in one place, so the balance is integral wherever they
        # are. The 0..1 bounds do the real work - they forbid leaving twice without coming
        # back, and coming home while already home.
        departures = {(m, t): [] for m in vehicles for t in time_steps}
        arrivals = {(m, t): [] for m in vehicles for t in time_steps}
        for (m, f, st), var in route_start.items():
            departures[m, st - approach_steps.get(f, 0)].append(var)
        for (m, f, st), var in route_end.items():
            back = st + trips_duration_steps[f] + return_steps.get(f, 0)
            if back <= last_step:
                arrivals[m, back].append(var)
            # a route finishing exactly at 24:00 needs no arrival step inside the day;
            # 3.3.6b already requires the truck to be home and charged by then
        for m in vehicles:
            for t in time_steps:
                at_depot[m, t] = model.addVar(lb=0.0, ub=1.0, name=f"at_depot{tag}[{m},{t}]")
            for t in time_steps:
                previous = at_depot[m, t - 1] if t > 0 else 1.0
                model.addLConstr(
                    at_depot[m, t] == previous
                    - gp.quicksum(departures[m, t]) + gp.quicksum(arrivals[m, t]))

        # 3.3.16e the point of all of it: the depot chargers and the V2G connection are at
        # the depot. A truck standing at a customer yard can use neither - only the public
        # chargers behind 'external_charging' remain available to it, which is what a
        # driver would actually do.
        for m in vehicles:
            for t in time_steps:
                model.addLConstr(z_m_f_s[m, f"CHG_t{t}", t] <= at_depot[m, t])
                vtg = f"V2G_t{t}"
                if vtg in v2g_virtual_trips:
                    model.addLConstr(
                        gp.quicksum(z_m_f_s[m, vtg, st]
                                    for st in event_possible_starts.get(vtg, [t]))
                        <= at_depot[m, t])

    # 3.3.16d-off where the truck is when there is no geography.
    #
    # route_chaining_status = 'off' is the comparison mode: no map, no legs, every trip
    # assignable after any other and the depot chargers available in any free step. It has
    # no `at_depot`, and because the crew rules of 3.3.17 are written on that state, they
    # used to be skipped entirely in this mode - a run configured `crewed` planned with no
    # Lenkzeit, no Arbeitszeit, no driver hours in the objective and no head count, while
    # still reporting itself as crewed. A crewed-vs-autonomous comparison made that way is
    # comparing autonomy against autonomy.
    #
    # The state the mode CAN support is the one the roster already falls back to when it
    # reads such a schedule (driver_away_flags, 5.0): without geography the only thing
    # known about a truck's whereabouts is whether it is driving a loaded trip, so a truck
    # is away exactly while it is on one. That understates the roster - the repositioning
    # and the waiting this mode cannot see still needed somebody - and it is stated as an
    # understatement rather than left as a silence.
    #
    # Written as an equality on the covering sum rather than as a fresh binary: 3.3.4 holds
    # that sum at most 1, so `1 - covering` is already in [0, 1] and integral wherever the
    # z's are. The variable exists so that everything downstream - 3.3.17, the roster
    # reader, the schedule - reads one name in both modes.
    #
    # The depot chargers stay open all day here, deliberately: 3.3.16e is inside the
    # chaining branch above and is not mirrored, because "available in any free step" is
    # what this mode means.
    if not chaining_on and crew_rules_on:
        for m in vehicles:
            for t in time_steps:
                at_depot[m, t] = model.addVar(lb=0.0, ub=1.0,
                                              name=f"at_depot{tag}[{m},{t}]")
                covering_real = gp.quicksum(
                    z_m_f_s[m, f, s] for f in day_trips_list
                    for s in active_start_times.get((f, t), []))
                model.addLConstr(at_depot[m, t] == 1 - covering_real)

    # 3.3.5 only a bev plugs in - to charge and, equally, to feed back
    # The V2G half was missing. An ICE truck has no E_neg, so a V2G slot assigned to one
    # moves no energy; nothing constrained those binaries and only penalty_charging_use
    # held them at zero. That is a tuning value, not a law: at 0 the solver could scatter
    # V2G slots over the diesel fleet for free, and since 3.3.10 now reads assignments as
    # use, they would have been reported as used ICE trucks feeding the grid.
    for m in vehicles:
        for t in time_steps:
            model.addLConstr(z_m_f_s[m, f'CHG_t{t}', t] <= is_bev[m])
    for f in v2g_virtual_trips:
        for m in vehicles:
            for s in event_possible_starts.get(f, []):
                model.addLConstr(z_m_f_s[m, f, s] <= is_bev[m])

    # 3.3.16f the empty running, as trips in their own right
    #
    # An approach, a return and a paid chain are real driving: they take time, they burn
    # fuel or charge, they are tolled, and they age the battery. So each one is a leg with
    # its own place in the timetable rather than an adjustment folded into the trip it
    # belongs to - it occupies its own steps (3.3.16g), spends its energy in them, and is
    # drawn in the disposition plot as its own bar.
    #
    # A leg needs no z of its own: the indicator already exists and already fixes when the
    # leg runs, because a leg only happens as the consequence of an assignment.
    #
    #   route_start[m,f,s]   -> approach leg, the app_steps before s
    #   route_end[m,f,s]     -> return leg, the ret_steps after f ends
    #   chain_from[m,f,g,s]  -> the paid run from f to g, in between the two
    #
    # chain_from is the one new indicator: route_start and route_end are already tied to a
    # start time, but a chain is indexed by the pair alone and so cannot say *when* its
    # kilometres fall. Only paid chains get one - a direct chain is zero km and zero steps,
    # and needs no leg at all.
    chain_from = {}
    deadhead_legs = []          # (m, indicator, first_step, n_steps, km, kind, trip, partner)
    if chaining_on:
        paid_links = {(f, g): day_routing.links[(f, g)][0]
                      for (f, g) in day_routing.links if day_routing.links[(f, g)][0] > 0.0}
        for (m, f, g), link in chain.items():
            if (f, g) not in paid_links:
                continue
            for st in possible_start_times.get(f, []):
                var = model.addVar(vtype=gp.GRB.BINARY, name=f"chain_from{tag}[{m},{f},{g},{st}]")
                chain_from[m, f, g, st] = var
                model.addLConstr(var <= z_m_f_s[m, f, st])
            model.addLConstr(
                gp.quicksum(chain_from[m, f, g, st] for st in possible_start_times.get(f, []))
                == link)

        # every leg the day could contain, with the steps it would occupy. One list, read
        # by the occupancy constraint, the per-step energy, the fuel bill, the toll, the
        # degradation and the schedule alike, so none of them can disagree about how far a
        # truck drove or when.
        for (m, f, st), var in route_start.items():
            steps = approach_steps.get(f, 0)
            if steps > 0:
                deadhead_legs.append((m, var, st - steps, steps,
                                      day_routing.approach.get(f, (0.0, 0.0))[0],
                                      'approach', f, None))
        for (m, f, st), var in route_end.items():
            steps = return_steps.get(f, 0)
            if steps > 0:
                deadhead_legs.append((m, var, st + trips_duration_steps[f], steps,
                                      day_routing.ret.get(f, (0.0, 0.0))[0],
                                      'return', f, None))
        for (m, f, g, st), var in chain_from.items():
            steps = link_steps.get((f, g), 0)
            if steps > 0:
                deadhead_legs.append((m, var, st + trips_duration_steps[f], steps,
                                      paid_links[(f, g)], 'connection', f, g))

    # 3.3.16g a leg occupies the vehicle exactly as a trip does
    # 3.3.4 already says a vehicle does at most one thing per step, but it was written
    # before the legs existed and so does not see them - which left a hole a truck could
    # be dispatched into, taking a second trip during the very hour it was repositioning
    # for the first. This states the same rule over the fuller set of activities. It
    # implies 3.3.4 rather than replacing it; the weaker one is left in place so the
    # geography-free model (route_chaining_status = 'off') keeps its own constraint.
    deadhead_at_step = {(m, t): [] for m in vehicles for t in time_steps}
    for m, var, first_step, n_steps, km, _kind, _f, _g in deadhead_legs:
        for t in range(first_step, first_step + n_steps):
            # a leg that would run past midnight cannot be taken at all: 3.3.16a only
            # offers route boundaries whose leg fits, and 3.3.16c forces a chained
            # successor to start after the connection, which no start time outside the day
            # can satisfy. The guard is here so the accounting never silently drops part
            # of a leg's kilometres instead.
            if time_steps[0] <= t <= time_steps[-1]:
                deadhead_at_step[m, t].append((var, km / n_steps))
    if chaining_on:
        for m in vehicles:
            for t in time_steps:
                covering_real = gp.quicksum(
                    z_m_f_s[m, f, s] for f in day_trips_list
                    for s in active_start_times.get((f, t), []))
                covering_virtual = gp.quicksum(
                    z_m_f_s[m, f, t] for f in virtual_trips
                    if t in event_possible_starts.get(f, []))
                ext_x = (x_m_t_l[m, t, 'external_charging']
                         if 'external_charging' in locations else 0)
                covering_deadhead = gp.quicksum(var for var, _km in deadhead_at_step[m, t])
                model.addLConstr(
                    covering_real + covering_virtual + ext_x + covering_deadhead <= 1)

    def deadhead_km_in_step(m, t):
        """Empty kilometres vehicle m drives in step t, spread over that leg's own steps."""
        terms = [km_per_step * var for var, km_per_step in deadhead_at_step.get((m, t), [])]
        return gp.quicksum(terms) if terms else 0.0

    def deadhead_km_total(m):
        """Every empty kilometre vehicle m drives over the day."""
        terms = [km * var for vm, var, _fs, _ns, km, _k, _f, _g in deadhead_legs if vm == m]
        return gp.quicksum(terms) if terms else 0.0

    # 3.3.17 the crew rules
    #
    # A driver is tied to a vehicle exactly while that vehicle is away from the home depot:
    # a truck on a charger needs nobody, a truck on the road or standing in a customer yard
    # needs somebody who cannot leave it until it is home. So `away` is the driver's clock,
    # and three things follow.
    #
    #   (a) Arbeitszeit - one absence is one driver's working day, so it may not exceed
    #       driver_max_shift_hours. Stated as "in any window that long plus one step, the
    #       vehicle is home at least once", which bounds every continuous absence at once
    #       without needing to know where the absences are. HARD by default
    #       (driver_shift_limit, 1.4c).
    #   (b) Lenkzeit - the driving inside one absence may not exceed
    #       driver_max_driving_hours. A running counter does this exactly: it accumulates
    #       driving and is forced to zero whenever the vehicle is home.
    #   (c) Lenkzeitpause - the driving since the last 45-minute stop may not exceed
    #       driving_time_before_break_minutes. A second counter, reset by a rest the model
    #       places rather than by the depot. See (c) below for what this replaces.
    #
    # And the reason waiting in a customer yard costs something: it is inside `away`, and
    # (a) is a hard bound on how long `away` may run. An hour spent standing in a yard is an
    # hour of the only shift a driver has, so the truck has to come home to get another one.
    # That used to be a wage in the objective; it is a constraint now (1.4c), and the
    # constraint is the part that was doing the work.
    away = {}
    drivers_needed = None
    # rest steps the model placed, read by the external-charging penalty in 3.4: charging
    # at a public station costs the driver time unless the truck is standing still for a
    # statutory rest anyway. Empty unless the crew rules are built, which is correct - an
    # autonomous fleet takes no breaks and so earns no waiver.
    break_cover_model = {}
    # Gated on the crew rules alone (crew_rules_on is decided at 3.3.16). It used to
    # require `chaining_on` as well, because the
    # whole block is written on at_depot and only the routing model built one - so a run
    # configured `crewed` with route_chaining_status = 'off' produced a schedule with no
    # driver cost in the objective, no Arbeitszeit window, no Lenkzeit cap and no head
    # count, and reported it as a crewed day. 3.3.16d-off now supplies at_depot in that
    # mode too (understating absence, and saying so), so the rules apply in both.
    if crew_rules_on and at_depot:
        trips_driving_steps = trips_driving_steps or {}
        approach_driving_steps = approach_driving_steps or {}
        return_driving_steps = return_driving_steps or {}
        link_driving_steps = link_driving_steps or {}

        # Floored onto the step grid, not rounded. The shift span is derived as working
        # time plus the mandatory break (2.1.1b), so with the defaults it is 9.75 h - half
        # a step off the grid, and the direction that half step is resolved in decides
        # whether the model is stricter or looser than the law. Rounding gave 20 steps,
        # a 10 h absence against a 9.75 h limit, and left the MILP more permissive than
        # the roster, which floors the same figure to 19 (hdv_driver_scheduling) - so the
        # optimisation accepted absences the roster could then not give to anybody. Both
        # floor now, which also leans the way every other limit in this file leans: never
        # allow more than the rule does.
        max_shift_steps = max(1, int(math.floor(driver_max_shift_hours / STEP_HOURS + 1e-9)))
        max_drive_steps = max(1, int(math.floor(driver_max_driving_hours / STEP_HOURS + 1e-9)))
        horizon = len(time_steps)

        for m in vehicles:
            for t in time_steps:
                away[m, t] = 1.0 - at_depot[m, t]

        # The DRIVING limits are enforced with a slack that costs penalty_crew_rule_breach
        # per half-hour of excess. That is not softness for its own sake: on real order data
        # some trips *cannot* be crewed legally from one depot at all - day 2 has five
        # whose approach alone is 6.5 h, so depot -> trip -> depot is 13 h of absence and
        # 11 h of driving before anything else is scheduled. A hard constraint turns those
        # into a bare "infeasible" that names nothing; a priced one keeps the rest of the
        # day legal, forces the breach to be as small as possible, and reports exactly
        # where the law had to give. Set the penalty high enough and it behaves as a hard
        # rule wherever a hard rule is satisfiable.
        #
        # The SHIFT limit is the exception and is hard by default (driver_shift_limit,
        # 1.4c). An absence the roster cannot staff is not a schedule with a penalty on it,
        # it is a schedule that cannot be run - and unlike the driving limits there is
        # always a legal alternative, because 2.6c has already removed every trip that does
        # not fit one absence on its own. Priced, the solver bought its way past this limit
        # and the run reported a duty block nobody could lawfully cover; hard, the block
        # cannot exist and driver_blocks_over_shift is 0 by construction.
        shift_excess = {}
        drive_excess = {}

        # (a) no absence longer than a working day.
        #
        # at_depot is continuous in [0, 1] but is an exact balance of the route-start and
        # route-end binaries (3.3.16d), so it is integral in any integer-feasible solution.
        # That is what makes the hard form safe to state on it: "at least one at-depot step
        # in every window of max_shift_steps + 1" really does mean the truck came home, and
        # not that twenty fractional halves added up to one.
        shift_limit_hard = str(driver_shift_limit) == 'hard'
        if max_shift_steps < horizon:
            window = max_shift_steps + 1
            for m in vehicles:
                for start in range(0, horizon - window + 1):
                    home_in_window = gp.quicksum(
                        at_depot[m, t] for t in range(start, start + window))
                    if shift_limit_hard:
                        model.addLConstr(home_in_window >= 1)
                        continue
                    slack = model.addVar(lb=0.0, name=f"shift_excess{tag}[{m},{start}]")
                    shift_excess[m, start] = slack
                    model.addLConstr(home_in_window + slack >= 1)

        # driving per step: a loaded trip counts only the steps it actually moves, not the
        # rest-area break 2.6b added to it, and a leg likewise
        driving_steps_at = {(m, t): [] for m in vehicles for t in time_steps}
        for m in vehicles:
            for f in day_trips_list:
                occupied = trips_duration_steps[f]
                moving = trips_driving_steps.get(f, occupied)
                share = moving / occupied if occupied > 0 else 0.0
                for t in time_steps:
                    for st in active_start_times.get((f, t), []):
                        driving_steps_at[m, t].append(share * z_m_f_s[m, f, st])
        for m, var, first_step, n_steps, _km, kind, f, g in deadhead_legs:
            moving = {'approach': approach_driving_steps.get(f, n_steps),
                      'return': return_driving_steps.get(f, n_steps),
                      'connection': link_driving_steps.get((f, g), n_steps)}[kind]
            share = moving / n_steps if n_steps > 0 else 0.0
            for t in range(first_step, first_step + n_steps):
                if time_steps[0] <= t <= time_steps[-1]:
                    driving_steps_at[m, t].append(share * var)

        # (c-i) the Lenkzeitpause, as a rest the model places.
        #
        # 2.6b gives every trip and every leg long enough to need one the 45 minutes it
        # cannot avoid, and that is all there was. It covers the break a single long run
        # obliges and nothing else: breaks were counted per trip and per leg independently,
        # so a chained route of 4.4 h + 4.4 h reached neither threshold and carried no
        # break anywhere, while 3.3.17(b) waved it through because 8.8 h is inside the 9 h
        # Lenkzeit. That is 8.8 hours of driving with no 45-minute stop in it - legal in
        # the model, illegal on the road, and precisely the regime the chaining model
        # creates. The file said "the breaks between trips are the model's to place" and
        # then never placed them.
        #
        # So they are placed. break_taken[m,s] starts a statutory rest at s covering
        # break_steps steps; during those steps the vehicle may not drive, which is the
        # whole cost of a break and the reason the constraint bites - an hour of the
        # truck's day is gone. It may still charge, at the depot or at a public station,
        # because the driver's rest and the truck's plug are independent things; that is
        # also what makes the penalty waiver of 3.4 mean something.
        #
        # Two quantities, deliberately separate:
        #
        #   break_cover_model   a rest the model placed. Excludes driving, waives the
        #                       external-charging time penalty.
        #   break_reset         anything that resets the since-last-stop counter: the
        #                       above, plus the trailing steps of a trip whose occupancy
        #                       already carries an internal break (2.6b).
        #
        # The second is why the two are not one variable. A trip's internal break sits
        # inside the trip's own occupancy, so it cannot be a step in which the truck is not
        # driving - but it is still a break, and it still resets the counter.
        break_steps = max(1, int(driving_break_duration_steps))
        max_drive_before_break_steps = max(1, int(driving_time_before_break_steps))
        break_cover_terms = {(m, t): [] for m in vehicles for t in time_steps}
        for m in vehicles:
            for start in time_steps:
                if start + break_steps - 1 > time_steps[-1]:
                    continue              # a rest that would run past midnight is not one
                started = model.addVar(vtype=gp.GRB.BINARY,
                                       name=f"break_taken{tag}[{m},{start}]")
                for t in range(start, start + break_steps):
                    break_cover_terms[m, t].append(started)

        # the trailing steps of a trip or leg that carries its own break (2.6b,
        # 2.6b-boundary). Which point inside it the driver actually stops at is not a
        # decision the model makes, so the convention is that it is the end of the
        # occupancy - the one placement that lets the counter carry a sensible value into
        # whatever follows.
        #
        # Legs matter here as much as trips do. An approach leg carrying the rest its own
        # sequence obliges is the mechanism 2.6b-boundary uses to make the rigid
        # leg-into-trip case satisfiable at all, so if the counter could not see that rest
        # the rule it exists to serve would be unsatisfiable by construction.
        internal_break_terms = {(m, t): [] for m in vehicles for t in time_steps}
        inside_broken_trip = {(m, t): [] for m in vehicles for t in time_steps}

        def _mark_broken(indicator, first_step, occupied, carried, only_vehicle):
            """An activity whose occupancy carries `carried` steps of statutory rest.

            The rest is taken as the trailing steps of the occupancy - a convention, since
            the model never says which half hours of a run are the moving ones. Every step
            of the occupancy is exempt from the cap, because the per-step driving inside it
            is the total smeared over the whole span and cannot be read as driving since
            the last stop; the trailing steps additionally reset the counter, so whatever
            follows starts from zero.
            """
            for t in range(first_step, first_step + occupied):
                if not (time_steps[0] <= t <= time_steps[-1]):
                    continue
                inside_broken_trip[only_vehicle, t].append(indicator)
                if t >= first_step + occupied - carried:
                    internal_break_terms[only_vehicle, t].append(indicator)

        for f in day_trips_list:
            occupied = trips_duration_steps[f]
            carried = occupied - trips_driving_steps.get(f, occupied)
            if carried <= 0:
                continue
            for s in possible_start_times.get(f, []):
                for m in vehicles:
                    _mark_broken(z_m_f_s[m, f, s], s, occupied, carried, only_vehicle=m)
        for m, var, first_step, n_steps, _km, kind, f, g in deadhead_legs:
            moving = {'approach': approach_driving_steps.get(f, n_steps),
                      'return': return_driving_steps.get(f, n_steps),
                      'connection': link_driving_steps.get((f, g), n_steps)}[kind]
            carried = n_steps - moving
            if carried > 0:
                _mark_broken(var, first_step, n_steps, carried, only_vehicle=m)

        for m in vehicles:
            for t in time_steps:
                cover = gp.quicksum(break_cover_terms[m, t])
                break_cover_model[m, t] = cover
                covering_real = gp.quicksum(
                    z_m_f_s[m, f, s] for f in day_trips_list
                    for s in active_start_times.get((f, t), []))
                covering_deadhead = gp.quicksum(
                    var for var, _km in deadhead_at_step.get((m, t), []))
                # a resting truck is a standing truck. This also holds the cover at most 1,
                # which is what makes the big-M reset in (c-ii) well posed: two rests may
                # not overlap, so break_reset below never exceeds one.
                model.addLConstr(cover + covering_real + covering_deadhead <= 1)

        # ... and only as much rest as the driving entitles the driver to.
        #
        # A rest costs the schedule nothing except the driving it displaces, and 3.4b pays
        # for one: the external-charging time penalty is waived while a rest runs. Left
        # unbounded the two combine into free money - the solver declares a rest in every
        # step the truck is not driving and charges at a public station through all of it,
        # which on the chaining-free model (where nothing ties a public charger to being
        # away from the depot) took a measured day from 0 to 16 penalty-free steps.
        #
        # The law itself gives the bound: 45 minutes of rest per 4.5 hours of driving. In
        # steps that is break_steps per max_drive_before_break_steps, and stating it as a
        # ratio keeps it linear and never binds on a rest the counter in (c-ii) actually
        # demands - driving D steps obliges floor(D / threshold) rests and this permits
        # break_steps * D / threshold, which is always at least as many.
        for m in vehicles:
            driven_total = gp.quicksum(
                term for t in time_steps for term in driving_steps_at[m, t])
            model.addLConstr(
                max_drive_before_break_steps
                * gp.quicksum(break_cover_model[m, t] for t in time_steps)
                <= break_steps * driven_total)

        # (b) Lenkzeit inside one absence, as a counter that the depot resets.
        #
        # The counter is capped at the length of the day, which is the most driving any
        # step could possibly have accumulated, and big_m is set above that cap plus one
        # step. Both matter: the reset works by making the accumulate-forward bound
        # vacuous at the depot, and it is only vacuous if big_m really does exceed
        # everything the counter can hold. With big_m merely equal to the horizon, a
        # counter near its own bound made "counter >= previous + driven - big_m" demand a
        # positive value in the same step the depot forces it to zero - an infeasibility
        # with no cause anywhere in the data.
        big_m_steps = horizon + 2
        for m in vehicles:
            previous = 0.0
            for t in time_steps:
                driven_now = gp.quicksum(driving_steps_at[m, t])
                counter = model.addVar(lb=0.0, ub=horizon,
                                       name=f"drive_since_depot{tag}[{m},{t}]")
                over = model.addVar(lb=0.0, name=f"drive_excess{tag}[{m},{t}]")
                drive_excess[m, t] = over
                model.addLConstr(counter <= previous + driven_now)
                model.addLConstr(counter >= previous + driven_now
                                 - big_m_steps * at_depot[m, t])
                model.addLConstr(counter <= big_m_steps * (1.0 - at_depot[m, t]))
                model.addLConstr(counter <= max_drive_steps + over)
                previous = counter

        # (c-ii) Lenkzeit since the last 45-minute stop, as a second counter.
        #
        # Same shape as (b) and a different reset. (b) is bounded by the absence and resets
        # at the depot; this one is bounded by driving_time_before_break_minutes and resets
        # only on a rest - a model-placed one, or the internal break of a long trip. A
        # depot stop does NOT reset it, deliberately: half an hour in the yard is not a
        # Lenkzeitpause, and letting it count as one would reopen the hole this closes from
        # the other side. A truck that is at home and not driving can always claim a rest
        # for free, so nothing legal is forbidden by the stricter reading.
        #
        # One exemption, and it is an honest one. Inside a trip that carries its own break,
        # the per-step driving is the trip's total driving smeared over its whole occupancy
        # (driving_steps_at above, `share`), because the model never says which half hours
        # of a long run are the moving ones. Read as "driving since the last stop" that
        # smeared figure is meaningless - a 9 h trip would show 8 h accumulated by its own
        # midpoint and breach a 4.5 h cap it satisfies by construction. So the cap is
        # lifted for exactly the steps a break-carrying trip occupies, and the trip's
        # trailing break then resets the counter for whatever follows it. What remains
        # enforced is the driving accumulated ACROSS trips and legs, which is the gap that
        # was open.
        break_excess = {}
        for m in vehicles:
            previous = 0.0
            for t in time_steps:
                driven_now = gp.quicksum(driving_steps_at[m, t])
                reset = (break_cover_model[m, t]
                         + gp.quicksum(internal_break_terms[m, t]))
                counter = model.addVar(lb=0.0, ub=horizon,
                                       name=f"drive_since_break{tag}[{m},{t}]")
                over = model.addVar(lb=0.0, name=f"break_excess{tag}[{m},{t}]")
                break_excess[m, t] = over
                model.addLConstr(counter <= previous + driven_now)
                model.addLConstr(counter >= previous + driven_now - big_m_steps * reset)
                model.addLConstr(counter <= big_m_steps * (1.0 - reset))
                model.addLConstr(
                    counter <= max_drive_before_break_steps + over
                    + horizon * gp.quicksum(inside_broken_trip[m, t]))
                previous = counter

        # how many drivers the day needs at its busiest: every vehicle away at the same
        # moment is a driver of its own, and no roster can do better than that peak
        drivers_needed = model.addVar(lb=0.0, name=f"drivers_needed{tag}")
        for t in time_steps:
            model.addLConstr(drivers_needed >= gp.quicksum(away[m, t] for m in vehicles))

        # ... and how many the day needs by volume, which is a different question and
        # sometimes the larger answer.
        #
        # The peak above is the only bound the model used to have, and it is blind to how
        # long the day is: two vehicles away all day and two away for one hour each give
        # the same peak of two. What separates them is that a driver may only work
        # driver_max_working_hours and may only drive driver_max_driving_hours, so the
        # total duty and the total driving each divide into a minimum head count:
        #
        #     drivers >= total hours away    / driver_max_working_hours
        #     drivers >= total hours driving / driver_max_driving_hours
        #
        # Both are valid - no driver can absorb more than their own limit - and both are
        # one constraint over variables the model already has. driver_max_working_hours in
        # particular had no presence in the optimisation at all before this: it was applied
        # only when the roster was built afterwards, so the model could not see the limit
        # that decides how many people its schedule actually needs.
        #
        # This does not close the gap to the roster, and is not meant to. What the roster
        # runs into is indivisibility - a 9 h absence cannot be split between two people,
        # however much slack the totals leave - and a bound on totals cannot express that.
        # It is bin packing, and the honest bound on a bin-packing optimum is the volume.
        # The remaining difference is reported rather than hidden (5.5c).
        if driver_max_working_hours and driver_max_working_hours > 0:
            model.addLConstr(
                drivers_needed * driver_max_working_hours
                >= gp.quicksum(away[m, t] for m in vehicles for t in time_steps) * STEP_HOURS)
        if driver_max_driving_hours and driver_max_driving_hours > 0:
            all_driving = gp.quicksum(
                term for m in vehicles for t in time_steps
                for term in driving_steps_at[m, t])
            model.addLConstr(
                drivers_needed * driver_max_driving_hours
                >= all_driving * STEP_HOURS)

        # the excess is measured per half-hour step, so the penalty is per half-hour of
        # illegal driving, of driving pushed past the point a 45-minute stop was due, and -
        # only under driver_shift_limit = 'priced' - of a shift run long. They are priced
        # the same, because each is the law giving way where no legal schedule exists, and
        # the run reports every half hour of it rather than absorbing it. shift_excess is
        # empty under the default hard shift limit, so that term is simply zero there.
        crew_breach_steps = (gp.quicksum(shift_excess.values())
                             + gp.quicksum(drive_excess.values())
                             + gp.quicksum(break_excess.values()))

    # 3.3.6 SoC dynamics for bev only (parameterized initial SoC)
    # v43: drive consumption only from real trips (as before). V2G discharge (to grid) and CHG (from grid) affect via x_m_t_E (E_neg/E_pos).
    # Virtual V2G "trips" (discharge events) and CHG "trips" (neg-discharge=charge) use z to gate/activate the E flows (same assignment as real trips).
    for m in bev_vehicles:
        # Step 0 acts on the day-start level exactly like every other step: the SoC it
        # leaves behind is the start level plus what flowed in, minus what was driven.
        # Pinning x_m_SoC[.,0] to the start level instead dropped step 0 out of the
        # balance, so x_m_t_E[.,0] was billed - or credited with V2G earnings - without
        # ever reaching the battery, while the two bounds in 3.3.11 clearly intend it to
        # charge and discharge like any other. The day still *starts* at
        # initial_soc_fraction; x_m_SoC[.,0] is the state after the first half hour.
        # x_m_t_E is battery-side throughout this balance - it is the charge the pack gains
        # or loses, already net of the conversion losses that 3.3.11c takes off the metered
        # flow. Nothing here has to know about them.
        consumption_0 = (gp.quicksum(
            (vehicle_consumption[m] / 100.0) * trips_distance_per_step[f] * z_m_f_s[m, f, s]
            for f in day_trips_list for s in active_start_times.get((f, 0), []))
            + (vehicle_consumption[m] / 100.0) * deadhead_km_in_step(m, 0))
        model.addLConstr(x_m_SoC[m, 0] == initial_soc_fraction * vehicle_energy_storage[m]
                                          + x_m_t_E[m, 0] - consumption_0)
        model.addLConstr(x_m_SoC[m, 0] <= vehicle_energy_storage[m])
        for t in time_steps[1:]:
            consumption_t = (gp.quicksum(
                (vehicle_consumption[m] / 100.0) * trips_distance_per_step[f] * z_m_f_s[m, f, s]
                for f in day_trips_list for s in active_start_times.get((f, t), []))
                + (vehicle_consumption[m] / 100.0) * deadhead_km_in_step(m, t))
            model.addLConstr(x_m_SoC[m, t] == x_m_SoC[m, t-1] + x_m_t_E[m, t] - consumption_t)
            model.addLConstr(x_m_SoC[m, t] <= vehicle_energy_storage[m])
            # no ">= 0" here: x_m_SoC is declared lb=0 (3.2.1), and a variable bound is
            # both stronger and cheaper than a row saying the same thing - presolve and
            # every bound-based cut generator read it directly. The row was also only ever
            # added for t >= 1, so step 0 has been running without it all along.

    # 3.3.6b day-boundary SoC: back at the starting level by 24:00
    # initial_soc_fraction is both ends of the day - the level every bev starts at 00:00
    # and the level it has to have reached again at 24:00. That makes the day repeatable:
    # the schedule cannot be paid for by running the batteries down overnight, so driving
    # and V2G discharge both have to be charged back before the day closes.
    # Stated as ">=" rather than "==": finishing above the target is operationally fine,
    # and since every kWh costs money the optimum sits on the target unless cheap own PV
    # or peak shaving makes it worth ending higher. "==" would forbid that for no reason.
    for m in bev_vehicles:
        model.addLConstr(x_m_SoC[m, time_steps[-1]] >= initial_soc_fraction * vehicle_energy_storage[m])

    # SoC implementation review (TODO.txt item 1): ... (v43: same + V2G/CHG now via explicit trip-like events gating E; drive cons separate as motion loss)

    # 3.3.8 SoC has to be sufficient for whole trip distance at start (using trip_distances dict)
    # Tested on the level the truck *departs* with, which is the one at the end of the
    # previous step: x_m_SoC[m, s] is already net of step s's own driving (3.3.6), so
    # testing it asked for the trip's energy plus its own first half hour on top - about
    # trip_energy / duration_steps too much, and double for a one-step trip. That reserve
    # kept trucks off trips they could finish. At s = 0 the level to depart with is the
    # day-start level, a constant, and the constraint then simply forbids the assignment
    # when the truck cannot start the day with enough charge.
    for m in bev_vehicles:
        day_start_level = initial_soc_fraction * vehicle_energy_storage[m]
        for f in day_trips_list:
            trip_energy = (vehicle_consumption[m] / 100.0) * trip_distances[f]
            for s in event_possible_starts.get(f, []):
                soc_at_departure = x_m_SoC[m, s - 1] if s >= 1 else day_start_level
                Mbig = trip_energy
                model.addLConstr(soc_at_departure >= trip_energy - Mbig * (1 - z_m_f_s[m, f, s]))

    # 3.3.9 v43 REMOVED: old V2G depot site logic + x_l_bev proxy (parking/chg loc x). 
    # Now V2G activated only when explicit V2G_t{t} "trip" assigned via z (unified trip logic); charging via CHG_ "trips".
    # Gating of E_neg / E_pos / x_E moved into the per-t power limits block below using v2g_z / chg_z.

    # 3.3.10 vehicle usage activation: a vehicle is used when it DRIVES a trip
    #
    # Over day_trips_list - the real trips - and not over all_events_for_z. Charging and
    # V2G are virtual "trips" in this formulation (v43), so quantifying over every event
    # made a truck that stood on a charger all day count as used; this counts only driving,
    # which is the work the fleet exists for.
    #
    # The consequence is deliberate and worth stating: y_m is the only thing carrying
    # penalty_vehicle_use, so a truck kept at the depot purely for V2G is now FREE in the
    # objective and invisible to the auto-sizing decision. Under auto_sizing the run may
    # therefore hold back trucks that earn on the spread without ever driving, and report a
    # fleet size that does not include them. postprocess (5.3) reports those vehicles and
    # their earnings separately so the effect is visible rather than inferred.
    for m in vehicles:
        for f in day_trips_list:
            for s in event_possible_starts.get(f, possible_start_times.get(f, [])):
                model.addLConstr(y_m[m] >= z_m_f_s[m, f, s])

    # 3.3.10b interchangeable vehicles are used lowest id first
    #
    # Two trucks the model cannot tell apart make every schedule into a family of
    # schedules: swap their labels and the objective does not move. Branch and bound has no
    # way to know that and explores the swaps as if they were different answers, so the
    # tree grows with the *factorial* of how many identical trucks the fleet holds. Five
    # was affordable and ten was not - measured on one day at a 50% gap, 132 s against
    # 62 s with the rule below, and the constrained run came back with the better schedule
    # of the two because it spent its time somewhere useful.
    #
    #     y[next] <= y[previous]     for consecutive identical vehicles
    #
    # "If you use k of them, use the first k." Valid precisely because they are
    # interchangeable: any schedule using the second and not the first becomes one that
    # does not by renaming, at the same cost. It removes the duplicates from the feasible
    # set rather than merely making them unattractive, which is what the vehicle-id
    # penalty used to do - one euro per id, in the objective, distorting the number it was
    # steering. That penalty is gone (1.5); this is what replaced it.
    #
    # Identical means identical *to the model*, so the test is built from the per-vehicle
    # parameters the model actually reads rather than from the columns of the roster. A
    # figure the objective uses and this signature omits would make two trucks look
    # interchangeable when their costs differ, and the rule would then cut off real
    # answers - which is the one way it can be wrong.
    # .get throughout: the battery-side tables (degradation, public charging) are keyed by
    # bev only, so a diesel truck is legitimately absent from them and a direct lookup
    # raised on the first ICE in the roster. Absent has to read as "no such figure for this
    # vehicle" and compare equal between two diesels, which None does and KeyError does not.
    def _vehicle_signature(m):
        return (bool(is_bev.get(m)),
                vehicle_consumption.get(m), vehicle_energy_storage.get(m),
                vehicle_charging_power.get(m), vehicle_v2g_power.get(m),
                cost_vehicle_100km.get(m), toll_rate_per_km.get(m),
                degradation_cost_efc.get(m), public_charging_eur_per_kwh.get(m))

    # Grouped by signature, not walked in id order. The chain used to be built from
    # *adjacent* sorted ids, which is the same rule only while every run of identical trucks
    # happens to be contiguous. That holds for the shipped roster - the one ice is id 1 and
    # the ten bev are 2-11 - and fails the moment a mixed fleet interleaves them. Move the
    # ice to id 5 and the ten bev become {1,2,3,4} and {6..11}: each half gets ordered and
    # nothing is said between them, so "use the first k" becomes "use the first a of one
    # half and the first b of the other" and the used-sets it admits go from 11 to 5 x 7.
    # Three times the duplicates, from a roster that only listed its trucks in a different
    # order - and silently, because the rows are still written, there are merely fewer of
    # them. A design run is where this bites: its candidate pools hand ids out in type
    # order, so two synthetic types that differ only in vehicle_price - which is capital and
    # so not in the signature below - are interchangeable to the day model and were never
    # chained to each other.
    #
    # Nothing it reported was wrong. The rule removes duplicate optima and never the
    # optimum, so a weaker version of it costs search time and not correctness, and
    # _design_fleet_ids takes copies in order on its own grounds (3.5.2) rather than on
    # this constraint's.
    #
    # Which trucks are interchangeable is a fact about the trucks, so it is read off the
    # signature and not off the order the roster happens to list them in. Ids are sorted
    # first, so both the chain within a group and the order the groups are visited stay
    # deterministic.
    # The rule stops at y on purpose, and taking it further has been tried. Two stronger
    # orderings over the same groups were built and measured in September 2026: driving load
    # descending with the id, and a lexicographic "take your first event only after your
    # predecessor took an earlier one" over all_events_for_z. Both preserve the optimum - 28
    # comparisons against a proven one, including V2G-on cases - and neither earned its
    # place. The baseline alone spreads 3.7x over three seeds on the one instance that
    # converges, which is wider than anything either won by; the lexicographic one also
    # closes from the bound side, handing back a dearer incumbent in 7 of 12 cases, and with
    # three of four days never reaching the target the incumbent is what a run returns.
    #
    # Worth knowing before reaching for them again: the premise that this rule goes inert on
    # a uniform fleet holds only when every truck is used, and it usually is not. Days 93, 2
    # and 1 run 3-4 trucks of 11, so the rule below is live and doing work; day 126 is the
    # one fully-used day in that set, and it is also the only one where a stronger ordering
    # won on every seed.
    identical_groups = {}
    for _m in sorted(vehicles):
        identical_groups.setdefault(_vehicle_signature(_m), []).append(_m)
    for _group in identical_groups.values():
        for _previous, _next in zip(_group, _group[1:]):
            model.addLConstr(y_m[_next] <= y_m[_previous])

    # 3.3.11 power and energy limits per step for bev  (v43: fully updated for virtual trip model)
    # x_m_t_E positive only when charging "trip" (CHG or ext), negative only when V2G "trip" assigned (same as real trip activation).
    # Gating uses z of V2G_t{t} / CHG_t{t} instead of old x locs / x_l_bev.
    # the best a single plugged-in truck can be offered, since it takes the strongest free
    # station; what several of them can draw together is bounded in 3.3.12
    strongest_station_kW = max(charging_infrastructure)

    # Depot charging is not throttled: a plugged-in truck draws the lowest of its own
    # charging power, the station's, and what the battery can still take - and never less
    # (3.3.11b). Stated per truck against the *strongest* station, this is an upper bound
    # and only that: it says no truck ever beats the best hardware on site, not that every
    # truck is standing on it. Which trucks may actually exceed a given station power is
    # settled inside the model, by the tier counting of 3.3.12(c) - no more trucks drawing
    # above any tier than there are stations above it.
    #
    # A mixed depot used to be rejected here instead. While the per-truck cap and the
    # fleet-level greedy bound of 3.3.12 were the only two words on the subject they
    # disagreed about a truck on a weak station - the greedy rule does not fix which truck
    # that is - and the day came back infeasible with nothing named, so any depot whose
    # occupiable stations were not all at least as strong as the strongest bev raised
    # before the solve. 3.3.12(c) states that missing fact as a constraint, which makes a
    # tiered depot - 2 x 100, 3 x 300, 5 x 600 kW against 350 kW trucks, which is what
    # depot_dataset.xlsx held when this was written - a case the model represents rather
    # than one it refuses. The check went with it. The shipped depot is 10 x 600 kW now,
    # so the tiering below costs nothing on it; the constraint stays because the workbook
    # is an input and the next roster may be mixed again.
    for m in bev_vehicles:
        P_ch = vehicle_charging_power[m]  # kW
        E_step_ch_max = P_ch * STEP_HOURS  # kWh per step
        P_v2g = vehicle_v2g_power[m]  # kW for V2G limit; paired with a station below

        # Schritte t >= 1: nie mehr laden als freie Kapazität, und nie mehr entladen als aktuell verfügbar
        for t in time_steps[1:]:
            model.addLConstr(x_m_t_E[m, t] <= vehicle_energy_storage[m] - x_m_SoC[m, t-1])  # freie Kapazität
            model.addLConstr(x_m_t_E[m, t] >= -x_m_SoC[m, t-1])
                
        for t in time_steps:
            # A plugged-in truck takes the strongest station that is free, so on its own it
            # is capped by the strongest station there is; how much the *fleet* can draw
            # together depends on how many are plugged in and is handled in 3.3.12.
            z_chg = z_m_f_s[m, f'CHG_t{t}', t]
            best_station_e = min(vehicle_charging_power[m], strongest_station_kW) * STEP_HOURS
            private_charge_e_cap = best_station_e * z_chg
            ext_contrib = 0.0
            ext_minimum = 0.0
            if 'external_charging' in locations:
                ext_contrib = E_step_ch_max * x_m_t_l[m, t, 'external_charging']
                ext_minimum = (min(charging_min_energy_kWh, E_step_ch_max)
                               * x_m_t_l[m, t, 'external_charging'])
            # a fresh expression, deliberately not `charge_e_cap = private_charge_e_cap`
            # followed by `charge_e_cap += ext_contrib`: LinExpr.__iadd__ mutates in place,
            # so that would add the external term to private_charge_e_cap itself and let an
            # external charger raise the cap of the depot-side E_private below - depot
            # charging without a depot station.
            charge_e_cap = private_charge_e_cap + ext_contrib
            # the cap is a station power, so it bounds the *metered* draw E_pos. What
            # reaches the battery is that much less the conversion loss, which is why the
            # bound on x_m_t_E carries the efficiency: without it the battery-side flow
            # would be held to a grid-side number and the pack would be charged as if the
            # loss did not happen.
            model.addLConstr(x_m_t_E[m, t] <= charging_efficiency * charge_e_cap)
            # E_pos <= same caps (for earnings/peak calcs)
            model.addLConstr(E_pos[m, t] <= charge_e_cap)

            # V2G lower bound + E_neg: gated by explicit V2G "trip" assignment at t (unified with real trip discharge logic)
            # the discharge a step can deliver is the inverter power over half an hour -
            # and the station's, which is the half this used to leave out. A discharging
            # truck stands at a bidirectional charger exactly as a charging one does, so it
            # works at the lower of its own rating and the station's, by the same
            # strongest-free-station rule 3.3.12 hands out for charging. Without the
            # min() a 375 kW truck could put 375 kW back through a 150 kW station: the
            # capacity model bounded the direction that costs money and left the direction
            # that earns it unbounded.
            E_v2g_effective = min(P_v2g, strongest_station_kW) * STEP_HOURS
            vtg = f'V2G_t{t}'
            v2g_z_t = gp.quicksum(z_m_f_s[m, vtg, s] for s in event_possible_starts.get(vtg, [t])) if vtg in v2g_virtual_trips else 0
            if v2g_status_iteration == 'on':
                # E_v2g_effective is what the inverter can put on the grid, so the battery
                # has to give up *more* than that - hence the division. Leaving it out made
                # this bound tighter than the E_neg cap below and silently capped the
                # discharge at discharging_efficiency of the inverter's rating.
                model.addLConstr(
                    x_m_t_E[m, t] >= -E_v2g_effective / discharging_efficiency * v2g_z_t)
            else:
                model.addLConstr(x_m_t_E[m, t] >= 0)
            # E_pos and E_neg are the positive and the negative part of the battery flow,
            # and this identity is what ties them to it. With only the one-sided bounds
            # "E_pos >= x_E" and "E_neg >= -x_E" both were free to exceed the actual flow,
            # and because the objective *pays* for E_neg the solver pushed it to its cap
            # while the battery stood still: V2G earnings and battery degradation booked on
            # energy no battery ever delivered, and no SoC change to match.
            # Complementarity needs no binary here: E_pos is capped by the charging
            # assignment and E_neg by the V2G assignment, and 3.3.4 lets a vehicle hold at
            # most one of them per step, so only one side can be non-zero.
            #
            # 3.3.11c the conversion losses (1.4a). E_pos and E_neg are metered energy -
            # E_pos is what the depot buys and what loads its grid connection, E_neg is
            # what it sells - while x_m_t_E is what the pack actually gains or loses. The
            # two frames differ by the loss, and this is the only place they meet:
            # a drawn kWh arrives as charging_efficiency kWh of charge, and putting a kWh
            # on the grid costs 1/discharging_efficiency kWh of charge. With both set to
            # 1.0 this collapses back to E_pos - E_neg == x_m_t_E, the lossless model.
            model.addLConstr(
                charging_efficiency * E_pos[m, t]
                - E_neg[m, t] / discharging_efficiency == x_m_t_E[m, t])
            if v2g_status_iteration == 'on':
                # The discharge is free between a floor and the hardware cap - it is not
                # pinned to full power the way external charging is (3.3.11d), and the
                # difference is deliberate.
                #
                # Pinning it was tried and taken out again. It is defensible on paper, since
                # both channels are bid in blocks at a rated power, but it costs revenue at
                # the margin for nothing in return: a truck that could sell 40 kWh in its
                # last slot of the day would have to sell the full ~175 kWh or none, and
                # where the larger figure breaches the 24:00 floor the answer is none. The
                # end-of-day rule (3.3.6b) is >= and not ==, so the model was never landing
                # on a knife edge that needed protecting - it was only ever giving up the
                # partial slots at the edges.
                model.addLConstr(E_neg[m, t] <= E_v2g_effective * v2g_z_t)
                # ... and the slot has to deliver. The bound above only gates the discharge
                # from above, so z = 1 with E_neg = 0 stays feasible - a V2G slot that does
                # no V2G. It changes nothing in the objective, so the solver picks such
                # assignments arbitrarily, they occupy the vehicle in 3.3.4 and they show up
                # as activity everywhere the schedule is read. Requiring a minimum discharge
                # makes the implication run both ways, so an assigned slot is a real one.
                model.addLConstr(E_neg[m, t] >= min(v2g_min_discharge_kWh, E_v2g_effective) * v2g_z_t)
            else:
                model.addLConstr(E_neg[m, t] == 0)

            # (the positive part is pinned by the identity above; the cap is in 3.3.11)

            # split E_pos into private (depot/internal via CHG z) vs public (external x) for differentiated charging cost from df
            model.addLConstr(E_private[m, t] + E_public[m, t] == E_pos[m, t])
            model.addLConstr(E_private[m, t] <= private_charge_e_cap)
            model.addLConstr(E_public[m, t] <= ext_contrib)
            # 3.3.11b a plugged-in truck charges at full power, never throttled
            # The caps above only bound E_private from one side, so the optimizer was free
            # to pick any value between a 1 kWh floor and the cap - it would sit at a
            # charger drawing a trickle whenever that suited the peak or the price curve.
            # A depot charger does not modulate: it delivers the lowest of the truck's
            # charging power, the station's power, and what the battery can still take,
            # and nothing less. So E_private is pinned to that minimum rather than
            # bounded by it. Plugging in at all stays the optimizer's decision (z_chg);
            # this only fixes what plugging in means.
            #
            #   full_power = min( best_station_e , battery_side )
            #
            # battery_side collapses the two battery limits into one linear term. Free
            # capacity is (cap - SoC) and the charging-curve derate is K*(cap - SoC), so
            # the tighter of them is min(1/eta_ch, K)*(cap - SoC) - no separate case needed.
            # K = E_step_ch_max / (0.2*cap) < 1/eta_ch only when a full step is less than a
            # fifth of the pack; above that the derate never binds before free capacity does.
            # The free-capacity side is 1/charging_efficiency, not 1: E_private is metered
            # energy, and filling a headroom of H kWh takes H/eta_ch kWh at the meter. At
            # eta_ch = 1 this is the old min(1, K).
            derate_factor = (E_step_ch_max / (0.2 * vehicle_energy_storage[m])
                             if charging_curve_status == 'on' else float('inf'))
            battery_factor = min(1.0 / charging_efficiency, derate_factor)
            headroom_0 = (1 - initial_soc_fraction) * vehicle_energy_storage[m]
            if charging_power_modulation == 'on':
                # 3.3.11c modulated charging: the caps are physics and stay, the floor
                # keeps an occupied slot honest, and nothing forces the maximum in
                # between. What the truck draws is then a decision, which is the point -
                # it is how the fleet buys a lower peak with the same energy.
                #
                # The floor matters more than it looks. Without it a truck could hold a
                # cable at exactly zero and pay only penalty_charging_use for it, which is
                # a charger occupied for nothing; with it, plugging in means moving at
                # least charging_min_energy_kWh, the same floor external charging has
                # carried all along. On an asset-sizing run the number of cables that
                # implies is priced on top, by penalty_charger_use (1.5a).
                ceiling = (min(best_station_e, battery_factor * headroom_0) if t == 0
                           else best_station_e)
                model.addLConstr(E_private[m, t] <= ceiling * z_chg)
                if t > 0:
                    # the battery side as well, stated here rather than left to E_pos:
                    # the comment below is right that an implicit bound three constraints
                    # away is the kind that gets loosened by accident
                    model.addLConstr(
                        E_private[m, t] <= battery_factor
                                           * (vehicle_energy_storage[m] - x_m_SoC[m, t-1]))
                model.addLConstr(
                    E_private[m, t] >= min(charging_min_energy_kWh, best_station_e) * z_chg)
            elif t == 0:
                # the day-start level is a constant, so the minimum is one too
                model.addLConstr(
                    E_private[m, t] == min(best_station_e, battery_factor * headroom_0) * z_chg)
            else:
                # min(constant, linear) needs one binary to state exactly. It is not a
                # free choice: with full_binds = 0 the station side is forced and the caps
                # above make that infeasible unless it really is the smaller, and the other
                # way round for 1 - so the binary is pinned by feasibility, not selected.
                headroom = vehicle_energy_storage[m] - x_m_SoC[m, t-1]
                # one big-M per constraint, each only as large as that constraint needs.
                # A single shared M of best_station_e + battery_factor*cap was valid but
                # loose, and a loose big-M is a weak LP relaxation - the bound the solver
                # prunes against, and the reason a run spends its time in the tree.
                m_station = best_station_e
                m_battery = battery_factor * vehicle_energy_storage[m]
                full_binds = model.addVar(vtype=gp.GRB.BINARY, name=f"chg_full_{m}_{t}{tag}")
                # The claim below - that full_binds is pinned by feasibility rather than
                # chosen - is only true while E_private is bounded above by the battery
                # side as well as by the station side. It was, but only *implicitly*: via
                # E_pos <= charge_e_cap, the charging-curve derate on E_pos, and
                # E_private <= E_pos. Three constraints in two other places, none of which
                # says what it is holding up. Loosen any of them and the solver could set
                # full_binds = 0 with E_private below full power, which is the exact
                # behaviour 3.3.11b exists to forbid - and nothing would fail, the schedule
                # would just quietly throttle. Stated here so the dependency is local.
                model.addLConstr(E_private[m, t] <= battery_factor * headroom)
                model.addLConstr(
                    E_private[m, t] >= best_station_e * z_chg - m_station * full_binds)
                model.addLConstr(
                    E_private[m, t] >= battery_factor * headroom
                                       - m_battery * (1 - full_binds)
                                       - m_battery * (1 - z_chg))
            # 3.3.11d a public stop draws everything the truck can take
            #
            # Not modulated, and deliberately unlike depot charging (3.3.11c). The reason
            # is penalty_charging_external_time: a step at a public station costs the
            # driver's time by the minute and dwarfs the energy in it, so the only sane way
            # to use one is to take the most the truck will accept and leave. Throttling
            # there would buy a slightly better peak - on somebody else's meter, which this
            # model does not pay - at the price of a second half hour of driver time, which
            # it does.
            #
            # The cap is the truck's own charging power, not a station's: a public charger
            # is not in charging_infrastructure and the model knows nothing about its
            # rating, so the vehicle is the only limit it can honestly apply. The battery
            # is the other one, and on the last stop of a fill it is the binding one -
            # which is what makes this a min() and not a constant.
            if 'external_charging' in locations:
                x_ext = x_m_t_l[m, t, 'external_charging']
                ext_battery_side = (battery_factor * headroom_0 if t == 0
                                    else battery_factor
                                         * (vehicle_energy_storage[m] - x_m_SoC[m, t-1]))
                model.addLConstr(E_public[m, t] <= E_step_ch_max * x_ext)
                model.addLConstr(E_public[m, t] <= ext_battery_side)
                if t == 0:
                    # both sides are constants at the day start, so the min is one too
                    model.addLConstr(
                        E_public[m, t] == min(E_step_ch_max,
                                              battery_factor * headroom_0) * x_ext)
                else:
                    # min(constant, linear) again, pinned by feasibility exactly as the
                    # depot version was before modulation: with ext_full = 0 the station
                    # side is forced and the caps above make that infeasible unless it
                    # really is the smaller, and the other way round for 1
                    ext_full = model.addVar(vtype=gp.GRB.BINARY,
                                            name=f"ext_full_{m}_{t}{tag}")
                    m_ext_station = E_step_ch_max
                    m_ext_battery = battery_factor * vehicle_energy_storage[m]
                    model.addLConstr(
                        E_public[m, t] >= E_step_ch_max * x_ext
                                          - m_ext_station * ext_full)
                    model.addLConstr(
                        E_public[m, t] >= ext_battery_side
                                          - m_ext_battery * (1 - ext_full)
                                          - m_ext_battery * (1 - x_ext))
            else:
                model.addLConstr(E_public[m, t] >= ext_minimum)

        # t=0 limits (full/empty battery start). Outside the per-step loop: they do not
        # depend on t, and adding them once per step put 48 copies of each into the model.
        model.addLConstr(x_m_t_E[m, 0] <= (1 - initial_soc_fraction) * vehicle_energy_storage[m])
        model.addLConstr(x_m_t_E[m, 0] >= -initial_soc_fraction * vehicle_energy_storage[m])

        # Charging curve derate (applied to E_pos): taper above ~80% SoC using linear factor on remaining capacity.
        # Step 0 is derated too, against the level the day starts at - it is a step like
        # any other since 3.3.6 puts it into the SoC balance, and skipping it would let a
        # truck that starts nearly full take a full-power half hour it could not sustain.
        if charging_curve_status == 'on':
            model.addLConstr(
                E_pos[m, 0] <= E_step_ch_max
                * (vehicle_energy_storage[m] - initial_soc_fraction * vehicle_energy_storage[m])
                / (0.2 * vehicle_energy_storage[m])
            )
            for t in time_steps[1:]:
                model.addLConstr(
                    E_pos[m, t] <= E_step_ch_max * (vehicle_energy_storage[m] - x_m_SoC[m, t-1]) / (0.2 * vehicle_energy_storage[m])
                )

    # 3.3.11b external charging only for bev (kept for external loc x), and only on the road
    #
    # A public charger is somewhere other than the home depot. Nothing said so: x_ext was
    # free in any step the vehicle was not otherwise occupied, at_depot included, so a
    # truck standing in its own yard could be charging "externally". That is not merely a
    # mislabelled row in the schedule - E_public is bounded by the truck's own power alone
    # and takes no share of 3.3.12, so a truck at home could draw full power through a
    # station the depot does not have and outside every capacity rule this model has. The
    # 10 EUR/min time penalty made it unattractive rather than impossible, and 3.4b waives
    # exactly that penalty during a rest, which is when the truck is most likely to be
    # standing at home.
    #
    # Only where the model knows where the truck is. With route_chaining_status = 'off'
    # there is no geography to place a charger in and at_depot is a driving indicator
    # rather than a location (3.3.16d-off), so the gate would read "public charging only
    # while driving" - which is why it is tied to the routing model and not to at_depot
    # merely existing.
    if 'external_charging' in locations:
        for m in vehicles:
            for t in time_steps:
                model.addLConstr(x_m_t_l[m, t, 'external_charging'] <= is_bev[m])
                if chaining_on:
                    model.addLConstr(
                        x_m_t_l[m, t, 'external_charging'] <= 1 - at_depot[m, t])

    # 3.3.12 the depot's stations, handed out strongest-first
    #
    # Which station a truck gets is not optimised. A truck that plugs in takes whichever
    # free station has the most power, so with k trucks plugged in at once the fleet
    # occupies the k strongest stations - a fact of the rule, not a decision. Two
    # constraints per step express it:
    #
    #   (a) no more trucks connected than there are stations
    #   (b) the energy they move together stays within the k strongest stations
    #   (c) no single truck moves more than the station it can actually be standing on
    #
    # CONNECTED, not charging. Every one of the three used to count only CHG assignments
    # and bound only E_private, so the whole capacity model saw one direction of a
    # bidirectional charger. A discharging truck occupied no station, took no share of the
    # aggregate power and answered to no tier - so the fleet could sell more power into the
    # grid at once than the depot has hardware to sell it through, and postprocess would
    # then reconstruct a station assignment (5.6) that the model's own numbers do not
    # support. A truck plugged in to discharge is plugged in; it is the same cable.
    #
    # 3.3.4 lets a vehicle hold at most one event per step, so a truck never charges and
    # discharges in the same half hour and E_private[m,t] + E_neg[m,t] is whichever of the
    # two is happening. That is what makes "connected power" a single linear expression
    # rather than a case distinction.
    #
    # (b) is piecewise linear in k. The cumulative power of the sorted stations is concave
    # (each further station is weaker than the last), so writing one line per breakpoint
    # and taking them all as upper bounds reproduces it exactly at every integer k: at
    # k = j the j-th line is tight and the others are slack.
    #
    # This replaces the per-(vehicle, station, step) assignment binaries - on a 10-station
    # depot with 10 vehicles, 4800 of them, about three quarters of the whole model.
    # what k trucks can draw together at best: the k strongest trucks on the k strongest
    # stations, each pair limited by the weaker of the two. Summing the station powers
    # alone would ignore that a 350 kW truck cannot use a 600 kW station fully - on this
    # depot that overstated the fleet's capacity by up to 1250 kW and weakened the bound
    # for nothing. Both sequences descend, so their pairwise minimum does too, which is
    # what makes the cumulative sum concave and the lines below exact at integer k.
    # taken over both directions, for the same reason the consistency check above is: the
    # pairing bounds what a truck can move through a station, and a bidirectional station
    # moves it either way. max() rather than the charging figure alone, so a fleet whose
    # V2G rating ever exceeds its charging rating is bounded by the larger of the two
    # instead of by the one that happens to be in the column named 'charging'.
    stations_desc = sorted(charging_infrastructure, reverse=True)
    truck_powers_desc = sorted((max(vehicle_charging_power[m], vehicle_v2g_power[m])
                                for m in bev_vehicles), reverse=True)
    pairable = min(len(stations_desc), len(truck_powers_desc))
    pair_kW = [min(truck_powers_desc[i], stations_desc[i]) for i in range(pairable)]
    cumulative_kW = [0.0]
    for power in pair_kW:
        cumulative_kW.append(cumulative_kW[-1] + power)

    # (c) the tiers, and how many trucks may be above each of them.
    #
    # (a) and (b) bound the fleet, and on a depot whose stations are all alike that is the
    # whole story, which is the case the shipped 10 x 600 kW depot is in - the loop below
    # writes nothing on it. A tiered depot is not: take 2 x 100 kW, 3 x 300 kW, 5 x 600 kW,
    # and there the aggregate bound alone is too generous, because it never says *which* truck
    # got which station. Six trucks plugged in may draw 1025 kWh between them under (b);
    # split evenly that is 170.8 kWh each, which needs six stations above 341 kW when the
    # depot has five. The sixth truck is on a 300 kW station and can take 150. Nothing in
    # (a) or (b) notices, and postprocess then reconstructs an assignment (5.6) that the
    # model's own numbers do not support.
    #
    # What closes it is a counting rule rather than an assignment. A truck drawing more
    # than a tier's power has to be standing on a station above that tier, and there are
    # only so many of those:
    #
    #     #{trucks drawing more than P} <= #{stations stronger than P}    for each tier P
    #
    # That is Hall's condition on this depot, and it is sufficient as well as necessary
    # here: the sets nest, so once every tier is satisfied the strongest-draw-to-strongest-
    # station greedy the reconstruction uses always succeeds.
    #
    # It costs one binary per (truck, step, tier *below the strongest*) - two tiers here,
    # so a tenth of the per-station assignment binaries (a) and (b) were written to avoid,
    # and none at all on a single-tier depot, where the loop below does not execute.
    station_tiers = sorted({float(p) for p in stations_desc[:pairable]}, reverse=True)
    # how many usable stations are strictly stronger than each tier
    stronger_than = {tier: sum(1 for p in stations_desc[:pairable] if p > tier)
                     for tier in station_tiers[1:]}
    strongest_e = max(stations_desc) * STEP_HOURS if stations_desc else 0.0

    def _connected_z(m, t):
        """1 while m occupies a depot station in step t, charging or discharging."""
        vtg = f'V2G_t{t}'
        charging = z_m_f_s[m, f'CHG_t{t}', t]
        if vtg not in v2g_virtual_trips:
            return charging
        return charging + gp.quicksum(z_m_f_s[m, vtg, s]
                                      for s in event_possible_starts.get(vtg, [t]))

    for t in time_steps:
        connected = gp.quicksum(_connected_z(m, t) for m in bev_vehicles)
        # (a) a station can hold one truck, so the number of stations is the ceiling
        model.addLConstr(connected <= pairable)
        # (b) k trucks share the k strongest stations, in whichever direction they use them
        depot_flow = gp.quicksum(E_private[m, t] + E_neg[m, t] for m in bev_vehicles)
        for j in range(pairable):
            model.addLConstr(
                depot_flow <= (cumulative_kW[j] + pair_kW[j] * (connected - j)) * STEP_HOURS)
        # (c) one tier at a time
        for tier, available in stronger_than.items():
            tier_e = tier * STEP_HOURS
            headroom = strongest_e - tier_e
            if headroom <= 0:
                continue
            above = []
            for m in bev_vehicles:
                over = model.addVar(vtype=gp.GRB.BINARY,
                                    name=f"above_tier_{int(tier)}{tag}[{m},{t}]")
                # held to the tier unless the truck is marked as needing a stronger
                # station; marked, it may go up to the strongest station there is, which
                # is the cap 3.3.11 already applies. On the flow through the cable, so a
                # truck discharging 375 kW needs a station above 375 kW exactly as a truck
                # charging at 375 kW does.
                model.addLConstr(E_private[m, t] + E_neg[m, t] <= tier_e + headroom * over)
                # a truck that is not connected moves nothing and never needs one
                model.addLConstr(over <= _connected_z(m, t))
                above.append(over)
            model.addLConstr(gp.quicksum(above) <= available)

    # 3.3.13 (removed): station power limits for charging energy now consolidated into effective per-location cap
    # in 3.3.11 power limits (previous per-l constraints were incorrect and forced x_E <=0 whenever >1 station defined).

    # 3.3.14 (removed): cumulative pre-charge condition for V2G.
    # It required the discharge up to every step to stay within the charging up to that
    # same step, which forbade selling any of the energy a truck starts the day with. That
    # was a policy, not physics, and it is no longer needed for either: the SoC balance
    # (3.3.6) already stops a battery going below empty, and the 24:00 target (3.3.6b)
    # already makes every kWh driven or sold be charged back within the day.

    # 3.3.15 site load profile + PV + peak shaving (implemented)
    # site_import[t] is the power drawn from the PUBLIC GRID at the depot [kW]:
    #   depot baseline demand + depot chargers - bidirectional discharge - PV
    # Only depot-side charging (E_private) flows through the depot grid connection;
    # E_public is drawn at an external charger and billed by its operator, so it must
    # not raise the depot peak. Bidirectional discharge (E_neg) serves the depot load
    # first and therefore lowers the power drawn from the grid.
    # The full PV generation is netted here, whoever consumes it: the peak relief follows
    # from the plant existing, not from the charging decision. Which share of it the
    # trucks take - and are billed for - is 3.3.15b.
    #
    # 3.3.15a the one infeasibility this cap can cause, diagnosed before it happens.
    #
    # site_import is floored at 0 and capped at site_peak_limit_kW, so a step in which the
    # depot's own inelastic load already exceeds the cap net of its own PV is infeasible
    # before a single truck moves. The fleet cannot help: E_private only adds to the draw
    # and the most V2G could do is subtract, which needs a truck with charge standing at a
    # station. This file pre-diagnoses the other way a day can be infeasible - a trip that
    # fits no route (3.3.16a-check) - and left this one to come back as Gurobi's bare
    # INFEASIBLE, which names nothing and sends the reader looking at the trips.
    over_cap = [(t, depot_baseline_load_kW[t] - pv_generation_kW[t]) for t in time_steps
                if depot_baseline_load_kW[t] - pv_generation_kW[t] > site_peak_limit_kW]
    if over_cap:
        worst_t, worst_kW = max(over_cap, key=lambda pair: pair[1])
        raise ValueError(
            f"the depot's own load exceeds site_peak_limit_kW ({site_peak_limit_kW:g} kW) "
            f"in {len(over_cap)} step(s) before any truck charges - worst at "
            f"{step_to_time(worst_t)} with {worst_kW:,.0f} kW of baseline load net of its "
            f"own PV. No disposition can satisfy the cap, so the day would come back "
            f"infeasible with no cause in the fleet. Raise site_peak_limit_kW, or check the "
            f"'consumption' sheet of depot_dataset.xlsx for the day being planned.")
    site_import = {}
    site_peak_kW = model.addVar(lb=0, ub=site_peak_limit_kW, name=f"site_peak_kW{tag}")
    for t in time_steps:
        site_import[t] = model.addVar(lb=0, name=f"site_import_{t}{tag}")
        depot_charging_power_t = gp.quicksum(E_private[m, t] for m in bev_vehicles) / STEP_HOURS
        v2g_power_t = gp.quicksum(E_neg[m, t] for m in bev_vehicles) / STEP_HOURS
        model.addLConstr(
            site_import[t] >= depot_baseline_load_kW[t] + depot_charging_power_t - v2g_power_t - pv_generation_kW[t]
        )
        model.addLConstr(site_import[t] <= site_peak_limit_kW)
        # the demand charge is billed on the highest grid draw of the day
        model.addLConstr(site_peak_kW >= site_import[t])

    # the peak the depot would draw with no bev at all: its own load, net of its own PV,
    # floored at zero per step because a site that generates more than it consumes draws
    # nothing rather than negative. A constant - it holds no decision variable - and the
    # counterfactual the demand charge is measured against below.
    baseline_peak_kW = max(
        [max(0.0, depot_baseline_load_kW[t] - pv_generation_kW[t]) for t in time_steps] + [0.0])

    # 3.3.15b own PV generation charged into the trucks [kWh per step]
    # E_pv_charging[t] is the part of that step's depot charging that the site's own PV
    # plant covers. It is not a free decision: the plant sits behind the depot meter, so
    # whatever it generates beyond the inelastic site load flows into whatever is
    # charging at that moment. Hence the exact
    #     E_pv_charging[t] = min(PV surplus[t], depot charging[t])
    # rather than an upper bound - the equality holds whether the PV kWh is cheaper or
    # dearer than the spot price, so the accounting cannot drift from the physics.
    # Priced at pv_charging_eur_per_kWh_t in the objective below; only the remainder
    # is bought from the grid at electricity_spot_price_€/kWh.
    #
    # This block is pinned by *feasibility*, not by the objective: the two big-M lines make
    # exactly one branch of the min feasible per step. That is what keeps the reported PV
    # share exact under the default opportunity price, where the objective coefficient on
    # E_pv_charging is zero and nothing would otherwise push the variable anywhere. The
    # binaries then buy reporting only - one per step with a PV surplus - so switch to a
    # fixed opportunity price if their solve time ever matters more than the figure does.
    E_pv_charging = {}
    max_depot_charge_kWh = sum(vehicle_charging_power[m] for m in bev_vehicles) * STEP_HOURS
    for t in time_steps:
        surplus_kWh = pv_charging_available_kWh[t]
        # the upper bound already carries "no more than the plant has left over", and it
        # fixes the variable to 0 for every step without surplus - at night, and whenever
        # the site's own load alone exceeds the generation
        E_pv_charging[t] = model.addVar(lb=0, ub=surplus_kWh, name=f"E_pv_charging_{t}{tag}")
        if surplus_kWh <= 0:
            continue
        depot_charging_kWh = gp.quicksum(E_private[m, t] for m in bev_vehicles)
        model.addLConstr(E_pv_charging[t] <= depot_charging_kWh)
        # ... and equal to the smaller of the two: surplus_binding = 1 makes the surplus
        # the binding side (the trucks take all of it), 0 the charging demand
        surplus_binding = model.addVar(vtype=gp.GRB.BINARY, name=f"pv_surplus_binding_{t}{tag}")
        big_m = max(surplus_kWh, max_depot_charge_kWh)
        model.addLConstr(E_pv_charging[t] >= surplus_kWh - big_m * (1 - surplus_binding))
        model.addLConstr(E_pv_charging[t] >= depot_charging_kWh - big_m * surplus_binding)

    # 3.3.15c vehicle-to-vehicle: the kWh that never reach the meter
    #
    # When one truck is discharging and another is charging in the same half hour, both at
    # the depot, the energy goes straight from the first to the second across the yard's
    # own busbar. It is never bought and never sold, so it carries neither the grid
    # overhead on the way in nor the marketing overhead on the way out.
    #
    #     E_v2v[t] = min( what the depot is charging , what the depot is discharging )
    #
    # Both sides are measured at the charger terminals, so they are the same AC kWh and
    # comparable without any conversion. The losses are unchanged: the energy still passes
    # the discharging truck's inverter and the charging truck's rectifier, so it is taxed
    # by both efficiencies exactly as a grid round trip would be. What V2V removes is the
    # fees, not the physics.
    #
    # THE DISCHARGE IS SPLIT AT SOURCE. A kWh leaves a battery in exactly one direction:
    # out to the public grid, where it is sold as arbitrage or paid for as flexibility, or
    # across the yard into another truck. It cannot be both. Both V2G channels are
    # deliveries to the grid - arbitrage is an energy sale across the meter, and
    # flexibility is a service the grid only receives if the power actually arrives there -
    # so a kWh that went next door was neither sold nor delivered. 3.4 therefore pays the
    # V2G price on (discharge - E_v2v) and nothing on E_v2v, and the receiving side is
    # refunded the whole buy price rather than the fees on it. A transferred kWh then costs
    # exactly nothing: no purchase, no sale, no fees either way.
    #
    # The earlier version paid the V2G price on the gross discharge and refunded only the
    # two overheads. For arbitrage that is self-consistent, but by coincidence: the sell
    # price is spot - selling_overhead, so buy, sell and refund cancel to zero. For
    # flexibility it is not, because that price is a service fee with no spot term in it to
    # cancel, and the depot collected flex_price - spot + selling_overhead per kWh for a
    # service it had not delivered. Measured on day 93 before this change: 1652 of 2404
    # discharged kWh were V2V, flexibility won all 48 steps, and the leak came to 311 EUR
    # against a 2504 EUR objective.
    #
    # PINNED BY FEASIBILITY, NOT BY THE OBJECTIVE - the same construction as the PV split
    # above, and the split is what made it necessary. While the refund was the overheads,
    # E_v2v carried a non-negative coefficient and the objective always pushed it up
    # against the tighter bound, so <= was enough. Its coefficient is now
    # buy_price - v2g_price, whose sign depends on the day: let the V2G price exceed the
    # buy price and the objective would drive E_v2v to zero and book a full export and a
    # full import in the same half hour, through one meter that physically nets them. So
    # the min is forced, and the figure stays exact whatever the prices do.
    E_v2v = {}
    max_depot_discharge_kWh = sum(vehicle_v2g_power[m] for m in bev_vehicles) * STEP_HOURS
    for t in time_steps:
        E_v2v[t] = model.addVar(lb=0.0, name=f"E_v2v_{t}{tag}")
        if v2v_status != 'on':
            model.addLConstr(E_v2v[t] == 0.0)
            continue
        depot_charging_kWh = gp.quicksum(E_private[m, t] for m in bev_vehicles)
        depot_discharge_kWh = gp.quicksum(E_neg[m, t] for m in bev_vehicles)
        # the two sides of the min. The second competes with the sun for the same charging
        # demand: own PV and a neighbouring truck are both local supply, and counting a kWh
        # as fed by both would deliver one kWh twice.
        local_demand_kWh = depot_charging_kWh - E_pv_charging[t]
        model.addLConstr(E_v2v[t] <= depot_discharge_kWh)
        model.addLConstr(E_v2v[t] <= local_demand_kWh)
        # ... and equal to the smaller of them: discharge_binding = 1 makes the discharge
        # the binding side (every kWh out of a battery lands in another one), 0 the
        # charging demand left after the sun
        discharge_binding = model.addVar(vtype=gp.GRB.BINARY,
                                         name=f"v2v_discharge_binding_{t}{tag}")
        big_m = max(max_depot_discharge_kWh, max_depot_charge_kWh)
        model.addLConstr(E_v2v[t] >= depot_discharge_kWh
                         - big_m * (1 - discharge_binding))
        model.addLConstr(E_v2v[t] >= local_demand_kWh - big_m * discharge_binding)

    # 3.4 define MILP cost function
    # driving_cost only for ice (fuel); bev energy costs now explicit via charging location (private/public from cost_parameters_energy df)
    driving_cost = (gp.quicksum(cost_vehicle_100km[m] * 0.01 * trip_distances[f] * z_m_f_s[m, f, s]
        for m in vehicles for f in day_trips_list for s in event_possible_starts.get(f, [])
        if m in ice_vehicles)
        # ... and the empty running, at the same rate: a diesel burns fuel repositioning
        # exactly as it does under load. The bev side needs no counterpart here, because
        # its empty kilometres are already in the SoC balance (3.3.16f) and therefore in
        # the charging bill.
        + gp.quicksum(cost_vehicle_100km[m] * 0.01 * deadhead_km_total(m)
                      for m in ice_vehicles))
    # Only what actually reached the grid earns. E_v2v is the part of the step's discharge
    # that went next door instead (3.3.15c), and neither channel pays for that: arbitrage
    # is a sale across the meter and flexibility is a service delivered to the grid, and a
    # kWh in the truck next door is neither.
    v2g_earnings = gp.quicksum(
        -(costs_v2g[t] / 1000.0)
        * (gp.quicksum(E_neg[m, t] for m in bev_vehicles) - E_v2v[t])
        for t in time_steps)
    # vehicle-to-vehicle (3.3.15c). The charging term above bills every kWh the trucks took
    # in, at that step's buy price, including the ones that came from the truck next door
    # and never crossed the meter. This gives the whole of that back - the price, not the
    # fees on it - which together with earning nothing on the sending side leaves a
    # transferred kWh at exactly zero.
    #
    # It creates no incentive to shuffle energy for its own sake: a kWh moved from one
    # truck to another still loses both conversions, and restoring the sending truck costs
    # that loss at the full buy price. The saving is only ever worth having when the
    # discharge and the charge were both worth doing anyway.
    v2v_saving = -gp.quicksum(depot_buy_eur_per_kWh_t[t] * E_v2v[t] for t in time_steps)
    # Battery aging, always priced - there is no switch for this (see the parameter block).
    #
    # Degradation is charged on V2G discharge only (E_neg). Driving and ordinary charging
    # age the battery too, but that wear is an unavoidable consequence of operating the
    # truck and is not attributable to the V2G decision, so pricing it here would only add
    # a constant-ish offset that biases the V2G business case. EFC keeps the usual
    # definition throughput / (2 * capacity), with the throughput narrowed to the
    # discharged energy.
    # + distribution via max_efc penalty (to spread aging, not concentrate on few vehicles)
    #
    # 3.4d WHAT THIS AGING MODEL LEAVES OUT, AND WHY
    #
    # Cycle depth and the SoC window are in it (soc_weight_factor, below). The two other
    # stressors the cell literature puts beside them are deliberately not, and both
    # omissions are assumptions about BEV trucks rather than simplifications of convenience:
    #
    #   (a) C-RATE. Rate-dependent aging is not modelled, because a heavy BEV truck does not
    #       reach the rates at which it matters. The packs here are 500-900 kWh, and the
    #       hardware on either side of them is small against that: a 350 kW depot charger
    #       into a 600 kWh pack is ~0.6C, V2G discharge is bounded by vehicle_v2g_power,
    #       which is the same converter or less, and traction draw is smaller again - a 40 t
    #       truck at motorway speed pulls a few hundred kW. Even megawatt charging only
    #       reaches ~1.7C, and only below 80% SoC before charging_curve_status derates it.
    #       So the whole operating envelope sits at around 1C at most, which is the region
    #       every rate-dependent aging study finds flat; the steep part of those curves is at
    #       2-4C and above, and it is reached by small packs, not by truck packs. Adding a
    #       C-rate term would therefore multiply the model's size - the rate is a variable
    #       here, so the term would be bilinear - for a coefficient that is constant over
    #       the range the trucks can actually operate in.
    #
    #   (b) TEMPERATURE. Temperature-dependent aging is not modelled either, because the
    #       vehicles are assumed to have a working battery thermal management system, which
    #       every modern BEV truck does: the pack is liquid-conditioned and held near its
    #       design point whether the truck is driving, fast-charging or standing on a
    #       charger overnight. The cell therefore does not see ambient temperature, it sees
    #       the setpoint, and an aging term in a variable that is held constant by design is
    #       a constant. What a thermal model WOULD add to this study is the energy the
    #       conditioning itself draws, which is a consumption question rather than an aging
    #       one and is inside vehicle_consumption.
    #
    # Both assumptions are stated in the README under "Battery degradation". They hold for
    # the fleet this model is built for; a study of light vehicles, of small packs pushed to
    # 3C, or of unconditioned packs in a cold yard would have to revisit them - and revisit
    # them here, in this expression, since this is where every kWh of wear is priced.
    max_efc = None
    degrad_expr = 0
    efc_m_vars = {}
    # the tangents are the same for every vehicle and step - the weight is a function
    # of the SoC *fraction*, so only the division by capacity below is per vehicle
    soc_weight_tangents = soc_aging_weight_tangents()
    if advanced_degradation_status == 'on':
        max_efc = model.addVar(lb=0, name=f"max_efc_degrad{tag}")
    for m in bev_vehicles:
        cap = vehicle_energy_storage[m]
        efc_base = degradation_cost_efc[m]
        # per-vehicle efc for the distribution penalty. This one stays on the full
        # throughput: it spreads *physical* wear evenly over the fleet, which happens
        # whether or not the wear is charged for.
        if advanced_degradation_status == 'on':
            efc_m = model.addVar(lb=0, name=f"efc_{m}{tag}")
            efc_m_vars[m] = efc_m
            drive_total_m = (gp.quicksum(
                (vehicle_consumption[m] / 100.0) * trip_distances[f] * z_m_f_s[m, f, s]
                for f in day_trips_list for s in event_possible_starts.get(f, []))
                + (vehicle_consumption[m] / 100.0) * deadhead_km_total(m))
            # what the *cells* see, not what the meter sees: the loss happens in the
            # converter, so the kWh that never reaches the battery never ages it either
            grid_thp_m = gp.quicksum(
                charging_efficiency * E_pos[m, t] + E_neg[m, t] / discharging_efficiency
                for t in time_steps)
            model.addLConstr(efc_m == (drive_total_m + grid_thp_m) / (2.0 * cap))
            model.addLConstr(max_efc >= efc_m)
        for t in time_steps:
            # the discharge as the battery delivers it. Selling E_neg to the grid costs
            # E_neg/eta_dis out of the pack, so that - not the metered figure - is the
            # throughput the aging is charged on.
            throughput = E_neg[m, t] / discharging_efficiency
            # Aging weight on the SoC the step starts from: 1.0 in the middle of the
            # window, rising to 1 + soc_weight_factor at both ends, because a cell ages
            # fastest held full and fastest again run flat. Quadratic in the deviation,
            # so the penalty stays mild across the working middle and steepens towards
            # the extremes rather than growing at a constant rate.
            # Carried as the maximum of the parabola's tangents rather than as the
            # square itself: w * E_neg would be cubic, and giving the square its own
            # variable needs a quadratic constraint, which made the model quadratically
            # constrained and cost far more in the solver than the last 1.6% of the
            # shape is worth. The tangents are plain linear constraints, and because
            # the coefficient on soc_w_var is efc_base * E_neg / (2*cap) >= 0 the
            # minimisation drives it onto their maximum - exact, wherever it matters.
            #
            # With the weighting on, soc_w is a variable and throughput carries E_neg, so
            # the product below is bilinear and the model is a non-convex MIQCP rather
            # than a MILP - which is what 3.5 measures the design run against and why the
            # monolith over several days does not scale. soc_weight_factor == 0 is the
            # branch that keeps it linear: every kWh then ages the battery alike, and the
            # weight is taken as the scalar 1.0 rather than as a variable pinned to it,
            # because E_neg * variable is a product of two variables however tightly the
            # second is bounded.
            if t == 0 or soc_weight_factor == 0:
                soc_w = soc_aging_weight(initial_soc_fraction if t == 0 else 0.5)
            else:
                soc_w_var = model.addVar(lb=1.0, ub=1.0 + soc_weight_factor,
                                         name=f"soc_aging_w_{m}_{t}{tag}")
                for x_i, w_i, slope_i in soc_weight_tangents:
                    model.addLConstr(
                        soc_w_var >= w_i + slope_i * (x_m_SoC[m, t-1] / cap - x_i))
                soc_w = soc_w_var
            weighted_thp = throughput * soc_w
            degrad_contrib = efc_base * weighted_thp / (2.0 * cap)
            degrad_expr += degrad_contrib
    degradation_cost = degrad_expr
    # annualized DSO demand charge [€/kW/year] apportioned to one day and billed on
    # the daily maximum grid draw (NOT summed over the steps - that would be a €/kWh tariff).
    # Charged on the *increment* the fleet causes, not on the whole site peak: the depot
    # draws baseline_peak_kW whether or not a single truck is electric, so that part is
    # not a cost of the disposition and attributing it to the fleet only inflates every
    # figure the run reports by the same amount.
    # The term is signed on purpose. If V2G pulls the peak below what the site alone would
    # have drawn, the difference is negative and the depot really does pay a smaller demand
    # charge than it would without the fleet - peak shaving is one of the things V2G is
    # for, and a floor at zero would hide it. The credit is bounded: site_import >= 0
    # per step, so the peak cannot fall below zero and the saving cannot exceed the
    # baseline charge itself.
    # Note this shifts the objective by a constant and so cannot change which schedule is
    # optimal - it changes what the run attributes to the fleet, not what the fleet does.
    peak_shaving_cost = (peak_power_price_eur_per_kW / 365.0) * (site_peak_kW - baseline_peak_kW)

    # 3.4c how many chargers the day needs at its busiest (penalty_charger_use, 1.5)
    #
    # 3.3.12(a) already forbids more trucks connected at once than the depot has stations,
    # so the fleet cannot exceed the hardware. What it never asked was whether the hardware
    # is needed: a run that plugs eight trucks in at once and a run that never plugs in more
    # than three cost the same, and only the second can be built on a three-cable depot.
    #
    # Same shape as drivers_needed (3.3.17): the count is a peak, not a total, because what
    # a depot buys is cables and the worst half hour of the day is what sets how many.
    # Connected, not charging - a truck selling into V2G holds the cable exactly as one
    # taking energy in does.
    #
    # Continuous rather than integer: it is bounded below by sums of binaries and carries a
    # positive cost, so the solver pushes it down onto one of those sums and it lands on an
    # integer without being told to. One variable and one row per step.
    #
    # Asset-sizing runs only (1.5a). "How many cables does the day need" is a buying
    # decision, and a disposition or a sweep has already bought them - there the term prices
    # hardware the operator owns and cannot change. Written as nothing at all rather than as
    # a zero coefficient, so those runs carry neither the variable nor its 48 rows and the
    # model is the one they would have had before this term existed.
    charger_price = charger_use_price(auto_sizing)
    charger_use_penalty = 0.0
    if charger_price:
        chargers_needed = model.addVar(lb=0.0, name=f"chargers_needed{tag}")
        for t in time_steps:
            connected_now = []
            for m in bev_vehicles:
                connected_now.append(z_m_f_s[m, f'CHG_t{t}', t])
                vtg = z_m_f_s.get((m, f'V2G_t{t}', t))
                if vtg is not None:
                    connected_now.append(vtg)
            model.addLConstr(chargers_needed >= gp.quicksum(connected_now))
        charger_use_penalty = charger_price * chargers_needed
    external_time_penalty = 0
    if 'external_charging' in locations and external_charging_status == 'on':
        penalized_external = model.addVars(bev_vehicles, time_steps, vtype=gp.GRB.BINARY, name=f"penalized_external{tag}")
        # 3.4b the waiver, tied to the break it stands for.
        #
        # Charging at a public station costs the driver time - the detour, the wait, the
        # tour not driven - and that is what penalty_charging_external_time prices. Unless
        # the truck has to stand still anyway because a rest is due, in which case the time
        # was already lost to the rest and charging through it is free of that penalty.
        #
        # The window used to be derived from the trips instead: it opened for break_steps
        # steps AFTER any trip long enough to carry an internal Lenkzeitpause. Two things
        # were wrong with that. It sat outside the occupancy the break is inside - 2.6b
        # adds the 45 minutes to the trip's own duration - so the same rest was charged
        # once as standing time and granted again afterwards as free charging time, two
        # disjoint placements of one break. And it keyed on trips_duration_steps, the
        # occupancy, where the legal trigger is driving; the two coincide today only
        # because a break is never added below the threshold.
        #
        # 3.3.17(c-i) now places rests explicitly, so the waiver is simply those steps: the
        # truck is standing because the law says it must, and it may spend the time on a
        # charger. break_cover_model is empty when the crew rules are not built, which is
        # the right answer for an autonomous fleet - no driver, no rest, no waiver. That is
        # also a correction: this block is outside 3.3.17 and used to hand the same free
        # window to a driverless truck.
        for m in bev_vehicles:
            for t in time_steps:
                resting = break_cover_model.get((m, t), 0.0)
                model.addLConstr(penalized_external[m, t] <= x_m_t_l[m, t, 'external_charging'])
                model.addLConstr(penalized_external[m, t] <= 1 - resting)
                model.addLConstr(
                    penalized_external[m, t]
                    >= x_m_t_l[m, t, 'external_charging'] - resting)
        external_time_penalty = gp.quicksum(
            penalty_charging_external_time * STEP_MINUTES * penalized_external[m, t]
            for m in bev_vehicles for t in time_steps)
    # v43: penalty per occupied slot - internal CHG virtual trips (station use), external x
    # (if any) and V2G virtual trips, all at the same rate.
    # The V2G term matters beyond its size: without it an assignment that discharges
    # nothing is free, so the solver leaves such binaries set wherever branch-and-bound
    # happened to land. Those stray assignments cost nothing and change no energy, but they
    # occupy the vehicle in constraint 3.3.4 and make the raw z's useless for reading the
    # schedule off. Pricing the slot the same as a charging slot removes them.
    chg_penalty_terms = []
    for m in vehicles:
        for t in time_steps:
            chg_penalty_terms.append( z_m_f_s[m, f'CHG_t{t}', t] )
            vtg = f'V2G_t{t}'
            if vtg in v2g_virtual_trips:
                chg_penalty_terms.append( z_m_f_s[m, vtg, t] )
            if 'external_charging' in locations:
                chg_penalty_terms.append( x_m_t_l[m, t, 'external_charging'] )
    charging_penalty = penalty_charging_use * gp.quicksum(chg_penalty_terms)

    # 3.3.10d one charge per plug-in, not one per half hour (penalty_charging_block, 1.5)
    #
    # The slot charge above counts steps and cannot see how they are arranged: three
    # scattered half hours and three consecutive ones cost it the same €3. Nothing else
    # separates them either, because depot charging is priced per kWh at that step's rate
    # and the price curve holds one value across 28 steps of the day - so inside a price
    # window every arrangement is an alternative optimum of exactly equal cost and the
    # solver keeps whichever it reached first. That is what a scattered night is.
    #
    # A block here is a CONNECTED PERIOD - one visit to a charger, from plugging in to
    # pulling away - and what is counted is how many of them a truck makes:
    #
    #     connected[m,t] = chg[m,t] + v2g[m,t]      (3.3.4: at most one event a step, so
    #                                                this is an indicator, not a count)
    #     start[m,t] >= connected[m,t] - connected[m,t-1]     (>= connected[m,0] at step 0)
    #
    # Both halves of that were got wrong first, and both were caught by measuring rather
    # than by reading, so they are written down here:
    #
    #   * Counting charge starts against a CHARGING predecessor - start >= chg[t] - chg[t-1]
    #     - makes every charge that resumes after a discharge a new block. That is the
    #     arbitrage round trip, buy then sell then buy, which is the one pattern in this
    #     model that earns money. It became an anti-V2G penalty wearing a tidiness label:
    #     on day 93 at 10 EUR a block the operating cost rose 1018 -> 1793 EUR (+76 %) and
    #     the gap went from a converged 8.3 % to 21.6 % against a 15 % target.
    #   * Counting charge starts against a CONNECTED predecessor fixes that and introduces
    #     a worse fault, because a connected period that opens with a discharge is then
    #     never counted at all. The model found it immediately: at 50 EUR a block the
    #     counted figure fell 9 -> 3 while the number of times a truck actually pulled up to
    #     a charger rose 12 -> 32. The penalty improved its own metric by making the
    #     behaviour it stood for nearly three times worse.
    #
    # Counting the connected period itself is the form with nothing left to game: every
    # arrival is one start, whatever the truck does once it is plugged in, and the term
    # cannot be avoided by reordering charge and discharge inside a visit.
    #
    # A lower bound is enough and an equality would be wasted: the term is minimised and
    # the coefficient is positive, so the solver pushes every start down to the bound, and
    # with chg binary the bound is integral - which is why these can be continuous and cost
    # no branching. One variable and one row per (bev, step); nothing at all when the
    # penalty is zero, so switching it off restores the old model exactly rather than
    # leaving a layer behind that happens to be free.
    #
    # Depot charging only: public charging already pays penalty_charging_external_time per
    # minute, which dwarfs this. What is counted is the number of times a truck arrives at a
    # charger, not what it does once it is there.
    # the parameter shadows the module global of the same name, so None - a caller that
    # predates this term - has to reach past it to section 1.5 rather than read itself
    block_price = float(penalty_charging_block if penalty_charging_block is not None
                        else globals()['penalty_charging_block'])
    charging_block_penalty = 0.0
    chg_block_start = {}
    if block_price:
        for m in bev_vehicles:
            connected_before = None
            for t in time_steps:
                # on the cable either way: charging or selling back, and 3.3.4 makes the
                # sum an indicator rather than a count
                discharging_now = z_m_f_s.get((m, f'V2G_t{t}', t))
                connected_now = z_m_f_s[m, f'CHG_t{t}', t]
                if discharging_now is not None:
                    connected_now = connected_now + discharging_now
                start = model.addVar(lb=0.0, name=f"chg_block_start{tag}[{m},{t}]")
                model.addLConstr(start >= (connected_now if connected_before is None
                                           else connected_now - connected_before))
                chg_block_start[m, t] = start
                connected_before = connected_now
        charging_block_penalty = block_price * gp.quicksum(chg_block_start.values())
    vehicle_use_penalty = penalty_vehicle_use * gp.quicksum(y_m[m] for m in vehicles)
    # actual charging energy costs (private depot vs public external) using E split + rates derived from cost_parameters_energy
    charging_energy_cost = gp.quicksum(
        depot_buy_eur_per_kWh_t[t] * E_private[m, t] + public_charging_eur_per_kwh.get(m, 0.0) * E_public[m, t]
        for m in bev_vehicles for t in time_steps)
    # ... and the correction for the part of the depot charging the site generates itself:
    # those kWh never pass the grid meter, so they are re-priced from the spot price of
    # costs_dataset.xlsx to what an own PV kWh is worth. The spot price is the same for
    # every truck but not the same all day, so the correction is per step: it has to undo
    # the price that step was actually charged at, not a daily average.
    #
    # The price it re-prices *to* is an opportunity price (1.4): what the kWh would have
    # earned had it not gone into a truck, i.e. spot less the cost of selling it. The
    # price it re-prices *from* is the buy price, spot plus the grid overhead. The spot
    # term is in both and cancels, so what this correction is worth per kWh is exactly
    # grid_energy_overhead + energy_selling_overhead, whatever the market does that day.
    pv_charging_cost = gp.quicksum(
        (pv_charging_eur_per_kWh_t[t] - depot_buy_eur_per_kWh_t[t]) * E_pv_charging[t]
        for t in time_steps)
    charging_energy_cost = charging_energy_cost + pv_charging_cost
    # truck toll (distance-based, different rates for diesel vs bev)
    toll_cost = (gp.quicksum(
        toll_rate_per_km.get(m, 0.0) * trip_distances.get(f, 0.0) * z_m_f_s[m, f, s]
        for m in vehicles for f in day_trips_list for s in event_possible_starts.get(f, [])
    )
        # an empty truck pays the same toll per kilometre as a loaded one
        + gp.quicksum(toll_rate_per_km.get(m, 0.0) * deadhead_km_total(m) for m in vehicles))
    # drivers (1.4c, 3.3.17). Two terms, and NEITHER of them is the wage.
    #
    #   heads  - a tie-break towards fewer, longer shifts instead of many short ones, on the
    #            day's peak concurrency. Priced like penalty_vehicle_use, i.e. as a device
    #            and not as a salary, and reported under steering_penalties_€ with the rest
    #            of the apparatus.
    #   breach - what it costs to push past a crew limit the model is allowed to push past
    #            (the driving ones; the shift limit is hard by default). Not money either -
    #            it is what keeps an illegal schedule from looking like a cheap one.
    #
    # The wage used to be a third term here: driver_hourly_rate_eur per hour a vehicle was
    # away. It is gone, and 1.4c has the argument. In short: at ~20 EUR/h over a fleet of
    # trucks away most of the day it was the largest number in the objective by an order of
    # magnitude, so V2G earnings and battery wear - the things this model is built to weigh
    # against each other - sat inside the MIPGap of a term that was nearly constant across
    # every schedule. What the wage was actually steering (bring the truck home) is stated
    # as the hard shift limit of 3.3.17a instead, which steers it exactly rather than by
    # price. The salary itself is rostered and reported afterwards (5.5c).
    driver_cost = 0
    if drivers_needed is not None:
        driver_cost = (penalty_driver_use * drivers_needed
                       + penalty_crew_rule_breach * crew_breach_steps)
    total_cost = driving_cost + v2g_earnings + v2v_saving + degradation_cost + peak_shaving_cost + external_time_penalty + charging_penalty + charging_block_penalty + charger_use_penalty + vehicle_use_penalty + charging_energy_cost + toll_cost + driver_cost
    if advanced_degradation_status == 'on' and max_efc is not None:
        total_cost += degradation_distribution_penalty * max_efc   # v43: encourages distribution of degradation across vehicles
    # The vehicle-id tie-break that used to be added here is gone; 3.3.10b states the
    # same intent as a constraint instead. It was a euro per id on a used vehicle, which
    # bought an ordering by paying for it in the objective it was ordering - and it had to
    # be switched off under auto_sizing, because a bias towards low ids distorts which
    # vehicles a design run selects. The constraint needs no such exemption: it says the
    # trucks are interchangeable, which is true whoever chose them.
    # A design run owns the objective: it sums this day's cost with every other day's and
    # adds the ownership term (3.4), so setting one here would throw the others away.
    if not composing:
        model.setObjective(total_cost, gp.GRB.MINIMIZE)

    return (model, E_neg, y_m, total_cost)



# 3.5 THE DECOMPOSED DESIGN RUN
#
# Same problem as 3.4, solved on the axis the problem is actually thin on.
#
# The monolith folds D days into one MILP. Measured (benchmarks/results_design_scaling.jsonl,
# Gurobi 13.0.3, 1800 s cap): D=1 converges in 304 s; D=2 misses the 10 % target; from D=3
# the solver explores *one* node and returns no feasible fleet at all. Node count falls as
# days are added - 6639, 1218, 1 - because the spatial root relaxation over thousands of
# bilinear terms costs more than the whole budget. That is not slowness, it is a wall, and
# no parameter fixes a root node that never finishes.
#
# What makes decomposition exact here rather than a heuristic is the structure 3.4 already
# documents: the SoC returns to its starting level at 24:00 (3.3.6b), so nothing carries
# across midnight and the days couple through one thing only - which trucks were bought.
# Fix the fleet and the days are independent single-day disposition problems, each the size
# this codebase has always solved comfortably.
#
# 3.5.1 why this is a lattice search and not Benders
#
#       Textbook Benders needs LP subproblems so the master can be fed duals. Fixing the
#       fleet here does not soften a day at all: it stays a non-convex MIQCP with ~4000
#       integer variables, and there are no duals to take. Logic-based Benders would leave
#       only no-good cuts ("not this fleet again"), which is weaker than enumeration.
#
#       Enumeration is affordable because the master is tiny. Symmetry breaking (3.4.2)
#       forces copies of a type to be taken in order, so a fleet is not a subset of the
#       pool but a *count per type* - four numbers, each 0..copies. That is the whole
#       decision space, and it is searched cheapest-ownership-first.
#
# 3.5.2 the two monotonicities that do the pruning
#
#       Cost is monotone non-increasing in the fleet: an extra truck can always be left
#       standing, because y_m and the energy flows are permitted by ownership and never
#       forced by it. So the operating cost of the *full pool* is a valid lower bound on
#       the operating cost of every fleet, and ownership is the only term that grows.
#       Once ownership(F) * horizon + that bound reaches the incumbent, no fleet further
#       down the ordering can win and the search stops - having proved it, not assumed it.
#
#       Feasibility is monotone the other way: if a fleet cannot serve a day, no subset of
#       it can either. A day proved infeasible therefore prunes a whole down-set.
#
#       A day that merely *times out* proves nothing and must not prune anything. The two
#       are kept apart deliberately - conflating them would silently discard fleets that
#       were fine but slow, which is exactly the kind of error that would not show up in
#       the answer.
#
# 3.5.3 what it certifies
#
#       Every fleet reported on has been costed by solving every one of its days, at the
#       same optimization_MIPGap the monolith would have used. So the answer is not an
#       estimate: it is a real schedule for every day. What differs from 3.4 is the shape
#       of the guarantee - the monolith returns one Gurobi gap over the whole horizon,
#       this returns a per-day gap on the winning fleet plus whether the fleet search
#       itself was exhaustive. Both are reported rather than blended into one number.


def _design_fleet_ids(counts, by_type):
    """The vehicle ids a count vector names.

    Symmetry breaking takes copies of a type in order (3.4.2), so owning k of a type means
    owning its first k ids: every other choice of k is a relabelling of this one.
    """
    ids = []
    for type_index in sorted(counts):
        ids.extend(by_type[type_index][:counts[type_index]])
    return ids


# 3.5.0a the objective every day of a design run is solved against
#
# A design run sizes a fleet, so the roster is its answer and not its input - which is the
# case auto_sizing describes (1.3), and which used to matter here because the vehicle-id
# tie-break had to be taken out of the objective for it: a bias towards low ids distorts
# which vehicles the run selects rather than merely tidying a plot.
#
# That exemption was itself buggy for a long time - _design_day_cost, _design_day_floor and
# the final re-solve all build from the build_arguments run_optimization assembles, which
# carry the *module's* auto_sizing ('off' by default), so every candidate was costed with
# the tie-break in while the same days were reported through postprocess with
# auto_sizing='on' hardcoded (3.5.7). The search compared candidates under one objective
# and the run accounted for the winner under another; worse, the pool hands out ids in type
# order, so the first type listed in synthetic_fleet always drew the cheapest ids and a run
# could prefer a type for where it sits in a spreadsheet.
#
# None of that can happen now. The tie-break is gone and 3.3.10b states the ordering as a
# constraint between vehicles the model cannot tell apart, which is a fact about the fleet
# and not a price - so it needs no exemption and cannot tilt a design run towards a type.
# The auto_sizing plumbing below stays because the flag still decides other things.
#
# Applied to the build arguments rather than to the module global on purpose.
# build_runtime_context() also reads auto_sizing - it decides which sheet of
# fleet_dataset.xlsx is the roster - and a design worker calls it; flipping the global
# would have sent every worker to the workbook's first sheet instead of to
# 'existing_fleet'. Nothing in a design run reads that roster (every solve is handed a
# fleet_override), but a setting whose correctness depends on which sheet is first is not
# one to rely on.
def _design_objective_arguments(build_arguments):
    """The build arguments a design day-solve uses: auto-sizing semantics, whatever the module says."""
    build_arguments = dict(build_arguments)
    build_arguments['auto_sizing'] = 'on'
    return build_arguments


# 3.5.0 what the search actually cost
#
# The day solves are the whole expense of a design run, so the number of them is the one
# figure that says whether the search was cheap or ruinous. It used to be reported as
# `evaluated * horizon_days + horizon_days`, which is not a count but an assumption - that
# every candidate fleet was solved on every day. The implementation makes that false in
# both directions: an infeasible fleet stops on the hardest day, a dominated one stops as
# soon as the floor says it cannot win, the minimum-size probe and the full-pool floor
# solves are not candidates at all, and the final re-solve is a separate pass. On a
# three-day run with heavy pruning the formula overstated the work by a factor of two and
# on a run with floor solves it understated it.
#
# So the solves are counted where they are asked for. Counted in the *parent*, because a
# spawned worker's globals never come back: every one of them is either called here
# directly or dispatched from here and read back here.
#
# One honest gap, named rather than papered over: when a candidate is abandoned part way,
# imap_unordered may already have handed further days to workers, and those finish without
# anyone reading them. They are counted separately as `day_solves_abandoned` - an upper
# bound, since a task still queued when the iterator is dropped never runs at all.
# 'candidate' covers every day solved against a candidate fleet, whether the minimum-size
# bisection asked for it or the lattice search did - both go through _design_evaluate, and
# both cost exactly one solve.
_design_solves = {'candidate': 0, 'floor': 0, 'final': 0, 'abandoned': 0}


def _design_solves_reset():
    for key in _design_solves:
        _design_solves[key] = 0


# 3.5.0b infeasible, or unbounded, and the difference is not cosmetic
#
# A day that comes back 'infeasible' prunes a whole down-set (3.5.2): if this fleet cannot
# serve the day, no subset of it can, so every smaller fleet is discarded unsolved. That is
# the search's sharpest tool and it is only sound if the day really was infeasible.
#
# GRB.INF_OR_UNBD is Gurobi saying it does not know which. Presolve found the model has no
# optimum but could not separate "no feasible point" from "objective runs to -inf", and it
# returns the joint status rather than guess. Reading it as infeasible - which this did -
# takes the one answer that licenses pruning and gives it to the one case that must never
# prune: an unbounded objective here is not a property of the fleet, it is a sign error in
# the model. Anything that pays to discharge without limit unbounds every fleet equally, so
# the search would discard the down-set of every candidate in turn, run to the end of the
# lattice and report a fleet - confidently, and on a model that was broken before it
# started.
#
# So the ambiguity is resolved instead of assumed. DualReductions = 0 switches off the
# presolve reductions that cost Gurobi the distinction; the second solve is slower and is
# charged only in the case that actually arrives here, which on a sound model is never.
# Unbounded then raises, because there is nothing for a design run to do with it.
def _design_solve_outcome(model, what):
    """'feasible' | 'infeasible' | 'unknown' for a solve that produced no incumbent.

    Raises when the model turns out to be unbounded: that is a defect in the formulation
    and not a fact about the fleet under test.
    """
    status = model.Status
    if status == gp.GRB.INFEASIBLE:
        return 'infeasible'
    if status == gp.GRB.UNBOUNDED:
        raise ValueError(
            f"the design model is UNBOUNDED on {what}: the objective can be driven to minus "
            f"infinity, so there is no cheapest schedule to find. This is a defect in the "
            f"formulation rather than anything about the fleet or the day - a sign error in "
            f"the V2G pricing or in an earnings term is the usual cause. The search cannot "
            f"continue: an unbounded model is unbounded for every candidate, so nothing it "
            f"reported would mean anything.")
    if status != gp.GRB.INF_OR_UNBD:
        return 'unknown'
    # Gurobi could not tell the two apart. Ask again without the reductions that cost it
    # the distinction, rather than picking the reading that happens to prune.
    model.setParam('DualReductions', 0)
    model.optimize()
    if model.Status == gp.GRB.INFEASIBLE:
        return 'infeasible'
    if model.Status == gp.GRB.UNBOUNDED:
        raise ValueError(
            f"the design model is UNBOUNDED on {what} (established with DualReductions=0 "
            f"after Gurobi first reported INF_OR_UNBD). The objective can be driven to minus "
            f"infinity, which is a defect in the formulation rather than anything about the "
            f"fleet - a sign error in the V2G pricing or in an earnings term is the usual "
            f"cause. The search cannot continue.")
    if model.SolCount > 0:
        return 'feasible'
    # still undecided, or the re-solve hit the clock. Proves nothing, prunes nothing.
    return 'unknown'


def _design_day_cost(day_index, date_text, fleet_frame, params_for_day, time_limit):
    """Solve one day of the range against one fixed fleet.

    Returns {'status': 'ok'|'infeasible'|'unknown', ...}. 'infeasible' means Gurobi proved
    this fleet cannot serve this day and is safe to prune subsets on; 'unknown' means the
    cap ran out with no incumbent and proves nothing at all. See 3.5.2, and 3.5.0b for why
    INF_OR_UNBD is not read as either without asking again.
    """
    model = None
    try:
        with model_parameters(optimization_time_limit_s=time_limit):
            build_arguments, context = run_optimization(
                params_for_day[day_index], fleet_override=fleet_frame,
                day_curves=day_curves_for(date_text), build_only=True)
            model, _E_neg, y_m, _day_cost = model_build(
                **_design_objective_arguments(build_arguments))
            solve_model(model, optimization_MIPGap, gurobi_threads)
            if model.SolCount == 0:
                outcome = _design_solve_outcome(
                    model, f"day {day_index} against a candidate fleet of "
                           f"{len(fleet_frame)} vehicle(s)")
                if outcome != 'feasible':
                    return {'status': outcome, 'cost': None, 'used': None, 'gap': None}
            return {'status': 'ok', 'cost': float(model.ObjVal), 'gap': float(model.MIPGap),
                    'used': [m for m in context['vehicles'] if y_m[m].X >= 0.5]}
    finally:
        if model is not None:
            model.dispose()


def _design_day_floor(day_index, date_text, pool, params_for_day, time_limit):
    """A lower bound on one day's cost under ANY fleet drawn from the pool.

    The pool is a superset of every candidate and cost is monotone non-increasing in the
    fleet (3.5.2), so the pool's optimum is a lower bound on every candidate's cost - and
    Gurobi's dual bound on the pool model is a lower bound on that optimum.

    Returns (dual_bound, used_vehicle_ids). dual_bound is None when the solve produced no
    usable one, in which case the caller simply searches without a floor; used is None when
    it found no incumbent.
    """
    model = None
    try:
        with model_parameters(optimization_time_limit_s=time_limit):
            build_arguments, context = run_optimization(
                params_for_day[day_index], fleet_override=pool,
                day_curves=day_curves_for(date_text), build_only=True)
            # the same objective the candidates are costed against (3.5.0a), or the floor
            # would be a bound on a different problem than the one it prunes
            model, _E_neg, y_m, _day_cost = model_build(
                **_design_objective_arguments(build_arguments))
            solve_model(model, optimization_MIPGap, gurobi_threads)
            # 3.5.0b again: an unbounded pool model raises there rather than being read as
            # a pool that cannot serve the day, which is the message this used to give.
            if model.SolCount == 0 and _design_solve_outcome(
                    model, f"day {day_index} against the full pool") == 'infeasible':
                raise ValueError(
                    f"the design model found no feasible fleet: the full pool cannot serve "
                    f"day {day_index}. Widen the pool (fleet_pool_copies_for), relax the "
                    f"crew rules, or check that the days can be served at all.")
            bound = float(model.ObjBound)
            bound = None if bound in (float('-inf'), float('inf')) else bound
            # the incumbent is not a bound on anything (3.5.2 runs the other way), but the
            # vehicles it *used* name a fleet that demonstrably serves this day, which is
            # worth having: the union over days is a feasible starting incumbent.
            used = ([m for m in context['vehicles'] if y_m[m].X >= 0.5]
                    if model.SolCount else None)
            return bound, used
    finally:
        if model is not None:
            model.dispose()


def _design_capability_order(pool, type_of, by_type, type_order):
    """The pool's vehicle types, strongest first, when one order dominates for feasibility.

    Whether a *set* of trips can be driven at all does not depend on price or on which type
    is cheaper - it depends on whether a vehicle can cover the distances and be back in
    time. In this model that reduces to two things, and only for battery trucks:

      range           storage / consumption. A diesel has no SoC balance, no charging cap
                      and no day-boundary level in the model at all (3.3.6, 3.3.11 and
                      3.3.5 are all written `for m in bev_vehicles`), so on the energy axis
                      it is unconstrained - it dominates every battery truck outright.
      charging power  how fast the energy comes back between trips.

    A type dominates another when it is at least as good on both. Returns the types sorted
    strongest-first, or None when no total order exists - two types where each beats the
    other on one axis cannot be ranked, and the caller must then do without this.
    """
    capability = {}
    for type_index in type_order:
        row = pool[pool['vehicle_id'] == by_type[type_index][0]].iloc[0]
        if str(row['vehicle_type']).strip().lower() != 'bev':
            capability[type_index] = (float('inf'), float('inf'))
            continue
        consumption = float(row['vehicle_consumption'])
        reach_km = (float(row['vehicle_energy_storage']) / consumption * 100.0
                    if consumption > 0 else float('inf'))
        capability[type_index] = (reach_km, float(row['vehicle_charging_power']))
    ranked = sorted(type_order, key=lambda t: capability[t], reverse=True)
    for stronger, weaker in zip(ranked, ranked[1:]):
        if not all(a >= b for a, b in zip(capability[stronger], capability[weaker])):
            return None            # not a total order: no dominating fleet can be built
    return ranked


def _design_strongest_counts(size, ranked, by_type):
    """The most capable fleet of exactly `size` vehicles the pool can supply.

    Takes vehicles in descending capability until the size is reached. By the dominance in
    _design_capability_order this fleet is at least as capable, vehicle for vehicle, as any
    other fleet of the same size - so if *it* cannot serve a day, no fleet of that size can.
    """
    counts = {t: 0 for t in by_type}
    left = size
    for type_index in ranked:
        take = min(left, len(by_type[type_index]))
        counts[type_index] = take
        left -= take
        if left <= 0:
            break
    return (None if left > 0 else counts)


def _design_min_fleet_size(ranked, by_type, pool, day_order, params_for_day, time_limit,
                           floor, ceiling, report=None):
    """The smallest number of vehicles that can serve every day, found by bisection.

    Feasibility is monotone in fleet size once the strongest fleet of each size is used:
    if the best k vehicles cannot do it, neither can any other k, and if they can, so can
    the best k+1. That makes it a sorted predicate, so bisection finds the boundary in
    about log2(pool) solves.

    This matters more than it looks. Searching cheapest-ownership-first means the search
    meets the small fleets first, and nearly all of them are infeasible: on the measured
    seven-day run 58 of 60 evaluations were infeasibility proofs and only two fleets were
    ever costed. Establishing the boundary once turns all of that into arithmetic.

    Every probe that succeeds is a fully costed feasible fleet, so they are handed back
    rather than thrown away: they are the search's first incumbents and its first superset
    bounds, and they cost nothing extra because the solves have already been paid for.

    Returns (size, evaluations_used, costed, conclusive). costed is [(counts, operating),
    ...] for every probe that came back feasible. conclusive is False when a probe ran out
    of time: the bisection then stops early and `size` is whatever had been proved so far,
    which is *not* the same as the range being infeasible. Only `size is None` together
    with conclusive True means no fleet in the pool can serve the range.
    """
    used = 0
    costed = []
    low, high, best = floor, ceiling, None
    while low <= high:
        middle = (low + high) // 2
        counts = _design_strongest_counts(middle, ranked, by_type)
        if counts is None:
            low = middle + 1
            continue
        outcome = _design_evaluate(counts, by_type, pool, day_order, params_for_day,
                                   time_limit)
        used += 1
        if report is not None:
            report(middle, outcome['status'])
        if outcome['status'] == 'ok':
            costed.append((dict(counts), outcome['operating']))
            best = middle
            high = middle - 1
        elif outcome['status'] == 'infeasible':
            low = middle + 1
        else:
            # unknown: this size is neither proved nor excluded, so the bisection cannot
            # continue safely in either direction. Stop, and say that it stopped - a caller
            # that read this as "nothing was feasible" would report an infeasible range on
            # the strength of a time limit.
            return (best, used, costed, False)
    return (best, used, costed, True)


# 3.5.8 solving one candidate's days at the same time
#
# At a fixed fleet the days are independent - that is the whole basis of 3.5 - so the D
# solves a candidate costs can run at once. They are also the entire runtime: a candidate
# cost about 22 s on the three-day range and essentially all of it is day-solves.
#
# What this cannot do is inherit the parent's state. The design run configures itself by
# assigning module globals (charging_infrastructure, the thinned all_trips a benchmark may
# have installed, auto_sizing) and a Pool worker starts from a *fresh import* of this
# module under 'spawn', so it would silently plan against the depot and the trip set of the
# defaults rather than of the run. Every one of those is therefore passed explicitly and
# re-established inside the worker. That is the whole of the B3 problem: globals are fine
# as a calling convention until something runs in another process.
#
# Two things are deliberately kept:
#
#   the hardest day stays sequential.  day_order is hardest-first precisely so an
#       unworkable fleet is rejected after one solve instead of D. Handing all D days to
#       the pool at once would throw that away and cost D solves for every infeasible
#       candidate - and most candidates are infeasible. So day one is solved alone, and the
#       rest only if it survived. D solves become 1 + one parallel round.
#
#   threads are divided, not multiplied.  Gurobi is given every core by default; D workers
#       each doing that would oversubscribe the machine and be slower than serial. Each
#       worker gets its share. This does change what each day-solve returns - thread count
#       changes the search path, and this study measured 2/4/8/16 threads giving different
#       node counts on the same model - so a parallel run may settle on a different, equally
#       valid schedule inside the same MIPGap. The fleet it picks should not change; that is
#       checked rather than assumed.
def _design_config_snapshot():
    """Every configuration global a worker has to be given, and nothing derived.

    A worker re-imports this module, so anything the caller changed reverts to the value in
    the file: optimization_MIPGap above all, but equally the penalties, the feature
    switches and the degradation settings. The web interface changes several of them on
    every run (it execs this source and assigns onto that namespace), so without this the
    parent would solve one day of a range at the requested gap and the workers would solve
    the rest at the default - a difference that shows up nowhere in the result.

    What must not be copied is anything build_runtime_context() derives, because the worker
    calls it: handing it a stale all_trips or a stale price curve would either be
    overwritten or, worse, kept. That list is read off the function's own `global`
    statement rather than repeated here, so the two cannot drift apart.

    Only plain values travel. A DataFrame or a gurobipy handle is either derived state or
    not picklable, and in both cases has no business being copied this way.
    """
    import re
    import inspect
    derived = set()
    for match in re.finditer(r'^\s*global\s+(.+)$',
                             inspect.getsource(build_runtime_context), re.M):
        derived.update(name.strip() for name in match.group(1).split(','))

    def plain(value):
        if isinstance(value, (bool, int, float, str, type(None))):
            return True
        if isinstance(value, (list, tuple)):
            return all(plain(item) for item in value)
        if isinstance(value, dict):
            return all(isinstance(k, (str, int)) and plain(v) for k, v in value.items())
        return False

    return {name: value for name, value in globals().items()
            if not name.startswith('_') and name not in derived and plain(value)}


# set by _design_worker_init in each pool worker. Defined here as well so a task that
# somehow reaches an uninitialised process fails saying so, rather than with a bare
# NameError from inside a solve - and so the name is visible to anything reading this file.
_design_worker_state = None

_design_workers = {'pool': None, 'size': 0, 'day_task': None,
                   'floor_task': None, 'failure': None}


def _design_worker_init(state):
    """Make a pool worker into a copy of the parent's design run.

    Called once per worker. Everything here is something the parent set on a module global
    that a fresh import would not have.
    """
    import io
    import contextlib
    global charging_infrastructure, charging_station_ids, run_kind
    global all_trips, gurobi_threads, _design_worker_state
    # the caller's configuration first: build_runtime_context() reads several of these
    # (order_data_days, the date, the scenario switches) to derive what it derives
    for name, value in state['config'].items():
        globals()[name] = value
    # a worker of a design run is doing sizing, whatever the config it was handed says -
    # which is what keeps it from writing figures and from pricing off the day sheet (1.4b1)
    run_kind = 'sizing'
    with contextlib.redirect_stdout(io.StringIO()):
        build_runtime_context()
    # after the context, not before: build_runtime_context() rebuilds all_trips from
    # trips.csv and would overwrite a thinned set the caller installed
    all_trips = state['all_trips']
    charging_infrastructure = state['charging_infrastructure']
    charging_station_ids = state['charging_station_ids']
    gurobi_threads = state['gurobi_threads']
    # the snapshot is deliberately generic, so it carries design_parallel_days along with
    # everything else. A worker has no business starting a pool inside a pool - it only
    # ever runs day tasks - so the setting is pinned off here rather than trusted not to
    # be reached.
    global design_parallel_days
    design_parallel_days = 1
    _design_worker_state = state


def _design_day_task(task):
    """One (day, fleet) solve inside a worker. Returns _design_day_cost's outcome."""
    day_index, date_text, fleet_ids, time_limit = task
    state = _design_worker_state
    if state is None:
        raise RuntimeError(
            "a design day-task reached a process that was never initialised - "
            "_design_worker_init did not run in this worker")
    pool = state['pool']
    frame = pool[pool['vehicle_id'].isin(fleet_ids)].reset_index(drop=True)
    params = {day_index: (state['scenario'], state['year'], state['v2g'], day_index)}
    outcome = _design_day_cost(day_index, date_text, frame, params, time_limit)
    return (day_index, outcome)


def _design_floor_task(task):
    """One full-pool day bound inside a worker (3.5.4).

    The floor is D independent solves with exactly the same independence as the candidate
    day-solves, and leaving it sequential was costing most of the benefit: on a seven-day
    range it was up to 87 % of the wall clock, which capped the whole run's speed-up at
    about 1.1x however well the search itself parallelised.
    """
    day_index, date_text, time_limit = task
    state = _design_worker_state
    if state is None:
        raise RuntimeError(
            "a design day-task reached a process that was never initialised - "
            "_design_worker_init did not run in this worker")
    params = {day_index: (state['scenario'], state['year'], state['v2g'], day_index)}
    bound, used = _design_day_floor(day_index, date_text, state['pool'], params, time_limit)
    return (day_index, bound, used)


def _design_start_workers(state, wanted):
    """Bring up the day-solve pool, or return None when it is not worth one.

    The functions handed to the pool are taken from the *importable* module rather than
    from whatever namespace this code is executing in. The web interface does not import
    this file - it execs it under the name 'hdv_opt_web', which is not in sys.modules - and
    'spawn' pickles an initializer by module-and-name, so a worker would try to import
    'hdv_opt_web' and fail. It failed quietly, too: the exception was caught below and the
    run fell back to sequential with a note on a stream the interface discards, so
    parallelism simply never happened in the application while working in every test that
    imported the module normally.

    Resolving through sys.modules fixes that and is also what makes the configuration
    snapshot meaningful: the child configures the real module, from the parent's values.
    """
    if wanted < 2:
        return None
    try:
        import io
        import importlib
        import contextlib
        # Importing is how a function becomes picklable by reference, and when this file is
        # exec'd rather than imported (the web interface) that import happens here, once per
        # process. It runs this module's top level - the derived-input check, the runtime
        # context, the trips fingerprint warning - all against file defaults and all of it
        # noise the caller did not ask for at this moment. Silenced rather than skipped:
        # the import itself is what makes the pool possible, and a second call is a
        # sys.modules lookup.
        with contextlib.redirect_stdout(io.StringIO()):
            home = importlib.import_module(
                __name__ if __name__ in sys.modules else 'hdv_disposition_optimization')
        pool = multiprocessing.get_context('spawn').Pool(
            processes=wanted, initializer=home._design_worker_init, initargs=(state,))
    except Exception as exc:                     # a pool that will not start is not fatal
        # recorded, not just printed: the note goes to a stream the web interface discards,
        # and parallel_day_workers = 0 on its own cannot be told from "none were asked
        # for". A run that wanted workers and did not get them should say which.
        _design_workers['failure'] = f"{type(exc).__name__}: {exc}"
        print(f"note: day-solves stay sequential ({type(exc).__name__}: {exc})", flush=True)
        return None
    _design_workers['pool'] = pool
    _design_workers['size'] = wanted
    _design_workers['day_task'] = home._design_day_task
    _design_workers['floor_task'] = home._design_floor_task
    return pool


def _design_stop_workers():
    pool = _design_workers.get('pool')
    if pool is not None:
        pool.terminate()
        pool.join()
    _design_workers['pool'] = None
    _design_workers['size'] = 0
    _design_workers['day_task'] = None
    _design_workers['floor_task'] = None


def _design_evaluate(counts, by_type, pool, day_order, params_for_day, time_limit,
                     day_floor=None, abort_above=None):
    """Operating cost of the whole range under one candidate fleet.

    day_order is hardest-day-first, so a fleet that cannot cope is rejected on its worst
    day instead of after paying for all the easy ones.

    abort_above stops the evaluation the moment this fleet can no longer win. Ownership is
    only 47-130 EUR per truck per day against operating costs of order 1000 EUR per day, so
    the ownership cutoff alone barely prunes - what actually decides a candidate is its
    operating cost, and that is only known by solving. day_floor (the cost of each day
    under the *full* pool, a valid lower bound on that day under any fleet, 3.5.2) lets the
    days already solved be added to a floor on the days not yet solved, so a hopeless fleet
    is dropped after one or two day-solves instead of all of them.
    """
    ids = _design_fleet_ids(counts, by_type)
    frame = pool[pool['vehicle_id'].isin(ids)].reset_index(drop=True)
    operating = 0.0
    per_day = {}

    workers = _design_workers.get('pool')
    if workers is not None and len(day_order) > 1:
        # the hardest day first and alone, so an unworkable fleet still costs one solve
        first_day, first_date = day_order[0]
        _design_solves['candidate'] += 1
        first = _design_day_cost(first_day, first_date, frame, params_for_day, time_limit)
        if first['status'] != 'ok':
            return {'status': first['status'], 'operating': None, 'per_day': {},
                    'failed_day': first_day}
        per_day[first_day] = first
        operating = first['cost']
        if abort_above is not None and day_floor is not None:
            remaining = sum(day_floor.get(later, 0.0) for later, _d in day_order[1:])
            if operating + remaining >= abort_above:
                return {'status': 'dominated', 'operating': None, 'per_day': per_day,
                        'failed_day': None}
        tasks = [(day, date, ids, time_limit) for day, date in day_order[1:]]
        pending = {day for day, _d in day_order[1:]}
        # imap_unordered, not map: results are taken as they land so the fleet can still be
        # abandoned part way. map() waits for the whole round, which threw away the abort
        # the sequential path has after every day and made a hopeless candidate cost all of
        # its remaining days. The solves already dispatched do finish - the pool is not
        # interrupted - but nothing waits for them and the next candidate starts sooner.
        for day_index, outcome in workers.imap_unordered(_design_workers['day_task'], tasks):
            pending.discard(day_index)
            _design_solves['candidate'] += 1
            if outcome['status'] != 'ok':
                _design_solves['abandoned'] += len(pending)
                return {'status': outcome['status'], 'operating': None,
                        'per_day': per_day, 'failed_day': day_index}
            per_day[day_index] = outcome
            operating += outcome['cost']
            if abort_above is not None and day_floor is not None:
                still_to_come = sum(day_floor.get(day, 0.0) for day in pending)
                if operating + still_to_come >= abort_above:
                    _design_solves['abandoned'] += len(pending)
                    return {'status': 'dominated', 'operating': None,
                            'per_day': per_day, 'failed_day': None}
        return {'status': 'ok', 'operating': operating, 'per_day': per_day,
                'failed_day': None}

    for position, (day_index, date_text) in enumerate(day_order):
        _design_solves['candidate'] += 1
        outcome = _design_day_cost(day_index, date_text, frame, params_for_day, time_limit)
        if outcome['status'] != 'ok':
            return {'status': outcome['status'], 'operating': None, 'per_day': per_day,
                    'failed_day': day_index}
        operating += outcome['cost']
        per_day[day_index] = outcome
        if abort_above is not None and day_floor is not None:
            remaining = sum(day_floor.get(later, 0.0)
                            for later, _d in day_order[position + 1:])
            if operating + remaining >= abort_above:
                return {'status': 'dominated', 'operating': None, 'per_day': per_day,
                        'failed_day': None}
    return {'status': 'ok', 'operating': operating, 'per_day': per_day, 'failed_day': None}


def run_design_optimization(day_specs, scenario_iteration, year_iteration,
                            v2g_status_iteration, pool_copies=None,
                            show_progress=True, progress=None):
    """Size one fleet against several days, then report each day against it.

    progress, if given, is called as progress(phase, done, total) whenever the run moves
    on - phase being 'bound', 'search' or 'solve' (3.5.4, 3.5.5, 3.5.7). It exists for the
    web interface, which has one line to say where a run of several minutes has got to;
    show_progress is the separate switch for the terminal narrative. The search phase
    passes total=None on purpose: how many fleets it costs is the answer rather than an
    input, which is the same reason day_solves is counted and not inferred (3.5.0).

    day_specs  [(day_index, 'DD.MM.YYYY'), ...] - the days as run_optimization numbers them
               (1-based inside order_data_days) with the calendar date each one falls on.

    Returns (design, day_results). design carries the fleet to buy and the charging
    infrastructure the days turned out to need; day_results is one postprocess per day.
    """
    if not day_specs:
        raise ValueError("a design run needs at least one day")

    # one stamp for the range, not one per day: the answer is a fleet, and its per-day
    # figures belong to that one answer however many hours they took to produce (1.0b)
    begin_output_run()

    day_frames = [all_trips[all_trips['day_ID'] == day] for day, _date in day_specs]
    # `pool_copies or ...` read an explicit 0 as "not given" and silently replaced it with
    # the derived cap, and it let a negative or fractional value through to load_fleet_pool
    # where range(int(copies)) turns it into an empty pool and a far less obvious error.
    # None is the only thing that means "decide for me"; anything else is an instruction and
    # is checked as one.
    if pool_copies is None:
        copies = fleet_pool_copies_for(day_frames)
    else:
        if isinstance(pool_copies, bool) or not isinstance(pool_copies, int):
            raise ValueError(
                f"pool_copies has to be a whole number of copies per vehicle type or None "
                f"to derive it from the work, got {pool_copies!r}.")
        if pool_copies < 1:
            raise ValueError(
                f"pool_copies is {pool_copies}; the design run needs at least one copy of "
                f"each type to have anything to choose from. Pass None to size the pool "
                f"from the days themselves (fleet_pool_copies_for).")
        copies = int(pool_copies)

    # the pool and the depot it charges against are premises of the design run, not of the
    # fleet under test: a candidate is judged against the same depot the monolith offered
    # it, so a small fleet is never flattered by a smaller depot. Both are built from the
    # full pool exactly as 3.4.0 builds them.
    probe_pool, _probe_types = load_fleet_pool(fleet_input_file, copies=1)
    bev_types = probe_pool[probe_pool['vehicle_type'] == 'bev']
    pool, type_of = load_fleet_pool(fleet_input_file, copies=copies)
    params_for_day = {day: (scenario_iteration, year_iteration, v2g_status_iteration, day)
                      for day, _date in day_specs}

    bev_candidates = int((pool['vehicle_type'] == 'bev').sum())
    design_station_kW = (float(bev_types['vehicle_charging_power'].max())
                         if len(bev_types) else 0.0)
    design_infrastructure = [design_station_kW] * max(bev_candidates, 1)
    design_station_ids = [str(i + 1) for i in range(len(design_infrastructure))]

    by_type = {}
    for vehicle_id in pool['vehicle_id'].tolist():
        by_type.setdefault(type_of[vehicle_id], []).append(vehicle_id)
    type_order = sorted(by_type)
    price_of = dict(zip(pool['vehicle_id'], pool['vehicle_price']))
    horizon_days = len(day_specs)

    def ownership_per_day_of(counts):
        return sum(daily_ownership_cost(price_of[by_type[t][i]])
                   for t in type_order for i in range(counts[t]))

    # hardest day first: an unworkable fleet should fail on the day most likely to break it
    trips_on = {day: len(all_trips[all_trips['day_ID'] == day]) for day, _d in day_specs}
    day_order = sorted(day_specs, key=lambda spec: -trips_on[spec[0]])

    # A floor on the fleet, so candidates that cannot serve the range at all are skipped
    # without spending a solve to discover it. It has to be a *sound* floor: this prunes
    # candidates outright, so a floor that is one too high silently discards the optimum.
    #
    # fleet_pool_copies_for() is the wrong source for it. That divides the day's driving by
    # the *working hours*, which is right for sizing a pool with headroom on top but is not
    # a valid lower bound here: a trip that does not fit inside the working hours keeps its
    # own window instead (section 2.6, trips_outside_work_hours), so a truck may legally be
    # occupied for longer than the window and the true minimum can be smaller than that
    # quotient. Dividing by the whole day instead is the loosest bound that is always true.
    #
    # Both approximations lean the safe way. trip_duration_h is the routed occupancy without
    # the Lenkzeitpause that trips_duration_steps adds, so the numerator is understated; the
    # denominator is the longest any one vehicle could possibly be busy. Both make the floor
    # smaller, and a floor that is too small only costs solves.
    day_length_h = len(time_steps) * STEP_HOURS
    busiest_occupancy_h = max(
        (float(pd.to_numeric(frame['trip_duration_h'], errors='coerce').fillna(0.0).sum())
         for frame in day_frames), default=0.0)
    min_vehicles = max(1, math.ceil(busiest_occupancy_h / day_length_h))


    evaluated = 0
    parallel_workers_used = 0
    parallel_workers_wanted = 0
    parallel_start_failure = None
    search_exhausted = False
    search_limit_hit = None
    _design_solves_reset()

    def _tick(phase, done, total=None):
        """Tell the caller where the run is, if it asked (progress= above)."""
        if progress is not None:
            try:
                progress(phase, done, total)
            except Exception:
                pass          # a reporting callback must never end a design run
    search_started = time.time()
    proven_infeasible = []
    timed_out = []
    # the depot this run charges against and the figures it does not draw, for the length
    # of the run and no longer (1.0c). model_build and postprocess read all three from the
    # module, which is why they are set rather than passed.
    with model_parameters(charging_infrastructure=design_infrastructure,
                          charging_station_ids=design_station_ids,
                          run_kind='sizing'):
        try:
            if show_progress:
                print(f"design run (decomposed): {horizon_days} day(s), pool of {len(pool)} "
                      f"candidates, {len(type_order)} type(s) x 0..{copies}; charging against "
                      f"{len(design_infrastructure)} x {design_station_kW:g} kW", flush=True)

            # 3.5.8 the day-solve pool. Started here because the globals it has to copy are
            #       only final now, and torn down in the finally below whatever happens.
            # sized by the number of days, not days-1: the search parallelises D-1 of them
            # (the hardest stays sequential so an unworkable fleet still costs one solve), but
            # the floor phase has D independent tasks and it was the larger cost of the two.
            # A manual setting is clamped the same way - 64 workers on an 8-core machine is
            # 64 Gurobi processes with one thread each, which is slower than not bothering.
            room = min(max(1, (os.cpu_count() or 2) - 1), parallel_worker_limit)
            if design_parallel_days == 'auto':
                wanted = min(horizon_days, room)
            else:
                wanted = max(1, min(int(design_parallel_days), horizon_days, room))
            _design_workers['failure'] = None
            if wanted >= 2:
                worker_threads = max(1, (os.cpu_count() or 2) // (wanted + 1))
                started_pool = _design_start_workers({
                    'config': _design_config_snapshot(),
                    'all_trips': all_trips, 'pool': pool,
                    'charging_infrastructure': list(design_infrastructure),
                    'charging_station_ids': list(design_station_ids),
                    'gurobi_threads': worker_threads,
                    'scenario': scenario_iteration, 'year': year_iteration,
                    'v2g': v2g_status_iteration,
                }, wanted)
                parallel_workers_wanted = wanted
                parallel_start_failure = _design_workers.get('failure')
                if started_pool is not None:
                    parallel_workers_used = wanted
                    if show_progress:
                        print(f"  day-solves in parallel: {wanted} worker(s) x "
                              f"{worker_threads} Gurobi thread(s)", flush=True)

            # 3.5.4 the floor, taken as a dual bound and not as a solution.
            #
            #       Costing the full pool outright is the obvious way to bound every candidate
            #       from below - and it is the single worst solve in the search. Measured on
            #       day 1: 2 trucks converge in 1.1 s, 5 in 11.7 s, and the 20-truck pool sits
            #       at one node and a 44.7 % gap after 60 s. It is the same root-node wall the
            #       monolith hits, for the same reason: fifteen bevs is fifteen sets of
            #       bilinear terms.
            #
            #       The incumbent of that solve is not what is needed. Gurobi's *dual bound* on
            #       the full-pool day is already a lower bound on that day under every fleet
            #       (the pool is a superset of all of them, 3.5.2), and the root relaxation
            #       that produces it is cheap. So the pool is solved under a short cap and only
            #       ObjBound is read off.
            day_floor = {}
            pool_used = {}
            floor_workers = _design_workers.get('pool')
            _design_solves['floor'] += len(day_order)
            _tick('bound', 0, len(day_order))
            if floor_workers is not None:
                floor_results = floor_workers.map(
                    _design_workers['floor_task'],
                    [(day, date, design_bound_limit_s) for day, date in day_order])
            else:
                floor_results = [
                    (day,) + _design_day_floor(day, date, pool, params_for_day,
                                               design_bound_limit_s)
                    for day, date in day_order]
            for position, (day_index, bound, used) in enumerate(floor_results, 1):
                _tick('bound', position, len(day_order))
                if used is not None:
                    pool_used[day_index] = used
                if bound is None:
                    day_floor = {}
                    break
                day_floor[day_index] = bound
            operating_bound = (sum(day_floor.values())
                               if len(day_floor) == len(day_order) else None)
            if show_progress and operating_bound is not None:
                print(f"  operating floor over the range: {operating_bound:,.0f} EUR",
                      flush=True)

            # 3.5.4b the smallest fleet that can serve the range at all, found once.
            #
            #        Without this the search discovers it the hard way: cheapest ownership
            #        first means it meets the too-small fleets first, and each one costs a
            #        solve to reject. On the measured seven-day run that was 58 of 60
            #        evaluations spent proving infeasibility, with only two fleets ever costed.
            #        Bisecting on the strongest fleet of each size settles the boundary in
            #        about log2(pool) solves and turns the rest into arithmetic.
            # The arithmetic floor has to leave the bisection something to bisect. If it
            # already exceeds the pool, low > high and the loop never runs: the old code
            # then returned (None, 0, [], True), slipped past the error below on the
            # `size_floor_evals` term, searched an empty candidate list and finally raised
            # "no feasible fleet inside the search budget" - naming a budget that was never
            # touched, when the truth is that the pool is too small to serve the range.
            # Said here instead, where the two numbers are still in hand.
            if min_vehicles > len(pool):
                raise ValueError(
                    f"the range needs at least {min_vehicles} vehicles by its driving volume "
                    f"alone, and the candidate pool holds {len(pool)} "
                    f"({len(type_order)} type(s) x {copies} copies). No fleet drawn from it "
                    f"can serve these days. Raise pool_copies, add types to the "
                    f"'synthetic_fleet' sheet, or shorten the range.")
            ranked = _design_capability_order(pool, type_of, by_type, type_order)
            size_floor_evals = 0
            probe_costed = []
            # whether the bisection shortcut was available at all (3.5.4b). When no single
            # capability order dominates - two types where each beats the other on range
            # and on charging power - there is no strongest-fleet-of-size to probe, so the
            # run falls back to paying for every too-small fleet the hard way. That is
            # correct and it is 10-50x slower on a wide pool, so it is recorded rather than
            # left to be inferred from a wall clock.
            size_floor_available = ranked is not None
            if not size_floor_available and show_progress:
                print("  no dominating capability order over the pool's types, so the "
                      "minimum-fleet bisection is skipped and every too-small fleet is "
                      "rejected by solving it", flush=True)
            if ranked is not None:
                def _say(size, status):
                    if show_progress:
                        print(f"  fleet size {size}: {status}", flush=True)
                proved_size, size_floor_evals, probe_costed, conclusive = _design_min_fleet_size(
                    ranked, by_type, pool, day_order, params_for_day,
                    design_search_day_limit_s, min_vehicles, len(pool), report=_say)
                evaluated += size_floor_evals
                _tick('search', evaluated)
                if proved_size is None and conclusive:
                    raise ValueError(
                        "the design model found no feasible fleet: not even the whole pool can "
                        "serve every day of the range. Widen the pool (fleet_pool_copies_for), "
                        "relax the crew rules, or check that the days can be served at all.")
                if proved_size is not None:
                    min_vehicles = max(min_vehicles, proved_size)
                    if show_progress:
                        print(f"  smallest fleet that can serve the range: {min_vehicles} "
                              f"vehicles ({size_floor_evals} solve(s))", flush=True)
                elif not conclusive:
                    # a day-solve ran out of time, so nothing about the minimum size was
                    # settled. The search carries on with the arithmetic floor and pays for the
                    # too-small fleets the hard way; raising design_search_day_limit_s is what
                    # buys the shortcut back.
                    search_limit_hit = search_limit_hit or 'day-solve time limit'
                    if show_progress:
                        print("  minimum fleet size not settled: a probe ran out of time "
                              "(raise design_search_day_limit_s)", flush=True)

            # 3.5.5 the incumbent comes from the search itself.
            #
            #       Searching cheapest-ownership-first means the small fleets - the fast ones -
            #       are costed first, and the first feasible one is already an upper bound. The
            #       expensive direction is entered only if the cheap one fails to beat it, which
            #       is the opposite of seeding from the pool and is why this terminates.
            # the bisection's feasible probes are already-costed fleets: strongest-of-size, so
            # usually cheap in count if not in composition. Folding them in gives the ownership
            # cutoff an incumbent before the lattice is touched, and gives operating_floor()
            # real supersets to work with instead of only the full-pool bound.
            costed = list(probe_costed)
            best_counts = None
            best_total = None
            for probe_counts, probe_operating in probe_costed:
                probe_total = (probe_operating
                               + ownership_per_day_of(probe_counts) * horizon_days)
                if best_total is None or probe_total < best_total:
                    best_counts, best_total = dict(probe_counts), probe_total
            if show_progress and best_total is not None:
                print(f"  best of the size probes: "
                      f"{[best_counts[t] for t in type_order]} -> {best_total:,.0f} EUR",
                      flush=True)

            # 3.5.5b a first incumbent from the pool's own answers. Each day of the floor solve
            #        that found an incumbent named the vehicles it used; a fleet holding the
            #        most of each type any single day needed serves all of them, because every
            #        day's own selection is contained in it. It costs one evaluation and gives
            #        the ownership cutoff something to bite on from the first candidate.
            if len(pool_used) == len(day_order):
                seed = {t: 0 for t in type_order}
                for used in pool_used.values():
                    day_counts = {t: 0 for t in type_order}
                    for vehicle_id in used:
                        day_counts[type_of[vehicle_id]] += 1
                    for t in type_order:
                        seed[t] = max(seed[t], day_counts[t])
                if sum(seed.values()) >= min_vehicles:
                    outcome = _design_evaluate(seed, by_type, pool, day_order, params_for_day,
                                               design_search_day_limit_s)
                    evaluated += 1
                    _tick('search', evaluated)
                    if outcome['status'] == 'ok':
                        costed.append((dict(seed), outcome['operating']))
                        seed_total = (outcome['operating']
                                      + ownership_per_day_of(seed) * horizon_days)
                        if best_total is None or seed_total < best_total:
                            best_counts, best_total = dict(seed), seed_total
                        if show_progress:
                            print(f"  seed fleet {[seed[t] for t in type_order]} -> "
                                  f"{seed_total:,.0f} EUR", flush=True)
                    # ... and a seed that did not come back ok is one more thing the search
                    # learned, not a null result. It used to be dropped on the floor: an
                    # infeasible seed never reached proven_infeasible, so its whole down-set
                    # was searched again one fleet at a time, and a timed-out one never
                    # reached timed_out, so the run could still report search_exhausted.
                    elif outcome['status'] == 'infeasible':
                        proven_infeasible.append(dict(seed))
                        if show_progress:
                            print(f"  seed fleet {[seed[t] for t in type_order]}: "
                                  f"infeasible", flush=True)
                    elif outcome['status'] == 'unknown':
                        timed_out.append(dict(seed))
                        if show_progress:
                            print(f"  seed fleet {[seed[t] for t in type_order]}: "
                                  f"ran out of time", flush=True)

            # 3.5.6 the lattice, cheapest ownership first. Ordering by ownership is what makes
            #       the cutoff terminal: ownership only grows down the list, so the first
            #       candidate whose ownership alone cannot beat the incumbent ends the search.
            candidates = []
            for combo in itertools.product(*[range(len(by_type[t]) + 1) for t in type_order]):
                counts = dict(zip(type_order, combo))
                if sum(combo) < min_vehicles:
                    continue
                candidates.append((ownership_per_day_of(counts), counts))
            candidates.sort(key=lambda entry: entry[0])

            def operating_floor(counts):
                """Tightest known lower bound on this fleet's operating cost.

                Cost is monotone non-increasing in the fleet (3.5.2), so any *superset* that
                has already been costed bounds this one from below - and a near superset bounds
                it far better than the full pool does. The full pool is the fallback because it
                is a superset of everything.
                """
                floor = operating_bound if operating_bound is not None else float('-inf')
                for bigger, bigger_operating in costed:
                    if all(counts[t] <= bigger[t] for t in type_order):
                        floor = max(floor, bigger_operating)
                return floor

            exhausted_list = True
            for own_per_day, counts in candidates:
                if any(all(counts[t] <= bad[t] for t in type_order) for bad in proven_infeasible):
                    continue
                own_total = own_per_day * horizon_days
                if best_total is not None:
                    # terminal: ownership only grows down the list and the full-pool floor
                    # applies to every remaining candidate, so nothing after this can win
                    if (operating_bound is not None
                            and own_total + operating_bound >= best_total):
                        search_exhausted = True
                        exhausted_list = False
                        break
                    # not terminal: a tighter floor from an already-costed superset can rule
                    # this one out without ruling out the cheaper-ownership fleets after it
                    if own_total + operating_floor(counts) >= best_total:
                        continue
                if (design_max_fleet_evals is not None
                        and evaluated >= design_max_fleet_evals):
                    search_limit_hit = 'fleet evaluations'
                    exhausted_list = False
                    break
                if (design_max_search_seconds is not None
                        and time.time() - search_started >= design_max_search_seconds):
                    search_limit_hit = 'search seconds'
                    exhausted_list = False
                    break
                outcome = _design_evaluate(
                    counts, by_type, pool, day_order, params_for_day,
                    design_search_day_limit_s, day_floor=day_floor,
                    abort_above=(None if best_total is None else best_total - own_total))
                evaluated += 1
                _tick('search', evaluated)
                if outcome['status'] == 'infeasible':
                    proven_infeasible.append(counts)
                    continue
                if outcome['status'] == 'unknown':
                    timed_out.append(counts)
                    continue
                if outcome['status'] == 'dominated':
                    continue
                costed.append((dict(counts), outcome['operating']))
                total = outcome['operating'] + own_total
                if best_total is None or total < best_total:
                    best_counts, best_total = dict(counts), total
                    if show_progress:
                        print(f"  fleet {[counts[t] for t in type_order]} -> "
                              f"{total:,.0f} EUR over {horizon_days} day(s)", flush=True)
            if exhausted_list:
                search_exhausted = True
            # a fleet that only timed out was never ruled out, so a search that met one has not
            # proved its answer however cleanly it ran off the end of the list
            if timed_out:
                search_exhausted = False
                if search_limit_hit is None:
                    search_limit_hit = 'day-solve time limit'

            if best_counts is None:
                raise ValueError(
                    "the design model found no feasible fleet inside the search budget. Raise "
                    "design_max_fleet_evals or design_search_day_limit_s, widen the pool "
                    "(fleet_pool_copies_for), or relax the crew rules.")

            # 3.5.7 the answer, re-solved. The search ran under design_search_day_limit_s; the
            #       fleet that won is costed again without that cap, so what is reported is a
            #       schedule certified to optimization_MIPGap like any disposition run.
            bought = _design_fleet_ids(best_counts, by_type)
            fleet_bought = pool[pool['vehicle_id'].isin(bought)].reset_index(drop=True)
            final_frame = fleet_bought.copy()

            day_results = []
            day_gaps = []
            day_statuses = []
            operating_total = 0.0
            for position, (day_index, date_text) in enumerate(day_specs, 1):
                _design_solves['final'] += 1
                _tick('solve', position, len(day_specs))
                # this day's own baseline load and PV, which the model is built against
                # below and - see the model_parameters around postprocess - has to be
                # *reported* against too
                curves = day_curves_for(date_text)
                build_arguments, context = run_optimization(
                    params_for_day[day_index], fleet_override=final_frame,
                    day_curves=curves, build_only=True)
                # the objective the search ranked this fleet on (3.5.0a)
                model, E_neg, y_m, _day_cost = model_build(
                    **_design_objective_arguments(build_arguments))
                # disposed on every exit, not only on the ones thought of. The search path
                # has finally-blocks around its solves and this one did not: a postprocess
                # that raised left a Gurobi model alive for the rest of the process, once
                # per day of the range.
                try:
                    with model_parameters(optimization_time_limit_s=design_final_day_limit_s):
                        solve_model(model, optimization_MIPGap, gurobi_threads)
                    if model.SolCount == 0:
                        # the same fleet was feasible during the search, so the only two
                        # readings are a clock that ran out and a genuine decomposition
                        # bug - and the message used to assert the second without checking
                        # for the first. design_final_day_limit_s is the one thing that can
                        # stop this solve, so it is named when it is set.
                        if design_final_day_limit_s is not None:
                            raise ValueError(
                                f"the winning fleet found no schedule for day {day_index} within "
                                f"design_final_day_limit_s ({design_final_day_limit_s:g} s). The "
                                f"fleet itself is fine - it was solved during the search - so this "
                                f"is the cap, not the decomposition. Raise it, or set it to None to "
                                f"run the final solve to optimization_MIPGap like any other day.")
                        raise ValueError(
                            f"the chosen fleet could not be re-solved on day {day_index}. This is a "
                            "bug in the decomposition, not a data problem: the same fleet was "
                            "feasible during the search, and nothing capped this solve.")
                    operating_total += float(model.ObjVal)
                    day_gaps.append(float(model.MIPGap))
                    # what the solver actually said about this day, kept rather than
                    # assumed. design['solver_status'] used to be the literal 2 whatever
                    # happened here (3.5.9).
                    day_statuses.append(int(model.Status))
                    # postprocess reads the depot's baseline load and PV curve as module
                    # globals - they are not in its signature - and those still hold the
                    # curves of date_disposition, the single day the module was configured
                    # for. A design run spans a range and builds each day against its own
                    # (day_curves above), so without this the demand charge, the grid peak,
                    # the no-BEV counterfactual and the grid figure of *every* day in the
                    # range were computed from one arbitrary day's sunshine and site load.
                    # The objective had the right curves and the report had the wrong ones,
                    # which is what the objective_residual of 5.8b caught.
                    with model_parameters(
                            depot_baseline_load_kW=curves['depot_baseline_load_kW'],
                            pv_generation_kW=curves['pv_generation_kW']):
                        result = postprocess(
                            model=model,
                            vehicles=context['vehicles'],
                            vehicle_types=context['vehicle_types'],
                            bev_vehicles=context['bev_vehicles'],
                            ice_vehicles=context['ice_vehicles'],
                            day_trips_list=context['day_trips_list'],
                            fleet=context['fleet'],
                            trips=context['trips'],
                            charging_infrastructure=charging_infrastructure,
                            write_outputs=writes_outputs(),
                            auto_sizing='on',
                            v2g_status_iteration=v2g_status_iteration,
                            costs_v2g=context['costs_v2g'],
                            penalty_charging_use=penalty_charging_use,
                            penalty_charging_block=penalty_charging_block,
                            penalty_vehicle_use=penalty_vehicle_use,
                            penalty_charging_external_time=penalty_charging_external_time,
                            time_steps=time_steps,
                            locations=context['locations'],
                            trip_distances=context['trip_distances'],
                            possible_start_times=context['possible_start_times'],
                            trips_duration_steps=context['trips_duration_steps'],
                            scenario_iterations=scenario_iteration,
                            scenario_year_iterations=year_iteration,
                            E_neg=E_neg,
                            y_m=y_m,
                            cost_vehicle_100km=context['cost_vehicle_100km'],
                            year_iteration=year_iteration,
                            depot_buy_eur_per_kWh_t=context['depot_buy_eur_per_kWh_t'],
                            public_charging_cost_eur_per_kWh=context['public_charging_cost_eur_per_kWh'],
                            event_durations=context['event_durations'],
                            event_possible_starts=context['event_possible_starts'],
                            virtual_trips=context['virtual_trips'],
                            v2g_virtual_trips=context['v2g_virtual_trips'],
                            chg_virtual_trips=context['chg_virtual_trips'],
                            toll_rate_per_km=context['toll_rate_per_km'],
                            pv_charging_available_kWh=context['pv_charging_available_kWh'],
                            pv_charging_eur_per_kWh_t=context['pv_charging_eur_per_kWh_t'],
                            day_routing=context['day_routing'],
                            approach_steps=context['approach_steps'],
                            return_steps=context['return_steps'],
                            link_steps=context['link_steps'],
                            dropped_trips=context['dropped_trips'],
                            warm_start_used=False,
                            charging_station_ids=charging_station_ids,
                            trips_outside_work_hours=context['trips_outside_work_hours'],
                            v2g_channel_at_step=context['v2g_channel_at_step'],
                            costs_v2g_arbitrage=context['costs_v2g_arbitrage'],
                            costs_v2g_flexibility=context['costs_v2g_flexibility'],
                            tag='',
                            plot_suffix=f"_day{day_index}",
                        )
                    # solve_*, not design_*: the design_* columns of the summary (5.9) are the
                    # run's one answer repeated on every row, and these two vary per row. Sharing
                    # the prefix put two opposite meanings in adjacent columns.
                    result['solve_day'] = day_index
                    result['solve_date'] = date_text
                    day_results.append(result)
                finally:
                    model.dispose()
        finally:
            _design_stop_workers()

    # 3.4.5 the charging infrastructure the days turned out to need - read off the finished
    #       schedules rather than optimized: no station is a decision variable
    ranked_peaks = []
    for result in day_results:
        peaks = sorted((float(kW) for kW in (result.get('chargers_kW') or [])), reverse=True)
        for rank, peak_kW in enumerate(peaks):
            while len(ranked_peaks) <= rank:
                ranked_peaks.append(0.0)
            ranked_peaks[rank] = max(ranked_peaks[rank], peak_kW)
    chargers_needed = [round(kW, 1) for kW in ranked_peaks]

    ownership_per_day = sum(daily_ownership_cost(p) for p in fleet_bought['vehicle_price'])
    objective = operating_total + ownership_per_day * horizon_days
    # ... and the same total with the search apparatus taken out of it (5.8b). operating_total
    # is a sum of ObjVals, so it carries every steering penalty of every day: the truck-use
    # charge, the charger-slot charge, the driver head charge, the id tie-break and the rest.
    # That is the right number for the *search* - it is what the lattice compared candidates
    # on - and the wrong one to quote as what the fleet costs to run. Both are reported, and
    # the penalty-free pair is the one a cost comparison between designs should use.
    #
    # The two also differ by the driver salary now, in the other direction: since 1.4c the
    # objective carries no wage, so operating_total does not either, while operating_clean
    # sums the reported operating_cost_€ of each day and those do include the roster's bill.
    # That is deliberate on both sides - the fleet is CHOSEN on what it costs to own and run
    # under the crew rules, and REPORTED with the crew it turned out to need priced in - but
    # it does mean the search cannot prefer a fleet for needing fewer drivers except through
    # penalty_driver_use and through the ownership term. On a range where the wage is the
    # thing being decided, read design_operating_clean_EUR rather than design_objective_EUR.
    operating_clean = sum(float(r.get('operating_cost_€') or 0.0) for r in day_results)
    steering_total = sum(float(r.get('steering_penalties_€') or 0.0) for r in day_results)

    # 3.5.9 what the solver proved about the answer, which is the worst of what it proved
    #       about the answer's days.
    #
    # This column was the literal GRB.OPTIMAL, unconditionally, regardless of whether the
    # final day-solves converged, ran into design_final_day_limit_s, or came back with an
    # incumbent of unknown quality. It sat next to search_exhausted, search_limit_hit,
    # gap_basis and day_mip_gaps - every one of which is scrupulous about saying how much
    # was actually established - and said "proved optimal" in all three cases. Of
    # everything a design run reports it was the one figure that could not be wrong by
    # accident, because nothing was ever read to produce it.
    #
    # Read now, and reduced the same way the gap is: a horizon is not one model, so the
    # honest summary over D days is the weakest certificate any of them carries. The word
    # form uses the same three names as a single-day run (5.5), because a reader should not
    # have to learn a second vocabulary for the same distinction, and the per-day codes are
    # kept beside it exactly as day_mip_gaps is.
    worst_status = (gp.GRB.OPTIMAL if not day_statuses
                    else (gp.GRB.OPTIMAL if all(s == gp.GRB.OPTIMAL for s in day_statuses)
                          else next(s for s in day_statuses if s != gp.GRB.OPTIMAL)))
    worst_gap = max(day_gaps) if day_gaps else 0.0
    if worst_status != gp.GRB.OPTIMAL:
        design_optimization_status = 'incumbent'
    elif worst_gap <= MIP_GAP_PROVEN:
        design_optimization_status = 'optimal'
    else:
        design_optimization_status = 'gap-optimal'

    design = {
        'days': horizon_days,
        'pool_copies_per_type': copies,
        'pool_size': len(pool),
        'fleet_bought_ids': bought,
        'fleet_size': len(bought),
        'ice_bought': int((fleet_bought['vehicle_type'] == 'ice').sum()),
        'bev_bought': int((fleet_bought['vehicle_type'] == 'bev').sum()),
        'fleet_capital_EUR': round(float(fleet_bought['vehicle_price'].sum()), 2),
        'ownership_cost_per_day_EUR': round(ownership_per_day, 2),
        'ownership_cost_horizon_EUR': round(ownership_per_day * horizon_days, 2),
        # what the search minimised - ObjVal per day plus ownership, steering penalties
        # and all. Kept because it is the number the candidates were ranked on.
        'objective_EUR': round(objective, 2),
        # ... and what the winning design actually costs to own and run, with the search
        # apparatus removed (5.8b). This is the figure to compare two designs on.
        'operating_cost_clean_horizon_EUR': round(operating_clean, 2),
        'steering_penalties_horizon_EUR': round(steering_total, 2),
        'total_cost_clean_EUR': round(operating_clean + ownership_per_day * horizon_days, 2),
        # the worst per-day certificate on the winning fleet. Not a horizon-wide Gurobi gap
        # - the horizon was never one model - so it is labelled for what it is (3.5.3)
        'mip_gap': round(worst_gap, 4),
        # the weakest certificate any day of the winning fleet carries (3.5.9), on the same
        # 'worst day' basis as mip_gap - never the assumption that all of them converged
        'solver_status': int(worst_status),
        'solver_status_basis': 'worst day',
        'optimization_status': design_optimization_status,
        'day_solver_statuses': list(day_statuses),
        'fleet_bought': fleet_bought.to_dict('records'),
        'design_station_kW': design_station_kW,
        'design_stations_offered': len(design_infrastructure),
        'chargers_needed': chargers_needed,
        'chargers_needed_count': len(chargers_needed),
        'chargers_needed_peak_kW': round(max(ranked_peaks), 1) if ranked_peaks else 0.0,
        # 3.5 bookkeeping: how the fleet was found, and whether the search proved it
        # what the gap above is a gap *of*. There is no single model over the range, so
        # this is the worst of the per-day gaps on the winning fleet rather than one gap
        # over the horizon - a different quantity, named so it cannot be mistaken (3.5.3)
        'gap_basis': 'worst day',
        # whether the fleet search finished because it had proved its answer, or because a
        # limit stopped it. search_limit_hit names which, and is None when nothing bound
        'search_limit_hit': search_limit_hit,
        'search_seconds': round(time.time() - search_started, 1),
        'parallel_day_workers': parallel_workers_used,
        'parallel_day_workers_wanted': parallel_workers_wanted,
        'parallel_start_failure': parallel_start_failure,
        'min_fleet_size': min_vehicles,
        # whether the minimum-fleet bisection (3.5.4b) was usable at all. False means the
        # pool's types have no dominating capability order, so the shortcut was skipped and
        # every too-small fleet had to be rejected by solving it - the same answer, at
        # 10-50x the solves on a wide pool. Reported because a search that quietly took the
        # slow path is indistinguishable from a slow instance.
        'min_fleet_bisection_used': bool(size_floor_available),
        'fleets_evaluated': evaluated,
        # counted, not inferred (3.5.0): a candidate is abandoned on its hardest day and
        # the floor and re-solve passes are not candidate evaluations at all, so no formula
        # over `evaluated` describes what the search actually ran
        'day_solves': (_design_solves['candidate'] + _design_solves['floor']
                       + _design_solves['final']),
        'day_solves_candidates': _design_solves['candidate'],
        'day_solves_floor': _design_solves['floor'],
        'day_solves_final': _design_solves['final'],
        # dispatched to a worker and then not waited for. An upper bound: a task still
        # queued when the iterator was dropped never started
        'day_solves_abandoned': _design_solves['abandoned'],
        'search_exhausted': search_exhausted,
        'fleets_proven_infeasible': len(proven_infeasible),
        'fleets_timed_out': len(timed_out),
        'day_mip_gaps': [round(g, 4) for g in day_gaps],
        'operating_cost_horizon_EUR': round(operating_total, 2),
    }
    return design, day_results



# 4 OPTIMIZATION
# 4.1 optimization subfunction
def solve_model(model, optimization_MIPGap, gurobi_threads, time_limit_s=None):
    # 4.2 set model parameters
    model.setParam('OutputFlag', 0)
    model.setParam('LogToConsole', 0)
    model.setParam('MIPGap', optimization_MIPGap)
    model.setParam('Threads', gurobi_threads)
    # No wall-clock cap by default: the solver runs until it meets optimization_MIPGap.
    # Set explicitly rather than left to Gurobi's default so the intent is on the page -
    # a stopped solve and a converged one are not the same result, and a time limit that
    # is not written down anywhere is the kind of thing that silently decides a study.
    #
    # time_limit_s overrides the run's own cap for one solve, and is only ever tightened
    # by it: it exists for the warm start (2.8a), which is a means and not an answer and
    # so must not be allowed to spend the budget the answer needs. The run's cap still
    # applies when it is the shorter of the two.
    run_cap = (gp.GRB.INFINITY if optimization_time_limit_s is None
               else float(optimization_time_limit_s))
    model.setParam('TimeLimit', run_cap if time_limit_s is None
                   else min(run_cap, float(time_limit_s)))
    # auto unless the run says otherwise; the reasoning is with optimization_presolve in
    # section 1.3. -1 = auto, 0 = off, 1 = conservative, 2 = aggressive
    model.setParam('Presolve', optimization_presolve)
    model.setParam('Aggregate', 1)  # 0 = off, 1 = moderate, 2 = aggressive
    model.setParam('Cuts', -1)  # -1 = auto, 0 = off, 1 = conservative, 2 = aggressive, 3 = very aggressive
    model.setParam('Method', -1)  # -1 = auto, 0 = primal simplex, 1 = dual simplex, 2 = barrier with crossover, 3 = concurrent, 4 = deterministic concurrent
    # Balanced by default, and set from a parameter rather than from the fleet mode - the
    # reasoning and the measurement behind it are with optimization_MIPFocus in section 1.3.
    # Written out even when it holds Gurobi's own default, for the same reason TimeLimit is:
    # a search-effort setting that decided a study should be on the page, not implied.
    model.setParam('MIPFocus', optimization_MIPFocus)
    # off by default: the node LPs of a time-indexed schedule are degenerate enough that
    # chasing equivalent bases costs more than it returns. Reasoning and measurement are
    # with optimization_DegenMoves in section 1.3.
    model.setParam('DegenMoves', optimization_DegenMoves)


    # 4.3 solve the MILP model
    model.optimize()

    # 4.3b the one infeasibility this file can name on sight.
    #
    # An infeasible MILP says nothing about which constraint did it, and the price on the
    # driving limits (3.3.17) exists precisely so that the crew side answers with a figure
    # rather than with a shrug. The shift limit is the exception - it is a hard rule by
    # default (driver_shift_limit, 1.4c) - so it is the one crew constraint that CAN make a
    # day infeasible, and a run that hits it deserves to be told which knob it is looking
    # at rather than left to bisect the parameter block.
    #
    # Stated as a hint and not as a diagnosis: nothing here proves the shift limit is the
    # cause, and an infeasible day usually has several. It names the candidate, says how to
    # test it, and leaves the conclusion to whoever reads the two runs.
    #
    # Gated on the crew rules being IN this model rather than on the mode alone, because
    # solve_model is also handed the relaxed warm-start build (2.8a), which is the same day
    # with 3.3.17 left out. An infeasibility there cannot be the shift limit, and saying it
    # might be would send the reader to the one parameter that provably did not cause it.
    # The scan costs one pass over the variables and only on the infeasible path.
    crew_rules_in_model = (fleet_operation_mode == 'crewed'
                           and model.Status == gp.GRB.INFEASIBLE
                           and any(v.VarName.startswith('drivers_needed')
                                   for v in model.getVars()))
    if writes_outputs() and crew_rules_in_model and driver_shift_limit == 'hard':
        print(f"infeasible. If this day was feasible before, the "
              f"{driver_max_shift_hours:g} h shift limit is the first thing to test: it is "
              f"a hard constraint (3.3.17a), so a day whose trips can only be served by "
              f"absences longer than one lawful shift now has no schedule at all rather "
              f"than an expensive one. Re-run with driver_shift_limit = 'priced' - if that "
              f"returns a schedule with over-shift duty blocks in it, the finding is that "
              f"this fleet cannot serve this day legally, and the fix is trucks or depots "
              f"rather than a solver setting.", flush=True)

    # 4.4 model tuning
    #model.write(str(project_path('results', 'fleet_disposition_tuning_model.lp')))
    #model.tune()
    #model.getTuneResult(0)
    #model.write(str(project_path('results', 'fleet_disposition_tuning_model_tuneresults.prm')))



# 4.5 the roster feedback pass (2.8c)
def _roster_score(model, head_price_in_objective, head_price_for_scoring, vehicles,
                  time_steps, day_trips_list, possible_start_times, trips_duration_steps,
                  day_routing):
    """Score a solved schedule against the head count its roster really needs.

    Returns (score, roster, peak). The score is the one measure on which two candidate
    schedules can be compared fairly here:

        ObjVal
          - the head proxy inside it        (head price x peak concurrency)
          + the run's own head price x the heads the roster actually needs

    i.e. the objective with the model's lower bound on the head count swapped for the
    figure the packing reached. Both candidates are scored with the same per-head price,
    or the comparison is rigged in favour of whichever was solved against the larger one.

    The WAGE is deliberately not in this. It is not in the objective either (1.4c), and
    putting it back here would let a pass whose whole purpose is the head count decide
    between two schedules on a salary the solver never saw - which is the reintroduction
    of the term through the back door. driver_cost_€ is reported from the surviving
    roster in 5.5c, where it belongs.

    head_price_in_objective is the penalty_driver_use the model was *built* with, which
    is not always the one configured for the run - the pass below deliberately solves a
    trial against a higher one to steer it. Subtracting the price that is actually inside
    ObjVal is what makes the two scores commensurable.

    The crew-rule breach penalty stays inside ObjVal on purpose. It is not a wage and not
    a real cost, but it is the only thing between a legal schedule and an illegal one, and
    removing it from the comparison would let the pass prefer a cheaper illegal answer.

    postprocess is not used for any of this: it writes every figure of the run as a side
    effect, so it cannot be asked a what-if about a schedule that may yet be discarded.
    """
    import hdv_driver_scheduling as driver_scheduling
    away = driver_away_flags(model, vehicles, time_steps, day_trips_list,
                             possible_start_times, trips_duration_steps, day_routing)
    roster = driver_scheduling.schedule_drivers(
        away, STEP_HOURS, driver_max_shift_hours, driver_hourly_rate_eur,
        max_working_hours=driver_max_working_hours,
        max_driving_hours=driver_max_driving_hours,
        wheel_hours_by_vehicle=driver_wheel_hours(model, vehicles, time_steps))
    needed = model.getVarByName('drivers_needed')
    peak = 0.0 if needed is None else needed.X
    score = (float(model.ObjVal) - head_price_in_objective * peak
             + head_price_for_scoring * roster['driver_count'])
    return score, roster, peak


def driver_roster_feedback_pass(model, E_neg, y_m, build_arguments, vehicles, time_steps,
                                day_trips_list, possible_start_times, trips_duration_steps,
                                day_routing):
    """Re-solve once against a driver head price the roster agrees with; keep the better.

    Returns (model, E_neg, y_m, head_price) for whichever schedule wins; the loser is
    disposed of. head_price is the penalty_driver_use the surviving model was *built*
    with, which is the re-priced one if the trial won. The caller has to carry it into
    postprocess: that is the figure inside this model's ObjVal, so it is the figure the
    penalty accounting has to subtract for the reconciliation to close (5.8b).
    """
    run_head_price = penalty_driver_use
    score_here, roster, peak = _roster_score(
        model, run_head_price, run_head_price, vehicles, time_steps, day_trips_list,
        possible_start_times, trips_duration_steps, day_routing)
    if roster['driver_count'] <= peak + 1e-9:
        return model, E_neg, y_m, run_head_price   # the proxy already covers the heads

    # What the driver term *should* have come to, expressed through the one lever the
    # model has. The objective charges `head price x peak concurrency`; what the day
    # actually needs is the same per-head weight over the heads the roster reaches.
    # Solving
    #
    #     new price x peak  =  run price x roster heads
    #
    # gives a head charge that makes the model's proxy come out at the right total, so
    # the pressure lands on peak concurrency - which is what the packing is short of. It
    # reduces to the run's own price exactly when the roster needs no more heads than the
    # peak, i.e. when nothing was wrong.
    #
    # The roster's cash bill used to be added into that numerator, back when the wage was
    # in the objective and the proxy was supposed to reproduce it. It is not, now (1.4c),
    # and adding it would be worse than redundant: the whole salary would re-enter the
    # objective through the head price, scaled by 1/peak, which is precisely the term this
    # model was asked to stop optimising against.
    #
    # Floored at the run's price so the pass can only ever make heads dearer, never
    # cheaper - lowering them is not a correction, it is a different model of what a
    # driver is worth, and that is a parameter decision and not this function's to make.
    if peak <= 1e-9:
        return model, E_neg, y_m, run_head_price
    repriced = max(run_head_price, run_head_price * roster['driver_count'] / peak)
    if repriced <= run_head_price + 1e-9:
        return model, E_neg, y_m, run_head_price

    if writes_outputs():
        print(f"driver feedback: the roster needs {roster['driver_count']} driver(s) "
              f"against the {peak:.2f} the objective paid for; re-solving once with the "
              f"head price at {repriced:.2f} EUR instead of {run_head_price:.2f}")

    trial = None
    try:
        with model_parameters(penalty_driver_use=repriced):
            trial, trial_E_neg, trial_y_m, _cost = model_build(**build_arguments)
            solve_model(trial, optimization_MIPGap, gurobi_threads)
    except gp.GurobiError as exc:
        # a second solve failing is not a reason to lose the first answer
        if trial is not None:
            trial.dispose()
        if writes_outputs():
            print(f"driver feedback: the re-solve failed ({exc}); keeping the first "
                  f"schedule")
        return model, E_neg, y_m, run_head_price

    if trial.SolCount == 0:
        trial.dispose()
        return model, E_neg, y_m, run_head_price

    # scored with the inflated price taken out of its objective and the run's own price
    # applied to its heads, exactly as the incumbent was
    score_trial, trial_roster, _peak = _roster_score(
        trial, repriced, run_head_price, vehicles, time_steps, day_trips_list,
        possible_start_times, trips_duration_steps, day_routing)

    if score_here <= score_trial:
        if writes_outputs():
            print(f"driver feedback: the first schedule is still cheaper once both are "
                  f"costed against their own rosters ({score_here:.2f} vs "
                  f"{score_trial:.2f} EUR); keeping it")
        trial.dispose()
        return model, E_neg, y_m, run_head_price

    if writes_outputs():
        print(f"driver feedback: the re-solved schedule rosters "
              f"{trial_roster['driver_count']} driver(s) and costs {score_trial:.2f} EUR "
              f"against {score_here:.2f}; taking it")
    model.dispose()
    return trial, trial_E_neg, trial_y_m, repriced


# 5 POSTPROCESSING
# 5.0 reading the crew side of a solved schedule back
#
# Two small readers, used by postprocess (5.5c) and by the roster feedback pass (2.8c).
# They are separate functions because the feedback pass needs the roster *before* anything
# is reported, and postprocess is far too heavy - and far too full of side effects, it
# writes every figure of the run - to be called for that.
def driver_away_flags(model, vehicles, time_steps, day_trips_list, possible_start_times,
                      trips_duration_steps, day_routing, tag=''):
    """{vehicle: [is it away from the depot in this step, ...]} from a solved model.

    Which steps count as away depends on how much geography the run had. With route
    chaining on, at_depot says it outright and the answer therefore covers the empty legs
    and any waiting in a customer yard - all of it time a driver cannot leave the truck.
    With chaining off there is no such state, and the best available reading is the loaded
    driving itself; that *understates* the roster, because the repositioning and waiting
    it cannot see still needed somebody.

    Read off the model's own at_depot whenever the model has one, rather than off
    day_routing. Since 3.3.16d-off the chaining-free crewed model carries at_depot too -
    pinned to exactly the "away while driving" reading below - so taking it from the
    variable means the roster and the MILP are looking at one number instead of at two
    implementations of it. The fallback stays for the models that genuinely have no such
    state: an autonomous run, and the relaxed warm-start twin of 2.8a.
    """
    away_by_vehicle = {}
    has_state = (bool(vehicles)
                 and model.getVarByName(f"at_depot{tag}[{vehicles[0]},{time_steps[0]}]")
                 is not None)
    for m in vehicles:
        if has_state:
            away_by_vehicle[m] = [
                model.getVarByName(f"at_depot{tag}[{m},{t}]").X < 0.5 for t in time_steps]
        else:
            driving = set()
            for f in day_trips_list:
                for s in possible_start_times.get(f, []):
                    if model.getVarByName(f"z_m_f_s{tag}[{m},{f},{s}]").X >= 0.5:
                        driving.update(range(s, s + trips_duration_steps[f]))
            away_by_vehicle[m] = [t in driving for t in time_steps]
    return away_by_vehicle


def driver_wheel_hours(model, vehicles, time_steps, tag=''):
    """{vehicle: [hours actually driven in this step, ...]}, or None if unavailable.

    Taken from the drive_since_depot counter of 3.3.17b, which accumulates driving across
    an absence and is forced to zero whenever the vehicle is home. Differencing it step by
    step therefore gives the driving of each step, and summing that over an absence gives
    back the counter's own value at the end of it - so the roster's Lenkzeit and the
    model's are the same number by construction rather than by two implementations
    agreeing.

    The counter only exists when the crew rules were built (chaining on, mode crewed).
    None means "no better information than the block length", which is what the roster
    then falls back to.
    """
    if not vehicles:
        return None
    probe = model.getVarByName(f"drive_since_depot{tag}[{vehicles[0]},{time_steps[0]}]")
    if probe is None:
        return None
    per_vehicle = {}
    for m in vehicles:
        previous = 0.0
        steps = []
        for t in time_steps:
            var = model.getVarByName(f"drive_since_depot{tag}[{m},{t}]")
            counter = 0.0 if var is None else max(0.0, var.X)
            # negative at a depot step, where the counter is reset to zero
            steps.append(max(0.0, counter - previous) * STEP_HOURS)
            previous = counter
        per_vehicle[m] = steps
    return per_vehicle


# 5.1 subfunction for postprocessing
def postprocess(model, vehicles, vehicle_types, bev_vehicles, ice_vehicles, day_trips_list, fleet, trips, charging_infrastructure, write_outputs, auto_sizing, v2g_status_iteration, costs_v2g, penalty_charging_use, penalty_vehicle_use, penalty_charging_external_time, time_steps, locations, trip_distances, possible_start_times, trips_duration_steps, scenario_iterations, scenario_year_iterations, E_neg, y_m, cost_vehicle_100km, year_iteration, depot_buy_eur_per_kWh_t, public_charging_cost_eur_per_kWh, event_durations, event_possible_starts, virtual_trips, v2g_virtual_trips, chg_virtual_trips, toll_rate_per_km, pv_charging_available_kWh=None, pv_charging_eur_per_kWh_t=None, day_routing=None, approach_steps=None, return_steps=None, link_steps=None, dropped_trips=None, warm_start_used=False, charging_station_ids=None, trips_outside_work_hours=None, v2g_channel_at_step=None, costs_v2g_arbitrage=None, costs_v2g_flexibility=None, penalty_charging_block=None, tag='', plot_suffix=''):
    # the station list is positional in the model (station index si); its charger_id from
    # depot_dataset.xlsx is what a reader of the schedule recognises
    def station_label(index):
        if charging_station_ids and index < len(charging_station_ids):
            return charging_station_ids[index]
        return str(index)

    # 5.0b what every figure calls a vehicle: "ICE 1", "BEV 1", "BEV 2" ...
    #
    # Counted within the type and from one, not taken from vehicle_id. The roster is one
    # list of mixed types, so on a fleet of one diesel and ten batteries the ids run
    # ICE 1, BEV 2 ... BEV 11 - and a reader counting battery trucks in the figure finds
    # the tenth one labelled eleven. Numbering per type says how many of each there are,
    # which is the question a row label is actually asked.
    #
    # The one thing this costs is that a row label is no longer a vehicle_id. The
    # schedule CSV, the charger assignment and every per-vehicle metric still carry the
    # id, so "BEV 1" in a figure is vehicle 2 in the table beside it. Worth knowing when
    # matching one to the other; the mapping is this dict and nothing else builds one.
    row_labels_by_vehicle = {}
    _seen_of_type = {}
    for _index, _m in enumerate(vehicles):
        _kind = str(vehicle_types[_index]).upper()
        _seen_of_type[_kind] = _seen_of_type.get(_kind, 0) + 1
        row_labels_by_vehicle[_m] = f"{_kind} {_seen_of_type[_kind]}"

    # stations strongest first, carrying their position in the input list so the schedule
    # can still name them by charger_id
    stations_by_power = sorted(range(len(charging_infrastructure)),
                               key=lambda i: (-charging_infrastructure[i], i))

    def _station_assignment(t):
        """Which charger each truck stands at in step t, by the rule the model assumes.

        The model prices the fleet's draw against the k strongest stations (3.3.12) but
        never names one, so the assignment is reconstructed here: the trucks drawing in
        this step take the strongest stations, the heaviest draw first. Reproducing the
        rule rather than reading a variable is what keeps the schedule consistent with
        the capacity the optimisation was given.

        This is sound rather than merely plausible because 3.3.12(c) holds the solve to
        the tier rule: no more trucks draw above a tier's power than there are stations
        above it. That is Hall's condition on a nested structure, so the greedy pairing
        below always succeeds and never seats a truck on a station weaker than its own
        draw. Without (c) - as this used to be - the aggregate bound alone permitted
        splits that no assignment could realise, and the station named here would then
        have been a guess the model's own numbers contradicted.
        """
        drawing = []
        for veh in bev_vehicles:
            energy = model.getVarByName(f"E_private{tag}[{veh},{t}]").X
            if energy > ENERGY_TOLERANCE_KWH:
                drawing.append((energy, veh))
        drawing.sort(key=lambda pair: (-pair[0], pair[1]))
        return {veh: stations_by_power[rank] for rank, (_e, veh) in enumerate(drawing)
                if rank < len(stations_by_power)}

    _assignment_cache = {}

    def station_at_step(vehicle, t):
        if t not in _assignment_cache:
            _assignment_cache[t] = _station_assignment(t)
        return station_label(_assignment_cache[t].get(vehicle, 0))

    # Read the solution back whenever there is one, not only when the solver proved it
    # optimal. A run stopped by optimization_time_limit_s keeps its incumbent - every
    # variable below has a value and the schedule is a real schedule - and the design model
    # (3.4) hits that limit as a matter of course, being one MILP over several days. The
    # status travels into the result either way, so a reader can still tell a proven answer
    # from an interrupted one; throwing the incumbent away only lost the run.
    if model.status == gp.GRB.OPTIMAL or model.SolCount > 0:
        # 5.2 disposition schedule plot
        # 5.2.1 create disposition schedule dataframe
        schedule_rows = []
        # v43: real trips only for "trip" rows
        assigned_trip_starts = [
            (m, f, s)
            for m in vehicles
            for f in day_trips_list
            for s in event_possible_starts.get(f, [])
            if model.getVarByName(f"z_m_f_s{tag}[{m},{f},{s}]").X >= 0.5
        ]
        # v43 note: no internal charging_locations x; detection uses chg_virtual_trips z's above
        for m, f, s in assigned_trip_starts:
            dur = event_durations.get(f, trips_duration_steps.get(f, 1))
            for t in range(s, s + dur):
                if t in time_steps:
                    schedule_rows.append({'vehicle': m, 'time_step': t, 'activity': 'trip', 'trip_ID': f, 'location': None})

        # the empty legs (3.3.16f), as rows of their own. They are driving like any other
        # driving - they occupy the vehicle, spend its charge and take up the day - so the
        # schedule shows them beside the loaded trips rather than hiding them inside one.
        # The label says which trip the leg belongs to and which way it runs, because an
        # empty bar with no explanation is exactly what makes a plot unreadable.
        approach_steps = approach_steps or {}
        return_steps = return_steps or {}
        link_steps = link_steps or {}
        deadhead_rows = []
        if day_routing is not None:
            for m in vehicles:
                for f in day_trips_list:
                    for s in possible_start_times.get(f, []):
                        started = model.getVarByName(f"route_start{tag}[{m},{f},{s}]")
                        steps = approach_steps.get(f, 0)
                        if started is not None and started.X > 0.5 and steps > 0:
                            deadhead_rows.append((m, s - steps, steps, 'approach', f, None))
                        ended = model.getVarByName(f"route_end{tag}[{m},{f},{s}]")
                        steps = return_steps.get(f, 0)
                        if ended is not None and ended.X > 0.5 and steps > 0:
                            deadhead_rows.append(
                                (m, s + trips_duration_steps[f], steps, 'return', f, None))
                        for (pf, pg), leg_steps_count in link_steps.items():
                            if pf != f or leg_steps_count <= 0:
                                continue
                            linked = model.getVarByName(f"chain_from{tag}[{m},{f},{pg},{s}]")
                            if linked is not None and linked.X > 0.5:
                                deadhead_rows.append(
                                    (m, s + trips_duration_steps[f], leg_steps_count,
                                     'connection', f, pg))
        deadhead_steps = set()
        for m, first_step, steps, kind, f, partner in deadhead_rows:
            label = (f"{f}>{partner}" if partner is not None
                     else (f">{f}" if kind == 'approach' else f"{f}>"))
            for t in range(first_step, first_step + steps):
                if t in time_steps:
                    deadhead_steps.add((m, t))
                    schedule_rows.append({'vehicle': m, 'time_step': t,
                                          'activity': 'deadhead', 'trip_ID': label,
                                          'location': None})

        # 5.2.1b which charging steps are the *buy* half of an arbitrage round trip
        #
        # Arbitrage is a round trip - buy a kWh cheap, sell it dear - so showing only the
        # discharge as V2G tells half the story and makes the charging that funded it look
        # like ordinary charging for driving. Both halves are marked here.
        #
        # Which kWh funded the resale cannot be read off the model: charge in a battery is
        # fungible, and nothing in the MILP distinguishes an electron bought to drive on
        # from one bought to sell back. So this is an *attribution convention*, not a model
        # output, and it is the one arbitrage itself implies: the energy sold back is the
        # energy that was bought most cheaply. Per vehicle, take the metered kWh needed to
        # cover the day's arbitrage discharge - grossed up by both conversion efficiencies,
        # because a kWh sold has to be bought back with the losses on top - and attribute
        # it to that vehicle's cheapest charging steps until it is covered.
        #
        # The flexibility channel is deliberately left out. It is paid for a service rather
        # than for energy, so its discharge is not the second half of a purchase.
        arbitrage_charge_steps = set()
        if v2g_status_iteration == 'on' and v2g_channel_at_step is not None:
            for m in bev_vehicles:
                arbitrage_out_kWh = sum(
                    E_neg[m, t].X for t in time_steps
                    if v2g_channel_at_step[t] == 'arbitrage'
                    and E_neg[m, t].X > ENERGY_TOLERANCE_KWH)
                if arbitrage_out_kWh <= ENERGY_TOLERANCE_KWH:
                    continue
                # metered kWh that had to be bought to put that much back on the grid
                to_cover = arbitrage_out_kWh / (discharging_efficiency * charging_efficiency)
                charging_steps = []
                for t in time_steps:
                    private = model.getVarByName(f"E_private{tag}[{m},{t}]").X
                    public = model.getVarByName(f"E_public{tag}[{m},{t}]").X
                    if private + public <= ENERGY_TOLERANCE_KWH:
                        continue
                    price = ((depot_buy_eur_per_kWh_t[t] * private
                              + public_charging_cost_eur_per_kWh * public)
                             / (private + public))
                    charging_steps.append((price, t, private + public))
                for _price, t, energy in sorted(charging_steps):
                    if to_cover <= ENERGY_TOLERANCE_KWH:
                        break
                    arbitrage_charge_steps.add((m, t))
                    to_cover -= energy

        # v43: detect virtual V2G/CHG assignments (explicit trips) for activity classification; only external x remains for locs
        for m in vehicles:
            for t in time_steps:
                on_trip = any(
                    model.getVarByName(f"z_m_f_s{tag}[{m},{f},{s}]").X >= 0.5
                    for f in day_trips_list for s in event_possible_starts.get(f, [])
                    if s <= t < s + event_durations.get(f, 0)
                )
                # a step spent repositioning already has its row above, and must not also
                # be reported as parking or as a charging slot
                if on_trip or (m, t) in deadhead_steps:
                    continue
                # A virtual V2G/CHG event only *permits* an energy flow, it does not force
                # one, and an assignment that stays at zero costs the objective nothing.
                # The solver therefore leaves plenty of them set, and reporting a slot as
                # active on the strength of its z alone painted charging and discharging
                # blocks into the disposition figure that no kWh - and no SoC change in the
                # SoC figure - corresponds to. A step counts as active only if energy moved.
                discharged = charged = 0.0
                if m in bev_vehicles:
                    discharged = model.getVarByName(f"E_neg{tag}[{m},{t}]").X
                    charged = model.getVarByName(f"E_pos{tag}[{m},{t}]").X

                # check V2G virtual trip at this exact slot
                activity = 'parking'
                loc = 'parking_lot'
                vtg = f'V2G_t{t}'
                if vtg in v2g_virtual_trips and discharged > ENERGY_TOLERANCE_KWH:
                    zv = model.getVarByName(f"z_m_f_s{tag}[{m},{vtg},{t}]")
                    if zv and zv.X >= 0.5:
                        # the sell half of V2G, labelled with the channel it settled in
                        channel = ('arbitrage' if v2g_channel_at_step is None
                                   else v2g_channel_at_step[t])
                        schedule_rows.append({
                            'vehicle': m, 'time_step': t, 'activity': 'v2g_discharge',
                            'location': f'v2g_{channel}', 'trip_ID': None})
                        continue
                # plugged in at the depot and actually drawing. The station it stands at
                # is not a model variable any more - station_at_step() applies the rule
                # the model was built on: strongest free station first.
                found_chg = False
                if charged > ENERGY_TOLERANCE_KWH:
                    zc = model.getVarByName(f"z_m_f_s{tag}[{m},CHG_t{t},{t}]")
                    if zc and zc.X >= 0.5:
                        # charging that funds an arbitrage resale is the other half of a
                        # V2G round trip, not charging for driving, and is shown as such
                        arbitrage_buy = (m, t) in arbitrage_charge_steps
                        schedule_rows.append({
                            'vehicle': m, 'time_step': t,
                            'activity': 'v2g_charge' if arbitrage_buy else 'charging_in',
                            'location': f'charging_station_{station_at_step(m, t)}',
                            'trip_ID': None})
                        found_chg = True
                if found_chg:
                    continue
                # external or pure parking
                if 'external_charging' in locations:
                    extv = model.getVarByName(f"x_m_t_l{tag}[{m},{t},external_charging]")
                    if extv and extv.X >= 0.5:
                        activity = 'external_charging'
                        loc = 'external_charging'
                        schedule_rows.append({'vehicle': m, 'time_step': t, 'activity': activity, 'location': loc, 'trip_ID': None})
                        continue
                # default idle parking (no event, no external x). Standing at the home
                # depot and standing in a customer yard look identical in a schedule and
                # are not the same thing at all: only the first has chargers and a V2G
                # connection. They are separate activities so the plot cannot suggest a
                # truck could have charged during a wait it spent 200 km from home.
                home = model.getVarByName(f"at_depot{tag}[{m},{t}]")
                idle = 'parking' if home is None or home.X > 0.5 else 'standby_away'
                schedule_rows.append({'vehicle': m, 'time_step': t, 'activity': idle, 'location': loc, 'trip_ID': None})
        schedule_df = pd.DataFrame(schedule_rows)

        # 5.2.0b where a charged kWh came from, per step
        #
        # Depot charging is one pool per half hour and three sources fill it: the site's own
        # PV, a truck discharging into another truck (V2V, 3.3.15c), and the grid, which is
        # whatever is left. The first two are decided by the model, the third is the residual
        # - the same arithmetic the energy accounting in 5.5 does, hoisted here because the
        # disposition figure is drawn long before that runs and both must say the same thing.
        #
        # PER STEP AND FLEET-WIDE, NOT PER TRUCK. E_pv_charging and E_v2v are indexed by time
        # only: the model decides how much of a step's depot charging the sun covered, never
        # which truck stood in the sun. Two trucks charging in the same half hour draw from
        # one pool and no attribution between them exists to be read off. So the cell code
        # names the source that supplied the largest share of that step, and every truck
        # charging in it carries the same letter - which is the honest reading. A step that
        # is 60 % PV and 40 % grid shows PC for both trucks; it does not mean one of them ran
        # on sunlight alone.
        pv_charging_per_step = {t: model.getVarByName(f"E_pv_charging_{t}{tag}").X
                                for t in time_steps}
        depot_charging_per_step = {
            t: sum(model.getVarByName(f"E_private{tag}[{m},{t}]").X for m in bev_vehicles)
            for t in time_steps}
        # Recomputed from the flows rather than read off E_v2v, so the figure is exactly
        # min(charging, discharging) net of what the sun already covered, whatever pressure
        # the objective did or did not put on the variable - with both overheads at zero the
        # term is worth nothing and the solver has no reason to push it anywhere.
        v2v_per_step = {}
        for t in time_steps:
            discharged_t = sum(E_neg[m, t].X for m in bev_vehicles)
            local_room = max(0.0, depot_charging_per_step[t] - pv_charging_per_step[t])
            v2v_per_step[t] = (min(local_room, discharged_t)
                               if v2v_status == 'on' else 0.0)
        # the grid is the residual, and max() rather than the bare subtraction: the three
        # are read back from a solved model at solver tolerance, so a step the sun covered
        # exactly can land a few micro-kWh negative and must read as zero, not as a source.
        grid_charging_per_step = {
            t: max(0.0, depot_charging_per_step[t] - pv_charging_per_step[t]
                   - v2v_per_step[t])
            for t in time_steps}
        # PC / VC / GC - the most valuable source that supplied a real share of the step.
        #
        # Largest share alone was the first rule here and it is the wrong one, for a reason
        # the data makes plain: PV is the smallest of the three in every run measured - 658
        # kWh against 4,043 of V2V and 6,230 of grid on one day, 1,175 against 13,143 on
        # another - so it never wins a step and PC never appears. A legend that names three
        # sources and can only ever draw two answers the question worse than not asking it.
        #
        # So the order is the depot's own merit order - its own sunlight first, then energy
        # that crossed the yard instead of the meter, then the metered import that is left -
        # and the first source present above tolerance takes the cell. Any PV at all makes
        # the step PC.
        #
        # That is a deliberate bias towards the scarce source and not an accident of it. A
        # share floor sat here first, at a fifth, on the reasoning that a step which is 99 %
        # grid should not read PC. Measured, the floor answered the wrong question: PV runs
        # about 9 % of a charging step on this depot and 49 % at its best, so a fifth hid it
        # in 24 of 25 steps and the figure could not show the thing it was built to show.
        # Whether the sun contributed at all is the question worth a cell; how much is a
        # question a two-character code cannot answer at any threshold.
        #
        # So read PC as "some of this step was PV", not "this step ran on PV". The cell says
        # which source, never how much - `pv_charging_kWh`, `v2v_kWh` and `grid_charging_kWh`
        # in the run summary are the quantities, and the depot power figure is where the
        # per-step mix can actually be seen.
        depot_charging_source_code = {}
        for t in time_steps:
            merit = (('PC', pv_charging_per_step[t]),
                     ('VC', v2v_per_step[t]),
                     ('GC', grid_charging_per_step[t]))
            present = [code for code, kWh in merit if kWh > ENERGY_TOLERANCE_KWH]
            # nothing drawn in this step, or nothing above noise: no source to name, so a
            # cell that somehow lands here keeps the generic code rather than claiming one
            depot_charging_source_code[t] = present[0] if present else 'DC'

        if write_outputs:
            plt, Patch = _pyplot()
            # the activity colours. They are the grid's palette as much as the bars', so
            # they stay out here where both can reach them and neither can drift from the
            # other; the hatches below are the bars' alone, since a grid cell has none.
            colors = {
                'trip': 'black', 'charging_in': 'tab:green', 'v2g_discharge': 'tab:red',
                # both kinds of charging are green, because both are energy going into a
                # battery; the dark one is the depot's own and the mid one is bought at
                # somebody else's station. Two shades of one hue say "same thing, different
                # place", which orange against green did not.
                'external_charging': 'darkgreen', 'parking': 'lightgrey',
                'charging': 'tab:green', 'v2g': 'tab:red',
                # empty running: the same blue as a loaded trip because it is the same
                # driving, lightened and hatched because it carries nothing
                # both halves of a V2G round trip read as V2G, but they are not the same
                # event: the lighter red buys, the strong red sells. Hatch alone was not
                # enough to tell them apart at a glance in a full day's figure.
                'v2g_charge': 'lightcoral',
                # empty running is grey, not a pale blue: it is the one activity that
                # earns nothing and costs fuel, toll and battery age, and a dark neutral
                # says "overhead" where a light tint of the loaded-trip colour said
                # "nearly a trip". It also keeps the two driving states apart in the grid,
                # where a pale blue sat between the parking greys and the loaded black.
                'deadhead': 'dimgray',
                # waiting away from home: idle like parking, but with no charger and no
                # V2G connection within reach. A cooler grey than parking rather than a
                # neighbouring one - in the bars the hatch tells them apart, but the grid
                # has no hatch and two all but identical greys make its legend useless
                'standby_away': '#AEB6BD',
            }
            hatches = {
                'trip': '', 'charging_in': '//', 'v2g_discharge': 'xx',
                'external_charging': '..', 'parking': '', 'charging': '//', 'v2g': 'xx',
                'v2g_charge': '//',
                'deadhead': '\\', 'standby_away': '..',
            }

        # 5.2.2 group consecutive time slots with same activity for plot
        #       Only the bar figure needs the runs; the grid is drawn per step from
        #       schedule_df directly, so with the bars off none of this has to happen.

        # ... and the day as a grid (src/hdv_grid_plots.py), which is how it is drawn now.
        # Bars are read along a row, one truck at a time; the grid is read down a column
        # too, which is the only way to see what the whole fleet was doing at 13:00 at once.
        #
        # The colour is the coarse state and carries the same meaning the bars give it, so
        # a run with the bars switched back on cannot disagree with this. The two characters
        # in the cell carry what the colour deliberately leaves out: which trip, and for a
        # V2G step which channel it settled in and which way the energy went.
        if write_outputs:
            import hdv_grid_plots as grid_plots

            # Ordered for the legend, which reads down its columns and not across them
            # (grid_categories lays it out column-major). Four columns of two puts each
            # state next to the one it is most easily confused with - the two ways of
            # charging, the two directions of V2G, the two places a truck can stand idle -
            # so the pair is read as a pair rather than hunted for along a row.
            disposition_palette = {
                'trip': (colors['trip'], 'order driving (trip n)'),
                'deadhead': (colors['deadhead'],
                             'empty driving (>n to trip n, n> to depot)'),
                # one colour, three codes: the bar is "this truck was taking energy in at
                # the depot" either way, and which source filled the pool is the detail the
                # cell carries - the same division of labour the V2G entry below uses
                'charging_in': (colors['charging_in'],
                                'depot charging (V2V=VC, grid=GC, PV=PC)'),
                'external_charging': (colors['external_charging'],
                                      'external charging (EC)'),
                'v2g_discharge': (colors['v2g_discharge'],
                                  'V2G discharging (arbitrage=AD, flexibility=FD)'),
                'v2g_charge': (colors['v2g_charge'], 'V2G charging (arbitrage=AC)'),
                'parking': (colors['parking'], 'depot parking (P)'),
                'standby_away': (colors['standby_away'], 'external depot parking (EP)'),
            }
            # two characters each, which is what a cell holds. The codes say where as well
            # as what - D for the depot, E for away from it - so a row can be read for
            # where the truck stood without going back to the legend for every cell.
            # charging_in is deliberately absent: its code is the energy source of that
            # step (5.2.0b) rather than a fixed string, and falling back to 'DC' is what
            # the source table already does when nothing identifiable was drawn
            plain_codes = {'v2g_charge': 'AC',
                           'external_charging': 'EC', 'parking': 'P',
                           'standby_away': 'EP'}

            def cell_code(activity, location, trip_id, step):
                """Two characters at most - the detail the colour does not carry."""
                if activity == 'trip':
                    return '' if trip_id is None else str(int(trip_id))
                if activity == 'deadhead':
                    # '>7' approaching trip 7, '7>' home from it, '7>9' the run between two
                    # chained trips - the last shortened to '>9', since where an empty leg
                    # is heading is what the cell is for and it has room for two characters
                    label = '' if trip_id is None else str(trip_id)
                    if len(label) > 2 and label.count('>') == 1:
                        head, tail = label.split('>')
                        return '>' + tail if tail else head + '>'
                    return label
                if activity == 'v2g_discharge':
                    return 'FD' if str(location).endswith('flexibility') else 'AD'
                if activity == 'charging_in':
                    # PC / VC / GC by the step's largest share, or DC when the pool holds
                    # nothing identifiable. Fleet-wide per step, so every truck charging in
                    # this half hour reads the same - see 5.2.0b for why that is the only
                    # attribution the model supports.
                    return depot_charging_source_code.get(step, 'DC')
                return plain_codes.get(activity, '')

            # a step could pick up more than one row - an empty leg that butts against the
            # trip it serves, say - so the state is decided by rank and not by which row the
            # frame happens to hold last. Driving outranks standing, and moving energy
            # outranks standing idle: the more specific claim is the one worth drawing.
            state_rank = {'parking': 0, 'standby_away': 1, 'external_charging': 2,
                          'charging_in': 3, 'v2g_charge': 4, 'v2g_discharge': 5,
                          'deadhead': 6, 'trip': 7}
            row_of = {m: i for i, m in enumerate(vehicles)}
            states = [[None] * len(time_steps) for _ in vehicles]
            cell_text = [[''] * len(time_steps) for _ in vehicles]
            for entry in schedule_df.itertuples():
                row = row_of.get(entry.vehicle)
                step = int(entry.time_step)
                if row is None or step not in time_steps:
                    continue
                held = states[row][step]
                if held is not None and state_rank.get(held, -1) >= state_rank.get(entry.activity, -1):
                    continue
                states[row][step] = entry.activity
                cell_text[row][step] = cell_code(entry.activity, entry.location,
                                                 entry.trip_ID, step)

            grid_plots.grid_categories(
                states, cell_text,
                # the same name every figure of the run gives the vehicle (5.0b)
                [row_labels_by_vehicle[m] for m in vehicles],
                project_path('results', output_filename(f'plot_disposition_schedule{plot_suffix}.png')),
                palette=disposition_palette, step_hours=STEP_HOURS,
                # four, not the default five: eight states over five columns leaves three
                # columns of two and two of one, which breaks the pairing the order above
                # exists for. Four gives two rows in every column.
                legend_columns=4, keep_open=displays_outputs(),
                title='Disposition Schedule (vehicle activity per 30-min step)',
                row_axis_label='Vehicle', plt=plt)

            if plot_run_cost_parameters:
                from hdv_figure_style import HOUR_TICK_STRIDE, TIME_AXIS_LABEL, hhmm

                hours = np.arange(len(time_steps), dtype=float) * STEP_HOURS
                fig_cost, axis_cost = plt.subplots(figsize=(11, 6.5))
                axis_cost.step(
                    hours, depot_buy_eur_per_kWh_t,
                    where='post', color='tab:blue', linewidth=2,
                    label='grid buying price')
                axis_cost.step(
                    hours, pv_charging_eur_per_kWh_t,
                    where='post', color='tab:orange', linewidth=2,
                    label='Grid selling price')
                axis_cost.step(
                    hours, [price / 1000.0 for price in costs_v2g_flexibility],
                    where='post', color='tab:red',
                    linewidth=2, label='Flexibility price')
                axis_cost.axhline(
                    public_charging_cost_eur_per_kWh, color='tab:purple',
                    linestyle='--', linewidth=1.5,
                    label='Public charging price')
                axis_cost.set_title(
                    f'Cost parameters used by the run - {scenario_iterations}, '
                    f'{scenario_year_iterations}')
                axis_cost.set_xlabel(TIME_AXIS_LABEL)
                axis_cost.set_ylabel('Price [€/kWh]')
                axis_cost.set_xlim(0, 24)
                # every two hours, the stride every other time axis of the project uses -
                # this one was on four and so could not be read against the schedule grids
                _cost_ticks = list(range(0, 25, HOUR_TICK_STRIDE))
                axis_cost.set_xticks(_cost_ticks)
                axis_cost.set_xticklabels([hhmm(hour) for hour in _cost_ticks])
                axis_cost.grid(True, alpha=0.3)
                axis_cost.legend(loc='best', fontsize='small')
                plt.tight_layout()
                plt.savefig(
                    project_path('results', output_filename('plot_cost_parameter.png')),
                    bbox_inches='tight', dpi=FIGURE_DPI)
                release_figure(plt, fig_cost)

        # 5.3 calculation of total-km, bev-km, ice-km driven, amount of needed vehicles
        total_bev_km = sum(
            trip_distances[f]
            for m, f, _ in assigned_trip_starts if m in bev_vehicles
        )
        total_km = sum(trip_distances.values())
        electrified_km_percentage = (total_bev_km / total_km) * 100 if total_km > 0 else 0
        # A vehicle counts as used when it drove a trip, and only then. assigned_trip_starts
        # holds the real trips alone - the V2G and charging "virtual trips" are not in
        # day_trips_list - so a truck that spent the day on a charger selling energy back is
        # not counted here. It moved energy; it did not do any of the work the fleet exists
        # for, and a fleet size that counts it answers a question nobody asked.
        #
        # The model agrees: y_m is raised by real trips only (3.3.10), so this count and the
        # objective's notion of a used vehicle are the same thing read two ways.
        trip_driving_vehicles = {m for m, _f, _s in assigned_trip_starts}
        used_ice = sum(1 for m in ice_vehicles if m in trip_driving_vehicles)
        used_bev = sum(1 for m in bev_vehicles if m in trip_driving_vehicles)
        used_bev_ids = [m for m in bev_vehicles if m in trip_driving_vehicles]
        used_bev_battery = fleet.loc[fleet['vehicle_id'].isin(used_bev_ids), 'vehicle_energy_storage'].astype(float).tolist()
        # Trucks that moved energy without driving anything. They cost nothing in the
        # objective now that y_m ignores V2X, so the run has no reason not to keep them -
        # which makes them exactly the thing a reader of a fleet size needs to be told
        # about: they are not in the count above, and they would still have to be bought.
        energy_moving_vehicles = {
            m for m in bev_vehicles
            if any(max(model.getVarByName(f"E_pos{tag}[{m},{t}]").X,
                       model.getVarByName(f"E_neg{tag}[{m},{t}]").X) > ENERGY_TOLERANCE_KWH
                   for t in time_steps)
        }
        v2g_only_vehicles = sorted(energy_moving_vehicles - trip_driving_vehicles)

        # 5.4 idle time, V2G-time, and V2G-cost calculation, V2G duration = steps with bev, E_neg>0, and "parking"
        idle_times = []
        for m in vehicles:
            if m in bev_vehicles:
                v2g_steps = [t for t in time_steps if E_neg[m, t].X > 1e-6]
                hours = len(v2g_steps) * STEP_HOURS
            else:
                hours = 0.0
            idle_times.append(hours)

        # How much of a step's discharge reached the grid, and so earned (3.3.15c). E_v2v
        # is decided for the fleet and not per truck - the yard has one busbar and the
        # model never says which truck's kWh went next door - so the step's V2V is
        # attributed pro rata to what each truck discharged into it. Any split of a
        # fleet-level quantity is a convention; this is the one that leaves every truck
        # with the same to-grid fraction, which is what "the busbar mixes them" means.
        #
        # Every earnings figure below goes through it. The one that matters most is
        # v2g_earnings_all_total, because that is what the cost reconciliation reads: left
        # gross while the objective paid net, it showed up as an unexplained residual of
        # exactly the V2V revenue.
        def to_grid_share(step):
            discharged = sum(E_neg[m, step].X for m in bev_vehicles)
            if discharged <= ENERGY_TOLERANCE_KWH:
                return 0.0
            return max(0.0, 1.0 - v2v_per_step.get(step, 0.0) / discharged)

        to_grid_factor = {t: to_grid_share(t) for t in time_steps}

        v2g_earnings_all = [] # for full fleet V2G utilisation
        for m in vehicles:
            if m in bev_vehicles:
                v2g_earnings_all_single = sum(
                    (costs_v2g[t] / 1000.0) * E_neg[m, t].X * to_grid_factor[t]
                    for t in time_steps)
            else:
                v2g_earnings_all_single = 0.0
            v2g_earnings_all.append(v2g_earnings_all_single)
        v2g_earnings_all_total = float(sum(v2g_earnings_all))
        
        # for auto fleet sizing: the earnings of the fleet the run reports, which is the
        # fleet that drove. What that leaves out is reported beside it rather than dropped
        # silently - with y_m raised by trips alone, a truck can now earn on the spread all
        # day without appearing in the fleet size, and the difference between the two
        # figures is exactly how much of the V2G revenue such trucks brought in.
        v2g_earnings_used = []
        for m in vehicles:
            if m in used_bev_ids:
                v2g_earnings_used_single = sum(
                    (costs_v2g[t] / 1000.0) * E_neg[m, t].X * to_grid_factor[t]
                    for t in time_steps)
            else:
                v2g_earnings_used_single = 0.0
            v2g_earnings_used.append(v2g_earnings_used_single)
        v2g_earnings_used_total = float(sum(v2g_earnings_used))
        v2g_earnings_non_driving = float(sum(
            (costs_v2g[t] / 1000.0) * E_neg[m, t].X * to_grid_factor[t]
            for m in v2g_only_vehicles for t in time_steps))

        # which channel each kWh was actually sold into. costs_v2g already carries the
        # better of the two per step, so the split is a matter of attributing the earnings
        # that were booked, not of re-pricing them.
        v2g_earnings_by_channel = {'arbitrage': 0.0, 'flexibility': 0.0}
        v2g_energy_by_channel = {'arbitrage': 0.0, 'flexibility': 0.0}
        if v2g_channel_at_step is not None:
            for t in time_steps:
                # what left for the grid, not what left the batteries: the V2V share was
                # never offered to either channel
                discharged = (sum(E_neg[m, t].X for m in bev_vehicles)
                              * to_grid_factor[t])
                if discharged <= ENERGY_TOLERANCE_KWH:
                    continue
                channel = v2g_channel_at_step[t]
                v2g_energy_by_channel[channel] += discharged
                v2g_earnings_by_channel[channel] += (costs_v2g[t] / 1000.0) * discharged


        # 5.5 total energy costs calculation (now actual: ice fuel + bev private/public charging energy from cost_parameters_energy)
        #
        # What the solver proved, in three words rather than one.
        #
        # GRB.OPTIMAL does not mean "the cheapest schedule there is". It means "the gap I
        # was asked to close is closed", and this model asks for optimization_MIPGap, which
        # defaults to 10 %. Reporting that as a bare "optimal" was the most misread figure
        # the run produced: a cost up to a tenth above the best bound, in a column headed
        # with the word for a proof. The terminal printed the achieved gap next to it, but
        # only on a run that writes its report, and the result dict - which is what a
        # sweep CSV and every plot downstream actually read - carried the word alone.
        #
        # So the three outcomes a solved model can have are now named apart, and the gap
        # that separates them travels with them in the result:
        #
        #   optimal      proved - the gap really did close to nothing
        #   gap-optimal  accepted at optimization_MIPGap, still that far from the bound
        #   incumbent    the clock ran out; a real schedule, no proof of anything
        #
        # MIPGap is only defined once there is an incumbent, and can come back as infinity
        # on a model with no bound yet, so it is read defensively.
        try:
            achieved_gap = float(model.MIPGap)
            if not math.isfinite(achieved_gap):
                achieved_gap = None
        except (AttributeError, gp.GurobiError):
            achieved_gap = None
        if model.status != gp.GRB.OPTIMAL:
            optimization_status = 'incumbent'
        elif achieved_gap is None or achieved_gap <= MIP_GAP_PROVEN:
            optimization_status = 'optimal'
        else:
            optimization_status = 'gap-optimal'
        # vehicle-steps in which a bev actually took energy, depot or external. Counted on
        # E_pos (= E_private + E_public) rather than on the CHG/external assignments: an
        # assignment only permits a flow, so counting those overstates the charging exactly
        # as it used to overstate it in the schedule (see 5.2.2).
        total_charging_steps    = sum(
            1 for m in bev_vehicles for t in time_steps
            if model.getVarByName(f"E_pos{tag}[{m},{t}]").X > ENERGY_TOLERANCE_KWH)
        # DSO demand charge on the daily maximum grid draw at the depot.
        # Recomputed from the primitives rather than read from the site_peak_kW variable:
        # when the demand charge is 0 nothing pushes that variable down to the true peak.
        site_peak_kW_value = 0.0
        for t in time_steps:
            depot_charging_kW = sum(
                model.getVarByName(f"E_private{tag}[{m},{t}]").X for m in bev_vehicles) / STEP_HOURS
            v2g_kW = sum(E_neg[m, t].X for m in bev_vehicles) / STEP_HOURS
            grid_draw_kW = (depot_baseline_load_kW[t] + depot_charging_kW
                            - v2g_kW - pv_generation_kW[t])
            site_peak_kW_value = max(site_peak_kW_value, grid_draw_kW)
        # the same counterfactual the objective is charged against (3.3.15): what the site
        # would peak at with no bev, so only the increment is attributed to the fleet.
        # Negative when V2G shaves the peak below it - a real saving, reported as one.
        baseline_peak_kW_value = max(
            [max(0.0, depot_baseline_load_kW[t] - pv_generation_kW[t]) for t in time_steps] + [0.0])
        bev_peak_increment_kW = site_peak_kW_value - baseline_peak_kW_value
        demand_charge_eur = (peak_power_price_eur_per_kW / 365.0) * bev_peak_increment_kW

        # actual energy purchase costs (replaces previous proxy back-calc)
        # diesel is billed per assigned trip with the truck's own l/100km, so trucks with
        # different consumptions are priced differently instead of at a fleet average.
        # This is the loaded distance only; the empty running is added in 5.5d, once the
        # routes have been read back and the deadhead kilometres are known per truck.
        ice_energy_cost = sum(
            cost_vehicle_100km[m] * 0.01 * trip_distances.get(f, 0.0)
            for m, f, _s in assigned_trip_starts if m in ice_vehicles
        )
        # bev charging is billed on the energy split the model decided on, exactly as the
        # objective prices it: depot from the grid, external, and the share the site's own
        # PV plant covered
        # pv_charging_per_step / depot_charging_per_step are built in 5.2.0b, which the
        # disposition figure needs long before this runs. One definition, not two.
        pv_charging_kWh = sum(pv_charging_per_step.values())
        # the same opportunity curve the objective was built from (1.4): with the parameter
        # at None it is the spot price of that step, so a PV kWh is billed exactly like a
        # grid kWh and the plant shows no energy-cost advantage - only the peak relief
        pv_energy_cost = sum(pv_charging_eur_per_kWh_t[t] * pv_charging_per_step[t]
                             for t in time_steps)
        # volume-weighted, for the print and the result row
        pv_price_avg = (pv_energy_cost / pv_charging_kWh
                        if pv_charging_kWh > ENERGY_TOLERANCE_KWH else 0.0)
        # what those kWh would have cost bought from the grid instead. The spot term is in
        # both prices and cancels, so this is exactly the sum of the two overheads per kWh
        pv_energy_saving_eur = sum(
            (depot_buy_eur_per_kWh_t[t] - pv_charging_eur_per_kWh_t[t]) * pv_charging_per_step[t]
            for t in time_steps)
        depot_charging_kWh = sum(depot_charging_per_step.values())
        external_charging_kWh = sum(model.getVarByName(f"E_public{tag}[{m},{t}]").X
                                    for m in bev_vehicles for t in time_steps)
        grid_charging_kWh = depot_charging_kWh - pv_charging_kWh
        # the depot tariff follows the intraday curve, so the grid part of the bill is
        # summed step by step at the price of that step rather than at a daily average -
        # charging into the cheap midday hours is the point of the arbitrage channel, and
        # an average price would hide exactly that
        grid_charging_cost = sum(
            depot_buy_eur_per_kWh_t[t] * (depot_charging_per_step[t] - pv_charging_per_step[t])
            for t in time_steps)
        bev_actual_energy_cost = (grid_charging_cost
                                  + public_charging_cost_eur_per_kWh * external_charging_kWh
                                  + pv_energy_cost)
        # volume-weighted, purely for the print below
        grid_charging_price_avg = (grid_charging_cost / grid_charging_kWh
                                   if grid_charging_kWh > ENERGY_TOLERANCE_KWH else 0.0)
        external_charging_cost = public_charging_cost_eur_per_kWh * external_charging_kWh
        # conversion losses (1.4a, 3.3.11c). Every charging figure above is metered energy
        # and every V2G figure is metered energy, so the losses are not in any of them -
        # they are the gap between the two frames and have to be stated separately or the
        # day's energy does not balance. The paid-for share of the loss is the whole point:
        # the depot buys metered_charging but only stores eta_ch of it, and it sells
        # v2g_kWh_metered while the pack gives up v2g_kWh_metered/eta_dis.
        # 5.5b vehicle-to-vehicle (3.3.15c). v2v_per_step is built in 5.2.0b, beside the
        # PV and grid shares it is derived with - the disposition figure labels its
        # charging cells from the same three numbers, and a second copy here would be a
        # second answer to "where did that kWh come from".
        v2v_kWh = sum(v2v_per_step.values())
        # What those kWh would have cost had they been bought - the whole price, because
        # the charging term bills them and they never crossed the meter (3.3.15c). This is
        # the objective's v2v_saving exactly, so the reconciliation below closes on it.
        v2v_saved_total = sum(depot_buy_eur_per_kWh_t[t] * v2v_per_step[t]
                              for t in time_steps)
        # the grid fees inside that price, kept because avoided fees are the part an
        # operator recognises; the rest of the saving is the energy itself at spot
        v2v_saved_grid_fees = grid_energy_overhead_eur_per_kWh * v2v_kWh
        # nothing is sold on a V2V kWh, so there are no selling fees to avoid. What the
        # transfer gives up is the V2G revenue itself, which is a forgone earning rather
        # than a saving and is reported as one - it is already out of v2g_earnings above.
        v2v_saved_selling_fees = 0.0
        v2v_forgone_v2g = sum((costs_v2g[t] / 1000.0) * v2v_per_step[t]
                              for t in time_steps)
        v2v_steps = sum(1 for value in v2v_per_step.values() if value > ENERGY_TOLERANCE_KWH)

        metered_charging_kWh = depot_charging_kWh + external_charging_kWh
        charging_loss_kWh = (1.0 - charging_efficiency) * metered_charging_kWh
        v2g_discharged_metered_kWh = sum(E_neg[m, t].X for m in bev_vehicles for t in time_steps)
        discharging_loss_kWh = v2g_discharged_metered_kWh * (1.0 / discharging_efficiency - 1.0)
        # what the cells actually saw, the figure the degradation in 5.8 is charged on
        battery_charged_kWh = metered_charging_kWh - charging_loss_kWh
        battery_discharged_kWh = v2g_discharged_metered_kWh + discharging_loss_kWh
        # priced at what that energy would have cost: the loss on the charging side is
        # bought at the same tariff as the rest, the loss on the V2G side is charge that
        # has to be bought back. Reported, not added - the objective already pays for it
        # through the metered quantities.
        avg_charging_price = (bev_actual_energy_cost / metered_charging_kWh
                              if metered_charging_kWh > ENERGY_TOLERANCE_KWH else 0.0)
        conversion_loss_cost = avg_charging_price * (charging_loss_kWh + discharging_loss_kWh)

        # 5.5b the routes the day was actually run as (3.3.16). Empty running is the price
        # of geography: a truck that finishes away from home has to get back, and every one
        # of those kilometres is fuel or charge that carries no freight. The share of it
        # against the loaded distance is the number worth watching - it is what chaining
        # exists to bring down.
        deadhead_approach_km = deadhead_return_km = deadhead_chain_km = 0.0
        routes_driven = chains_used = direct_chains_used = 0
        depot_steps_share = None
        # ... and the same kilometres split by truck, because they have to be *priced* and
        # not only counted. Diesel is billed at each vehicle's own l/100 km and the toll at
        # its own rate per km, so a fleet total cannot do it (5.5d).
        deadhead_km_by_vehicle = {m: 0.0 for m in vehicles}
        if day_routing is not None:
            for m in vehicles:
                for f in day_trips_list:
                    for s in possible_start_times.get(f, []):
                        started = model.getVarByName(f"route_start{tag}[{m},{f},{s}]")
                        if started is not None and started.X > 0.5:
                            routes_driven += 1
                            leg_km = day_routing.approach.get(f, (0.0, 0.0))[0]
                            deadhead_approach_km += leg_km
                            deadhead_km_by_vehicle[m] += leg_km
                        ended = model.getVarByName(f"route_end{tag}[{m},{f},{s}]")
                        if ended is not None and ended.X > 0.5:
                            leg_km = day_routing.ret.get(f, (0.0, 0.0))[0]
                            deadhead_return_km += leg_km
                            deadhead_km_by_vehicle[m] += leg_km
                    for (pf, pg), (km, _hours) in day_routing.links.items():
                        if pf != f:
                            continue
                        linked = model.getVarByName(f"chain{tag}[{m},{f},{pg}]")
                        if linked is not None and linked.X > 0.5:
                            chains_used += 1
                            deadhead_chain_km += km
                            deadhead_km_by_vehicle[m] += km
                            if km <= 0.0:
                                direct_chains_used += 1
            # how much of the fleet's day is spent standing at home, which is the window
            # depot charging and V2G actually had available to them
            depot_steps = sum(model.getVarByName(f"at_depot{tag}[{m},{t}]").X
                              for m in vehicles for t in time_steps)
            depot_steps_share = depot_steps / max(1, len(vehicles) * len(time_steps))
        deadhead_km = deadhead_approach_km + deadhead_return_km + deadhead_chain_km

        # 5.5c drivers (1.4c). A driver is tied to a vehicle exactly while it is away from
        # the home depot, so the roster is built from that and nothing else.
        #
        # Which steps count as "away" depends on how much geography the run had. With
        # route chaining on, at_depot says it outright and the block therefore covers the
        # empty legs and any waiting in a customer yard - all of it time a driver cannot
        # leave the truck. With chaining off there is no such state, and the best available
        # reading is the loaded driving itself; that *understates* the roster, because the
        # repositioning and waiting it cannot see still needed somebody.
        import hdv_driver_scheduling as driver_scheduling

        away_by_vehicle = driver_away_flags(model, vehicles, time_steps, day_trips_list,
                                            possible_start_times, trips_duration_steps,
                                            day_routing, tag)
        # ... and the driving inside those absences, which the roster needs to hold a
        # driver to the Lenkzeit. Read off drive_since_depot (3.3.17b) rather than
        # recomputed: that counter *is* the model's own accounting of driving since the
        # last time the vehicle was home, so what the roster checks is exactly what the
        # optimisation charged.
        wheel_hours_by_vehicle = driver_wheel_hours(model, vehicles, time_steps, tag)
        if fleet_operation_mode == 'crewed':
            roster = driver_scheduling.schedule_drivers(
                away_by_vehicle, STEP_HOURS, driver_max_shift_hours, driver_hourly_rate_eur,
                max_working_hours=driver_max_working_hours,
                max_driving_hours=driver_max_driving_hours,
                wheel_hours_by_vehicle=wheel_hours_by_vehicle)
        else:
            # an autonomous fleet is rostered with nobody. The duty blocks are still built,
            # because "how long is a vehicle away in one stretch" stays a useful figure and
            # is the direct comparison against what a crewed run could have managed - but
            # no driver is assigned to them and nothing is paid.
            blocks = driver_scheduling.build_duty_blocks(
                away_by_vehicle, STEP_HOURS,
                wheel_hours_by_vehicle=wheel_hours_by_vehicle)
            roster = {'drivers': [], 'blocks': blocks, 'over_shift': [],
                      'over_driving': [],
                      'longest_block_h': max((b.hours for b in blocks), default=0.0),
                      'longest_block_wheel_h': max((b.wheel_hours for b in blocks),
                                                   default=0.0),
                      'driver_count': 0, 'driver_lower_bound': 0, 'paid_hours': 0.0,
                      'driving_hours': sum(b.hours for b in blocks),
                      'wheel_hours': sum(b.wheel_hours for b in blocks),
                      'max_driver_wheel_h': 0.0, 'break_hours': 0.0,
                      'cost_eur': 0.0, 'vehicle_changes': 0}

        # what the optimizer itself had to give up on (3.3.17). The driving limits are
        # priced rather than hard, so a run is only legal if these come back at zero - and
        # where they do not, the trips involved cannot be crewed from this depot in a day at
        # all. Three slack families, not two: break_excess (3.3.17 c-ii) is the driving
        # pushed past the point a 45-minute stop was due, and it is priced at the same
        # penalty_crew_rule_breach as the other two. Counting only the first two left its
        # euros inside ObjVal with nothing in the report to explain them - they surfaced as
        # objective_residual_€ (5.8b), which is the accounting saying it has lost track
        # rather than anything about the schedule.
        #
        # shift_excess is still read, and under the default driver_shift_limit = 'hard' it
        # simply does not exist - the constraint carries no slack, so there is nothing to
        # sum and the term contributes zero. It is read rather than skipped because the
        # 'priced' fallback writes exactly these variables and this figure is what reports
        # what it cost.
        #
        # Read whenever the crew rules were built, which since 3.3.17 no longer requires
        # geography: a crewed run with route_chaining_status = 'off' carries the same
        # slacks and used to report a flat zero for all of them.
        crew_breach_h = 0.0
        peak_drivers_model = None
        if fleet_operation_mode == 'crewed':
            for var in model.getVars():
                if var.VarName.startswith((f'shift_excess{tag}[', f'drive_excess{tag}[',
                                           f'break_excess{tag}[')):
                    crew_breach_h += max(0.0, var.X) * STEP_HOURS
            needed = model.getVarByName(f'drivers_needed{tag}')
            peak_drivers_model = None if needed is None else needed.X

        # 5.5c-ii what the objective carried for drivers, against what the roster costs.
        #
        # These are two different quantities and the run reports both, because the whole
        # crew side of this model is a sequential decomposition - the vehicles are
        # optimised, then the people are fitted to the result - and a decomposition is
        # only honest if the size of the seam is visible.
        #
        # Since the wage left the objective (1.4c) the two are not even the same KIND of
        # quantity, and that is the point rather than a defect:
        #
        #   driver_cost_in_objective_€   the head charge, penalty_driver_use x the day's
        #                                peak concurrency. A steering term, counted under
        #                                steering_penalties_€ below, and the only driver
        #                                figure the solver ever saw.
        #   driver_cost_€                the wage bill, from the roster hdv_driver_scheduling
        #                                built out of the finished schedule: shift span,
        #                                sign-on to sign-off, over however many drivers the
        #                                limits actually need. This is what the operator
        #                                pays, it is inside operating_cost_€, and nothing in
        #                                the optimisation was weighed against it.
        #
        # driver_cost_gap_€ is therefore no longer "how far the proxy missed the bill" - it
        # is the whole bill less a steering charge, and it is kept because the pair of
        # numbers is still what says which of the two any given figure came from.
        #
        # The away-hours reading that used to sit here - sum(1 - at_depot) over the
        # continuous variable, rather than the roster's thresholded count, so the objective
        # was reconciled the way the objective computed it - went with the wage term it
        # priced. Nothing multiplies those hours by money any more.
        driver_head_cost_model = (penalty_driver_use * peak_drivers_model
                                  if peak_drivers_model is not None else 0.0)
        driver_cost_in_objective = driver_head_cost_model
        drivers_over_model = (None if peak_drivers_model is None
                              else roster['driver_count'] - int(round(peak_drivers_model)))

        # (only on a run that writes figures, 1.4b1)
        if write_outputs:
            # 5.2.4 the driver roster, as a figure
            #
            # Only drawn for a crewed fleet. An autonomous one has duty blocks but nobody to
            # put on the rows, and a chart of zero drivers says nothing the figures do not.
            if fleet_operation_mode == 'crewed' and roster['drivers']:
                plt, Patch = _pyplot()
                drivers_sorted = sorted(roster['drivers'], key=lambda d: (d.sign_on, d.driver_id))
                # the blocks the crew rules were broken on. Both figures mark them, so this
                # is worked out once, outside the switch below.
                over_limit = {id(b) for b in roster['over_shift']}

                # Two panels, because there are two questions and they are not the same one.
                # The top is who drives what and when - one row per driver, one bar per duty
                # block, labelled with the vehicle, so a driver changing trucks at the depot
                # is a visible event rather than a number in a table. The bottom is how many
                # are on duty at each moment, which is the shape of the day: where the peak
                # sits, how long it lasts, and how much of the roster is idle around it.
                # The grid below keeps the first question and gives up the second - a count
                # over time is a curve and a grid has no room for one.

                # ... and the roster as a grid: what each driver is doing, half hour by
                # half hour, which is how it is drawn now. A Gantt shows the shape of a
                # shift; this shows the handovers, because the vehicle number stands in
                # every cell and a change of number down a row is a change of truck.
                import hdv_grid_plots as grid_plots

                driver_palette = {
                    'vehicle': ('tab:blue', 'with a vehicle - cell: vehicle no.'),
                    'over_shift': ('indianred', 'block longer than one shift'),
                    'depot_break': ('whitesmoke', 'break at the depot, paid (B)'),
                }
                states = [[None] * len(time_steps) for _ in drivers_sorted]
                cell_text = [[''] * len(time_steps) for _ in drivers_sorted]
                for row, driver in enumerate(drivers_sorted):
                    # the whole span first - sign-on to sign-off is paid, blocks or not -
                    # then the blocks written over it. What is left as break is exactly the
                    # paid time with no vehicle, which is what the shift costs and does not
                    # move. Off duty stays empty, and shows as a dash.
                    for t in range(driver.sign_on, driver.sign_off + 1):
                        if t in time_steps:
                            states[row][t] = 'depot_break'
                            cell_text[row][t] = 'B'
                    for block in driver.blocks:
                        key = 'over_shift' if id(block) in over_limit else 'vehicle'
                        for t in range(block.first_step, block.last_step + 1):
                            if t in time_steps:
                                states[row][t] = key
                                cell_text[row][t] = str(block.vehicle)
                grid_plots.grid_categories(
                    states, cell_text,
                    [f"D{d.driver_id} ({d.shift_hours:g} h)" for d in drivers_sorted],
                    project_path('results', output_filename(f'plot_driver_schedule{plot_suffix}.png')),
                    palette=driver_palette, step_hours=STEP_HOURS,
                    # the crew limits used to be stated in the Gantt's title only. With
                    # that figure off they would leave the results entirely, and a red
                    # over-shift block is not readable without the limit it broke
                    title=(f"Driver Schedule ({len(drivers_sorted)} drivers, "
                           f"{roster['paid_hours']:.1f} paid h, "
                           f"{roster['cost_eur']:.0f} EUR, max "
                           f"{driver_max_driving_hours:g} h driving / "
                           f"{driver_max_shift_hours:g} h shift)"),
                    row_axis_label='Driver (shift length)', plt=plt,
                    keep_open=displays_outputs())
        # the time cost of standing at a public station, counted on the steps the model
        # actually paid for: those outside a Lenkzeitpause. The steps inside one are free
        # of it, and reporting them separately is the only way to see whether external
        # charging was cheap because it was necessary or because the penalty was waived.
        external_steps_penalised = 0
        external_steps_in_break = 0
        if 'external_charging' in locations and external_charging_status == 'on':
            for m in bev_vehicles:
                for t in time_steps:
                    if model.getVarByName(f"x_m_t_l{tag}[{m},{t},external_charging]").X < 0.5:
                        continue
                    if model.getVarByName(f"penalized_external{tag}[{m},{t}]").X >= 0.5:
                        external_steps_penalised += 1
                    else:
                        external_steps_in_break += 1
        external_time_penalty_eur = (penalty_charging_external_time * STEP_MINUTES
                                     * external_steps_penalised)
        # 5.5d the empty running, priced (3.3.16)
        #
        # The objective charges deadhead kilometres exactly as it charges loaded ones - a
        # diesel burns the same fuel repositioning, and the toll is per kilometre whether
        # the trailer is full or not (3.4). These two figures used to be summed over the
        # assigned *trips* alone, so every empty kilometre the optimiser paid for was
        # missing from the costs the run reported. With chaining on that is not a rounding
        # difference: it is the entire price of the geography, and it made a solution with
        # heavy repositioning look cheaper in the results table than it had been to the
        # solver - the one direction a cost report must never be wrong in.
        #
        # Only the diesel side needs adding here. A bev's empty kilometres are already in
        # the SoC balance (3.3.16f), so they arrive through the metered charging energy
        # and are in bev_actual_energy_cost. The toll has no such route and applies to
        # both, so it is added for every vehicle.
        ice_deadhead_energy_cost = sum(
            cost_vehicle_100km[m] * 0.01 * deadhead_km_by_vehicle.get(m, 0.0)
            for m in ice_vehicles)
        deadhead_toll_cost = sum(
            toll_rate_per_km.get(m, 0.0) * deadhead_km_by_vehicle.get(m, 0.0)
            for m in vehicles)
        ice_energy_cost += ice_deadhead_energy_cost

        energy_costs_total = ice_energy_cost + bev_actual_energy_cost
        pv_surplus_kWh = float(sum(pv_charging_available_kWh)) if pv_charging_available_kWh else 0.0
        # tolls, at the per-vehicle rates run_optimization built for the objective, so the
        # figure reported is the one that was optimised against - loaded and empty alike
        toll_costs_total = sum(
            toll_rate_per_km.get(m, 0.0) * trip_distances.get(f, 0.0)
            for m, f, _s in assigned_trip_starts
        ) + deadhead_toll_cost
        v2g_earnings             = v2g_earnings_all_total      
        
        # energy_costs_total is the energy bill itself - prices from costs_dataset.xlsx
        # times the energy actually taken - and nothing else. It used to have the vehicle-id
        # tie-break penalty subtracted from it and, under auto_sizing, the V2G earnings of
        # unselected vehicles added: neither was ever part of it, so both only corrupted the
        # figure. Those terms live in the objective and are reported on their own.
        if auto_sizing == 'on':
            # with auto_sizing the roster is a result, so the earnings reported are those
            # of the vehicles the run actually selected
            v2g_earnings = v2g_earnings_used_total
        
        # 5.6 amount of needed charging stations and power
        # Read off the same rule the model was built on: in every step the trucks drawing
        # energy occupy the strongest stations, so a station's peak is the highest power
        # any truck drew while standing at it. Stations the fleet never reached that far
        # down the list stay at zero and are reported as unused.
        peak_demands_per_station = {s: 0.0 for s in range(len(charging_infrastructure))}
        for t in time_steps:
            for m, station in _station_assignment(t).items():
                drawn_kW = model.getVarByName(f"E_private{tag}[{m},{t}]").X / STEP_HOURS
                peak_demands_per_station[station] = max(peak_demands_per_station[station], drawn_kW)
        used_LIS_peak_powers = [v for v in peak_demands_per_station.values() if v > 0]
        # which of the depot's stations the schedule actually occupies, by charger_id -
        # with a heterogeneous roster the powers alone no longer identify them
        used_station_ids = [station_label(s) for s, v in peak_demands_per_station.items() if v > 0]

        # 5.7 per-bev SoC / energy figure and the depot power overview
        if write_outputs and len(bev_vehicles) > 0:
            x_steps = time_steps[:]
            plt, Patch = _pyplot()

            # the whole day on every figure, labelled hh:mm like the disposition plot
            hour_ticks = list(range(0, len(time_steps), 4)) + [len(time_steps)]
            hour_labels = [step_to_time(step) for step in hour_ticks]

            def as_hours(axis):
                axis.set_xlim(0, len(time_steps))
                axis.set_xticks(hour_ticks)
                axis.set_xticklabels(hour_labels)

            # 5.7.2 individual bev plots with SoC and energy flow - the original stack of

            # ... and the same state of charge as a grid, which is how it is drawn now. A
            # curve is exact for one truck at a time; this puts the whole bev fleet in one
            # image, where a row running pale is a battery the day left with nothing in
            # reserve. A cell is the level at the end of its half hour, so a spike shorter
            # than that is the one thing the curves showed and this does not.
            import hdv_grid_plots as grid_plots

            soc_grid = np.full((len(bev_vehicles), len(time_steps)), np.nan)
            for row, m in enumerate(bev_vehicles):
                capacity = float(
                    fleet.loc[fleet['vehicle_id'] == m, 'vehicle_energy_storage'].values[0])
                if capacity <= 0:
                    continue
                for t in time_steps:
                    soc_grid[row, t] = 100.0 * (
                        model.getVarByName(f"x_m_SoC{tag}[{m},{t}]").X / capacity)

            # 5.7.2b the power that moves that level, in the same cells rather than in a
            #        second figure. "How full" and "how hard it is being pushed" are two
            #        questions - a battery can sit at 80 % all afternoon whether it got
            #        there at 50 kW or at 600 - but they are always asked together, and
            #        answering them in two images meant reading a row here, finding the
            #        same row there and holding the first in mind while doing it. The
            #        level takes the colour, because its shape over the day is what the
            #        eye should find without reading; the power takes the digits, because
            #        it is read cell by cell when a cell looks wrong.
            #
            #        Metered power, not battery-side: a truck pulling its rated kW prints
            #        that rating, where the battery-side figure would fall short of it by
            #        the conversion loss and never quite show the number the hardware is
            #        specified by.
            power_grid = np.full((len(bev_vehicles), len(time_steps)), np.nan)
            for row, m in enumerate(bev_vehicles):
                for t in time_steps:
                    charged = model.getVarByName(f"E_pos{tag}[{m},{t}]").X
                    discharged = model.getVarByName(f"E_neg{tag}[{m},{t}]").X
                    if max(charged, discharged) <= ENERGY_TOLERANCE_KWH:
                        continue                  # not plugged in, or plugged in and idle
                    power_grid[row, t] = (charged - discharged) / STEP_HOURS

            grid_plots.grid_heatmap(
                # the same names the disposition grid uses (5.0b), so a row can be followed
                # from one figure to the other
                soc_grid, [row_labels_by_vehicle[m] for m in bev_vehicles],
                project_path('results',
                             output_filename(f'plot_SoC_charging_power{plot_suffix}.png')),
                # the bar says what the colour is and nothing else. What the cells hold is
                # a property of the figure, not of the scale, so it belongs to the title -
                # where a reader looks once - rather than under a gradient they look at
                # every time they decode a shade.
                value_label='State-of-charge [%]',
                colour_map=grid_plots.soc_colour_map(), step_hours=STEP_HOURS,
                keep_open=displays_outputs(),
                vmin=0.0, vmax=100.0,
                text_values=power_grid,
                # signed always, not only when negative. The direction used to be a colour
                # and is now a character, so it has to be one that is there to be read: a
                # column of "350" and "-175" hides the charging and a column of "+350" and
                # "-175" does not. It costs no width - the minus already set it - and so no
                # font size either.
                text_format='{:+.0f}',
                title='State-of-Charge and Dis-/Charging of BEV trucks '
                      '(colour: state-of-charge [%], number: metered power [kW], '
                      '+ charging and - discharging, per 30-min step)',
                row_axis_label='Vehicle', plt=plt)

            # 5.7.3 depot power overview: everything that meets at the grid connection
            #       The fleet used to be a row inside the SoC figure, where it could only be
            #       compared with itself. On its own axis it can be put next to the site's
            #       own load and its PV, which is what decides the grid draw.
            depot_charging_kW_t = []   # what the charging infrastructure pulls
            v2g_feed_kW_t = []         # what the trucks push back
            for t in x_steps:
                depot_charging_kW_t.append(
                    sum(model.getVarByName(f"E_private{tag}[{m},{t}]").X for m in bev_vehicles) / STEP_HOURS)
                v2g_feed_kW_t.append(
                    sum(E_neg[m, t].X for m in bev_vehicles) / STEP_HOURS)
            baseline_kW_t = [depot_baseline_load_kW[t] for t in x_steps]
            pv_kW_t = [pv_generation_kW[t] for t in x_steps]
            # the grid sees the site load and the chargers, minus what PV and V2G supply.
            # Negative means the depot exports rather than draws.
            grid_with_bev = [b + c - v - p for b, c, v, p
                             in zip(baseline_kW_t, depot_charging_kW_t, v2g_feed_kW_t, pv_kW_t)]
            # the same site without the fleet: no charging, no V2G, just load and PV
            grid_without_bev = [b - p for b, p in zip(baseline_kW_t, pv_kW_t)]

            fig3, ax3 = plt.subplots(figsize=(16, 8))
            ax3.bar(x_steps, depot_charging_kW_t, width=0.9, color='tab:green', alpha=0.45,
                    label='charging infrastructure [kW]')
            ax3.bar(x_steps, [-v for v in v2g_feed_kW_t], width=0.9, color='tab:red', alpha=0.45,
                    label='V2G discharge [kW]')
            ax3.plot(x_steps, baseline_kW_t, color='tab:blue', linewidth=2,
                     label='depot consumption [kW]')
            ax3.plot(x_steps, pv_kW_t, color='tab:orange', linewidth=2,
                     label='depot PV generation [kW]')
            ax3.plot(x_steps, grid_with_bev, color='black', linewidth=2.5,
                     label='grid draw with BEV [kW]')
            ax3.plot(x_steps, grid_without_bev, color='grey', linewidth=2, linestyle='--',
                     label='grid draw without BEV [kW]')
            ax3.axhline(y=0.0, color='black', linewidth=0.8, alpha=0.5)
            ax3.set_title('Depot Power Overview (negative = fed into the grid)')
            ax3.set_xlabel('Time of day [hh:mm]')
            ax3.set_ylabel('Power [kW]')
            as_hours(ax3)
            ax3.grid(True, alpha=0.3)
            ax3.legend(loc='upper right', fontsize='small', ncol=2)
            plt.tight_layout()
            plt.savefig(project_path('results', output_filename(f'plot_depot_power_overview{plot_suffix}.png')),
                        bbox_inches='tight', dpi=FIGURE_DPI)
            release_figure(plt, fig3)

            # the aging weight the objective was actually built from, against the parabola
            # it stands for. The model never carries the square itself (that made it
            # quadratically constrained - see 3.4); it carries the maximum of the tangents
            # in soc_aging_weight_tangents(), which is an approximation *from below* - the
            # blue line therefore never rises above the grey one, and the two meet exactly
            # at the breakpoints. Drawn from the live parameters rather than fixed numbers,
            # so changing soc_weight_factor or soc_weight_breakpoints shows what that did.
            if (plot_degradation_weight_curve
                    and advanced_degradation_status == 'on' and soc_weight_factor > 0):
                tangents = soc_aging_weight_tangents()
                soc_grid = np.linspace(0.0, 1.0, 1001)
                exact = 1.0 + 4.0 * soc_weight_factor * (soc_grid - 0.5) ** 2
                pwl = np.array([max(w + slope * (x_val - x) for x, w, slope in tangents)
                                for x_val in soc_grid])
                # how far the approximation ever falls below the parabola. Sampled on the
                # grid rather than solved for: the extremes sit midway between breakpoints
                # and 1001 points resolve them to well past the digits printed.
                max_error = float(np.abs(pwl - exact).max())

                fig4, ax4a = plt.subplots(figsize=(9, 5.6))
                ax4a.plot(soc_grid * 100.0, exact, color='0.75', linewidth=5,
                          label=f'parabola  1 + 4·{soc_weight_factor:g}·(SoC−50 %)²')
                ax4a.plot(soc_grid * 100.0, pwl, color='tab:blue', linewidth=2,
                          label=f'charged: max of {len(tangents)} tangents '
                                f'(max error {max_error:.4f})')
                ax4a.plot([x * 100.0 for x, _w, _s in tangents], [w for _x, w, _s in tangents],
                          'o', color='tab:blue', markersize=8, label='breakpoints (exact there)')
                # the two numbers the parameter is defined by: 1.0 at half charge and
                # 1 + soc_weight_factor at either extreme
                for x_pos, ha in ((0.0, 'left'), (100.0, 'right')):
                    ax4a.annotate(f'{1.0 + soc_weight_factor:g}', xy=(x_pos, 1.0 + soc_weight_factor),
                                  xytext=(4 if ha == 'left' else -4, 6), textcoords='offset points',
                                  ha=ha, fontsize='large')
                ax4a.annotate('1.0', xy=(50.0, 1.0), xytext=(0, 10), textcoords='offset points',
                              ha='center', fontsize='large')
                ax4a.set_title('Aging weight: piecewise-linear vs the parabola it stands for')
                ax4a.set_xlabel('SoC at the start of the step [%]')
                ax4a.set_ylabel('aging weight w  [-]')
                ax4a.set_xticks(range(0, 101, 10))
                ax4a.grid(True, alpha=0.3)
                ax4a.legend(loc='upper center', fontsize='small')

                plt.tight_layout()
                # no plot_suffix: the curve is a function of the parameters alone, so every
                # day of a run would write the same picture
                plt.savefig(project_path('results', output_filename('plot_degradation_weight_curve.png')),
                            bbox_inches='tight', dpi=FIGURE_DPI)
                release_figure(plt, fig4)

        # 5.8 degradation
        #     only the V2G discharge is cycled at a cost, exactly as priced in the
        #     objective; driving and ordinary charging are not charged for. That is why it
        #     is no longer v2g_equivalent_full_cycles x €/EFC: the cycles stay a plain
        #     physical count, the cost carries the SoC weight, and the two differ by the
        #     weight's mean.
        #
        #     Two figures, because at a nonzero MIP gap they are not the same number.
        #     degradation_cost_€ evaluates the piecewise-linear aging weight at the SoC the
        #     step started from - the physically correct value. The objective instead holds
        #     a *variable* per step, bounded below by the tangents of the weight curve
        #     (3.4), and the minimisation only presses it down onto them as far as closing
        #     the gap is worth a node. Wherever it was left slack and the step discharged,
        #     the objective charged more aging than the curve says. That is an artefact of
        #     the formulation, not a cost, so the reported figure is the curve one - and
        #     the objective's is reported beside it, because the reconciliation of 5.8b has
        #     to subtract what the objective actually carried or the difference comes back
        #     as an unexplained residual. On day 1 at the default 10 % gap it is ~0.80 EUR.
        v2g_efc_total = 0.0
        degradation_cost_in_objective = 0.0
        degradation_cost_total = 0.0
        for m in bev_vehicles:
            cap = fleet.loc[fleet['vehicle_id'] == m, 'vehicle_energy_storage'].values[0]
            price = fleet.loc[fleet['vehicle_id'] == m, 'vehicle_price'].values[0]
            warranty = fleet.loc[fleet['vehicle_id'] == m, 'vehicle_battery_warranty'].values[0]
            efc_base = degradation_cost_per_efc(price, warranty)
            # on the discharge as the *battery* delivers it (E_neg is metered, so the pack
            # gives up E_neg/eta_dis), which is what the objective charges in 3.4
            v2g_efc_total += (sum(E_neg[m, t].X for t in time_steps)
                              / discharging_efficiency / (2.0 * cap))
            for t in time_steps:
                soc_before = (initial_soc_fraction * cap if t == 0
                              else model.getVarByName(f"x_m_SoC{tag}[{m},{t-1}]").X)
                throughput = E_neg[m, t].X / discharging_efficiency
                # the piecewise-linear weight the objective was built from, not the
                # parabola it approximates - evaluated at the SoC, which is the weight
                # that step's discharge actually earns
                soc_w = soc_aging_weight(soc_before / cap)
                degradation_cost_total += efc_base * throughput * soc_w / (2.0 * cap)
                # ... and the weight the objective carried, which is the same scalar where
                # 3.4 used one and the solved variable where it used one of those
                if t == 0 or soc_weight_factor == 0:
                    soc_w_objective = soc_aging_weight(
                        initial_soc_fraction if t == 0 else 0.5)
                else:
                    weight_var = model.getVarByName(f"soc_aging_w_{m}_{t}{tag}")
                    soc_w_objective = soc_w if weight_var is None else weight_var.X
                degradation_cost_in_objective += (efc_base * throughput
                                                  * soc_w_objective / (2.0 * cap))

        # 5.8b what the day cost, and what the solver was merely steered by
        #
        # The objective is not a bill, and it is not the whole bill either. It holds most of
        # the money - fuel, electricity, tolls, battery wear, the demand charge - plus a
        # handful of terms that exist only to make the search behave: a flat charge per
        # truck used, another per occupied charger slot, a tie-break on vehicle id, a nudge
        # to spread battery aging, a deterrent on public charging, a head count standing in
        # for a roster the MILP cannot build, and a price on breaking the driving limits so
        # that an undrivable trip does not come back as a bare "infeasible".
        #
        # Every one of those is a device. Nobody invoices the operator €1 for occupying a
        # charger or €500 for a half-hour over the Lenkzeit, and a vehicle-id tie-break is
        # not a cost in any sense at all - it is there so two runs of the same data return
        # the same schedule. Summing them into a reported cost, which is what reading
        # ObjVal as "the cost of the day" does, inflates the figure by an amount that
        # depends on how the search was tuned rather than on what the fleet did. Tighten
        # penalty_vehicle_use to sharpen the sizing and the day appears to get dearer.
        #
        # And it runs the other way once: the driver WAGE is a real cost that the objective
        # deliberately does not carry (1.4c). It is in operating_cost_€ and it is not in
        # ObjVal, which is the one term of the three-way split that is money the solver
        # never weighed.
        #
        # So they are separated here and reported under their own names. operating_cost_€
        # is the day's economics and nothing else; steering_penalties_€ is the modelling
        # apparatus, visible rather than deleted, because a large one is a signal that the
        # tuning is distorting the answer and not only the arithmetic.
        used_vehicle_count = sum(1 for m in vehicles if y_m[m].X >= 0.5)
        # Both vehicle penalties are read off the model's own objective coefficients on
        # y_m rather than re-derived from the parameters, because re-deriving them needs
        # to know whether the id tie-break was applied - it is skipped under auto_sizing
        # (3.4) - and that flag does not always reach here with the value the model was
        # built with. The design run is the case: it builds each day with the module's
        # auto_sizing and then calls postprocess with 'on' regardless, because what it
        # wants from the flag is the used-vehicle fleet size. Deriving the penalty from
        # the flag therefore reported zero for a term that was in the objective, and the
        # reconciliation below showed it as an unexplained residual. The coefficient is
        # the objective, so there is nothing left to get out of step.
        y_objective_total = sum(y_m[m].Obj * y_m[m].X for m in vehicles)
        vehicle_use_penalty_eur = penalty_vehicle_use * sum(y_m[m].X for m in vehicles)
        # Zero since 3.3.10b replaced the id tie-break with a constraint, and kept as the
        # check that it is: penalty_vehicle_use is now the only thing on a y coefficient,
        # so anything left over here is a term someone added to y without saying so.
        vehicle_id_penalty_eur = y_objective_total - vehicle_use_penalty_eur
        # one charge per occupied slot: depot charging, V2G, and standing at a public
        # station - the same three the objective counts
        occupied_slots = 0
        for m in vehicles:
            for t in time_steps:
                chg = model.getVarByName(f"z_m_f_s{tag}[{m},CHG_t{t},{t}]")
                if chg is not None and chg.X >= 0.5:
                    occupied_slots += 1
                vtg = model.getVarByName(f"z_m_f_s{tag}[{m},V2G_t{t},{t}]")
                if vtg is not None and vtg.X >= 0.5:
                    occupied_slots += 1
                if 'external_charging' in locations:
                    ext = model.getVarByName(f"x_m_t_l{tag}[{m},{t},external_charging]")
                    if ext is not None and ext.X >= 0.5:
                        occupied_slots += 1
        charging_slot_penalty_eur = penalty_charging_use * occupied_slots
        # and one charge per plug-in (3.3.10d). Counted off the schedule rather than read
        # off chg_block_start, for the same reason the slot count above is: a figure the
        # reconciliation checks has to be derived from what the trucks did, or it can only
        # ever confirm that the model agrees with itself.
        block_price_reported = float(
            penalty_charging_block if penalty_charging_block is not None
            else globals()['penalty_charging_block'])
        charging_blocks = 0
        for m in bev_vehicles:
            connected_before = False
            for t in time_steps:
                chg = model.getVarByName(f"z_m_f_s{tag}[{m},CHG_t{t},{t}]")
                charging_now = chg is not None and chg.X >= 0.5
                # a block is one visit to a charger - plug in, do whatever, pull away - so
                # a discharge neither ends one nor escapes being counted as the start of
                # one. Same rule as 3.3.10d, or this figure would disagree with the term it
                # is reporting, which is how the two earlier versions of it were caught.
                vtg = model.getVarByName(f"z_m_f_s{tag}[{m},V2G_t{t},{t}]")
                discharging_now = vtg is not None and vtg.X >= 0.5
                connected_now = charging_now or discharging_now
                if connected_now and not connected_before:
                    charging_blocks += 1
                connected_before = connected_now
        charging_block_penalty_eur = block_price_reported * charging_blocks
        # ... and the cables the day needed at once (3.4c). Counted off the schedule, like
        # every other steering figure here, so the reconciliation can disagree with the
        # model rather than merely echo it.
        #
        # The COUNT is reported whatever kind of run this is - "how many cables was the
        # fleet on at its busiest" is a fact about the day that a disposition wants to know
        # as much as a sizing run does. Only the PRICE is asset-sizing only (1.5a), which is
        # why charger_use_price() is applied below and not here: on a disposition or a sweep
        # the peak comes back as a figure and 0.00 EUR beside it.
        chargers_concurrent_peak = 0
        for t in time_steps:
            connected_here = 0
            for m in bev_vehicles:
                chg = model.getVarByName(f"z_m_f_s{tag}[{m},CHG_t{t},{t}]")
                if chg is not None and chg.X >= 0.5:
                    connected_here += 1
                    continue
                vtg = model.getVarByName(f"z_m_f_s{tag}[{m},V2G_t{t},{t}]")
                if vtg is not None and vtg.X >= 0.5:
                    connected_here += 1
            chargers_concurrent_peak = max(chargers_concurrent_peak, connected_here)
        charger_use_penalty_eur = charger_use_price(auto_sizing) * chargers_concurrent_peak
        crew_breach_penalty_eur = penalty_crew_rule_breach * (crew_breach_h / STEP_HOURS)
        aging_spread_penalty_eur = 0.0
        if advanced_degradation_status == 'on':
            worst = model.getVarByName(f"max_efc_degrad{tag}")
            if worst is not None:
                aging_spread_penalty_eur = degradation_distribution_penalty * worst.X
        steering_penalties_eur = (vehicle_use_penalty_eur + vehicle_id_penalty_eur
                                  + charging_slot_penalty_eur + charging_block_penalty_eur
                                  + charger_use_penalty_eur
                                  + external_time_penalty_eur
                                  + driver_head_cost_model + crew_breach_penalty_eur
                                  + aging_spread_penalty_eur)

        # the day's economics. The driver line is the roster's wage bill, and it stays in
        # here even though the objective no longer carries it (1.4c): a salary the solver
        # did not optimise against is still a salary the operator pays, and operating_cost_€
        # is defined as what the day costs rather than as what was minimised. The whole
        # difference between the two is what the reconciliation below states.
        operating_cost_eur = (energy_costs_total
                              + toll_costs_total
                              + degradation_cost_total
                              + demand_charge_eur
                              + roster['cost_eur']
                              - v2g_earnings
                              - v2v_saved_total)
        # ObjVal bridged back to the two figures above, as a check rather than a result.
        #
        # Two of the reported costs are deliberately *not* what the objective carried, and
        # both are swapped back here so that what is left is genuinely unexplained:
        #
        #   the driver line - the objective carries no wage at all since 1.4c, so the
        #                     roster's bill is simply taken back out. Nothing is put in its
        #                     place; the head charge the objective DOES carry is inside
        #                     steering_penalties_eur already.
        #   the aging line  - the objective carried a weight variable the solver left
        #                     slack, the report evaluates the weight curve (5.8)
        #
        # After that a residual of a few cents is rounding. A large one means a term was
        # added to the objective and not to this reconciliation, which is worth knowing
        # before the figures are quoted.
        #
        # One case where it will not close and should not be read as a fault: under
        # auto_sizing the earnings reported are those of the selected vehicles only while
        # the objective credits every vehicle's.
        objective_eur = float(model.ObjVal)
        objective_residual_eur = objective_eur - (
            operating_cost_eur
            - roster['cost_eur']
            - degradation_cost_total + degradation_cost_in_objective
            + steering_penalties_eur)

        # 5.9 output results and write results dictionary
        if write_outputs:
            # Gurobi calls a run OPTIMAL as soon as it meets MIPGap, so with the default 10%
            # "optimal" alone reads as exact while the cost may still be that far above the
            # best bound. The achieved gap says how far, so print it with the status.
            gap_text = ('gap n/a' if achieved_gap is None
                        else f'gap {achieved_gap * 100:.2f}%')
            print('optimization status:     ', optimization_status,
                  f'({gap_text}, target {optimization_MIPGap * 100:.2f}%)')
            print('OPERATING COST:          ', round(operating_cost_eur, 2),
                  '€ - fuel, electricity, tolls, battery wear, demand charge and the',
                  'roster, less V2G and V2V earnings. No modelling penalty is in it.')
            print('  steering penalties:    ', round(steering_penalties_eur, 2),
                  '€ of search apparatus, NOT a cost of the day:',
                  f'{vehicle_use_penalty_eur:.2f} truck use +',
                  f'{charging_slot_penalty_eur:.2f} charger slots +',
                  f'{charging_block_penalty_eur:.2f} plug-ins +',
                  # 0.00 on anything but an asset-sizing run (1.5a), and printed anyway so
                  # the line adds up to the total beside it on every kind of run
                  f'{charger_use_penalty_eur:.2f} cables at once +',
                  f'{driver_head_cost_model:.2f} driver heads +',
                  f'{external_time_penalty_eur:.2f} public-charging time +',
                  f'{crew_breach_penalty_eur:.2f} crew breach +',
                  f'{aging_spread_penalty_eur:.2f} aging spread +',
                  f'{vehicle_id_penalty_eur:.2f} id tie-break')
            print('  solver objective:      ', round(objective_eur, 2),
                  f'€ (residual against the two above {objective_residual_eur:+.2f} €,',
                  'which is rounding unless it is large)')
            print('energy costs:            ', round(energy_costs_total, 2), '€')
            print('truck toll costs:        ', round(toll_costs_total, 2), '€')
            print('V2G earnings:            ', round(v2g_earnings, 2), '€',
                  f"(arbitrage {v2g_earnings_by_channel['arbitrage']:.2f} € on "
                  f"{v2g_energy_by_channel['arbitrage']:.0f} kWh, "
                  f"flexibility {v2g_earnings_by_channel['flexibility']:.2f} € on "
                  f"{v2g_energy_by_channel['flexibility']:.0f} kWh)"
                  if v2g_channel_at_step is not None else '')
            print('degradation cost:        ', round(degradation_cost_total, 2), '€',
                  f'({round(v2g_efc_total, 3)} V2G EFC)')
            print('used ice-HDT:            ', used_ice)
            print('used bev-HDT:            ', used_bev, [math.ceil(x) for x in used_bev_battery], 'kWh')
            charger_list = [math.ceil(x) for x in used_LIS_peak_powers] if used_LIS_peak_powers else []
            print('installed chargers:      ', len(charging_infrastructure),
                  [math.ceil(p) for p in charging_infrastructure], 'kW (depot_dataset.xlsx)')
            print('used chargers:           ', len(used_LIS_peak_powers), charger_list, 'kW',
                  f'(id {", ".join(used_station_ids)})' if used_station_ids else '')
            print('depot grid peak:         ', round(site_peak_kW_value, 1), 'kW with bev,',
                  round(baseline_peak_kW_value, 1), 'kW without =',
                  f'{bev_peak_increment_kW:+.1f} kW from the fleet')
            print('demand charge:           ', round(demand_charge_eur, 2),
                  '€/day on that increment',
                  '(a saving - V2G shaved the site peak)' if bev_peak_increment_kW < 0 else '')
            print('own PV into trucks:      ', round(pv_charging_kWh, 1), 'kWh of',
                  round(pv_surplus_kWh, 1), 'kWh surplus =',
                  round(pv_energy_cost, 2), f'€ at {pv_price_avg:.3f} €/kWh average',
                  f'(spot - {energy_selling_overhead_eur_per_kWh:.3f} selling overhead; '
                  f'{pv_energy_saving_eur:.2f} € cheaper than buying the same kWh)')
            print('grid into trucks (depot):', round(grid_charging_kWh, 1), 'kWh at',
                  f'{grid_charging_price_avg:.3f} €/kWh average '
                  f'(curve {min(depot_buy_eur_per_kWh_t):.3f}-'
                  f'{max(depot_buy_eur_per_kWh_t):.3f} €/kWh incl. overhead)')
            print('public charging:         ', round(external_charging_kWh, 1), 'kWh =',
                  round(external_charging_cost, 2),
                  f'€ at {public_charging_cost_eur_per_kWh:.3f} €/kWh')
            if day_routing is not None:
                loaded_km = sum(trip_distances.get(f, 0.0) for f in day_trips_list)
                share = deadhead_km / loaded_km if loaded_km > 0 else 0.0
                print('routes:                  ', routes_driven, 'from the depot,',
                      chains_used, f'chained trip(s) ({direct_chains_used} of them direct)')
                print('empty running:           ', round(deadhead_km, 1), 'km =',
                      f'{share:.0%} of the {loaded_km:.0f} km under load',
                      f'({deadhead_approach_km:.0f} approach + {deadhead_return_km:.0f} return'
                      f' + {deadhead_chain_km:.0f} between chained trips)')
                print('fleet at the depot:      ',
                      f'{depot_steps_share:.0%} of all vehicle-steps - the only time depot',
                      'charging and V2G were available')
            if fleet_operation_mode != 'crewed':
                print('drivers:                  none - autonomous fleet.',
                      len(roster['blocks']), 'duty block(s) covering',
                      f"{roster['driving_hours']:.1f} vehicle-h away from the depot,",
                      f"longest {roster['longest_block_h']:g} h.",
                      'No crew limits applied, no Lenkzeitpause added, no trips removed.')
            bound = roster['driver_lower_bound']
            slack = ('' if roster['driver_count'] == bound
                     else f" - {bound} is the most vehicles away at once, so the roster "
                          f"uses {roster['driver_count'] - bound} more than that floor")
            if fleet_operation_mode == 'crewed':
                print('drivers:                 ', roster['driver_count'],
                      f"covering {len(roster['blocks'])} duty block(s) for "
                      f"{roster['paid_hours']:.1f} paid h "
                      f"({roster['driving_hours']:.1f} h with a vehicle, "
                      f"{roster['break_hours']:.1f} h on break at the depot)", slack)
                print('DRIVER SALARY:           ', round(roster['cost_eur'], 2),
                      f"€ at {driver_hourly_rate_eur:.2f} €/h on the shift span,",
                      f"{roster['vehicle_changes']} vehicle change(s) at the depot.",
                      'Inside OPERATING COST above, and reported only - the objective',
                      'carries no wage (1.4c), so no schedule was chosen against this.')
                print('  the model carried:     ', round(driver_cost_in_objective, 2),
                      f"€ of driver heads ({penalty_driver_use:.2f} €/head on a peak of "
                      f"{0 if peak_drivers_model is None else peak_drivers_model:.0f}),"
                      f" a steering term. What held the schedule to a crewable shape is"
                      f" the {driver_max_shift_hours:g} h shift limit and the"
                      f" {driver_max_driving_hours:g} h Lenkzeit, which are constraints"
                      f" and not prices.")
                if drivers_over_model:
                    print('  ROSTER NEEDS MORE:     ', drivers_over_model, 'driver(s) beyond',
                          'the', f'{peak_drivers_model:.0f}', 'the objective was charged for.',
                          f"The limits that decide this - {driver_max_working_hours:g} h of",
                          f"duty and {driver_max_driving_hours:g} h of driving per person -",
                          'bound the packing, not the totals, and a long absence cannot be',
                          'split between two people however much slack the totals leave.')
                print('driver Lenkzeit:         ', f"{roster['wheel_hours']:.1f} h at the wheel",
                      f"of {roster['driving_hours']:.1f} h with a vehicle; busiest driver",
                      f"{roster['max_driver_wheel_h']:.1f} h against the",
                      f"{driver_max_driving_hours:g} h daily limit")
            if fleet_operation_mode == 'crewed' and roster['over_driving']:
                print('  LENKZEIT BROKEN:       ', len(roster['over_driving']), 'of',
                      len(roster['blocks']), 'block(s) hold more than the',
                      f"{driver_max_driving_hours:g} h daily driving limit on their own",
                      f"(longest {roster['longest_block_wheel_h']:g} h at the wheel) -",
                      ', '.join(f"vehicle {b.vehicle} {b.wheel_hours:g} h"
                                for b in roster['over_driving'][:3]),
                      '- each is given its own driver and counted. No roster can make',
                      'those legal; the absence itself is too much driving for one person.')
            if fleet_operation_mode == 'crewed' and crew_breach_h > 1e-6:
                shift_note = (
                    f'The {driver_max_shift_hours:g} h shift limit is not among them - it '
                    f'is hard (3.3.17a), so none of these hours is an over-long absence.'
                    if driver_shift_limit == 'hard' else
                    f'driver_shift_limit is "priced", so some of these hours may be '
                    f'absences past the {driver_max_shift_hours:g} h shift limit.')
                print('  CREW LIMITS BROKEN:    ', f'{crew_breach_h:.1f} h beyond the',
                      f'{driver_max_driving_hours:g} h driving limit or the Lenkzeitpause.',
                      'Those two are priced, not hard, because some trips cannot be crewed',
                      'legally from this depot at all - a trip whose approach alone is 6 h',
                      'leaves no legal day. The solver broke them only where it had to;',
                      'every other vehicle-hour in this schedule is inside the rules.',
                      shift_note)
            if roster['over_shift']:
                cause = (
                    'driver_shift_limit is "priced", so the solver was allowed to buy its '
                    'way past 3.3.17a and did. Set it to "hard" to forbid this.'
                    if driver_shift_limit != 'hard' else
                    'This should not be reachable: 3.3.17a is hard, so no absence this '
                    'long is in the feasible set. Read it as a fault in the schedule '
                    'reader rather than as a finding about the day.')
                print('  OVER SHIFT:            ', len(roster['over_shift']), 'of',
                      len(roster['blocks']), f"block(s) exceed the {driver_max_shift_hours:g} h "
                      f"limit on their own (longest {roster['longest_block_h']:g} h) -",
                      ', '.join(f"vehicle {b.vehicle} {b.hours:g} h"
                                for b in roster['over_shift'][:3]),
                      '- each is given its own driver and counted, but the schedule cannot',
                      'be crewed legally as it stands.', cause)
            if dropped_trips:
                print(f'\nTRIPS REMOVED ({len(dropped_trips)}) - no driver could run these '
                      f'legally from {home_depot_location}, whatever else the fleet does:')
                for d in dropped_trips:
                    print(f"  trip {d['trip_ID']}: {d['start']} -> {d['end']}, "
                          f"{d['trip_distance_km']:.0f} km in {d['trip_duration_h']:.1f} h "
                          f"+ {d['approach_h']:g} h out + {d['return_h']:g} h back "
                          f"= {d['reason']}")
                print('  Every figure above excludes them. Move the depot, relax the crew '
                      'limits, or plan these as multi-day tours.\n')
            print('vehicle-to-vehicle:      ', round(v2v_kWh, 1), 'kWh passed truck to truck in',
                  v2v_steps, 'step(s), never crossing the meter =',
                  round(v2v_saved_total, 2), '€ saved')
            if v2v_kWh > ENERGY_TOLERANCE_KWH:
                print('  saved grid fees:       ', round(v2v_saved_grid_fees, 2),
                      f'€ ({grid_energy_overhead_eur_per_kWh:.3f} €/kWh of levies, taxes '
                      f'and network charges not paid on the way in)')
                print('  saved energy:          ',
                      round(v2v_saved_total - v2v_saved_grid_fees, 2),
                      '€ (the kWh themselves, at the spot price of each step)')
                print('  given up to get it:    ', round(v2v_forgone_v2g, 2),
                      '€ of V2G revenue. A kWh delivered next door was not delivered to '
                      'the grid, so neither channel pays for it (3.3.15c) - the net of '
                      'these two lines is what V2V was worth.')
                print('  not counted here:       the demand charge. 3.3.15 already nets the '
                      'discharge against the charging, so the peak - and its €/kW - was '
                      'never inflated by these kWh in the first place.')
            print('conversion losses:       ', round(charging_loss_kWh, 1), 'kWh charging at',
                  f'{charging_efficiency:.0%} +', round(discharging_loss_kWh, 1),
                  f'kWh discharging at {discharging_efficiency:.0%}',
                  f'(round trip {charging_efficiency * discharging_efficiency:.1%})',
                  f'= {conversion_loss_cost:.2f} € at the day\'s average '
                  f'{avg_charging_price:.3f} €/kWh')
            print('  external time penalty: ', round(external_time_penalty_eur, 2), '€ on',
                  external_steps_penalised,
                  f'x {STEP_MINUTES} min at {penalty_charging_external_time} €/min;',
                  external_steps_in_break, 'step(s) free inside a Lenkzeitpause',
                  f'({driving_break_duration_minutes:g} min after '
                  f'{driving_time_before_break_minutes / 60:g} h driving)\n')
            # who drives what, and when - one row per duty block, in shift order
            driver_rows = []
            for driver in roster['drivers']:
                for block in driver.blocks:
                    driver_rows.append({
                        'driver': driver.driver_id,
                        'vehicle': block.vehicle,
                        'from': step_to_time(block.first_step),
                        'to': step_to_time(block.last_step + 1),
                        'block_h': round(block.hours, 2),
                        'shift_from': step_to_time(driver.sign_on),
                        'shift_to': step_to_time(driver.sign_off + 1),
                        'shift_h': round(driver.shift_hours, 2),
                        'break_h': round(driver.break_hours, 2),
                        'vehicles_driven': len(driver.vehicles)})

        return {
            'iteration_number': 1,
            'fleet_size': used_ice + len(used_bev_battery) if auto_sizing == 'on' else len(vehicles),
            'fleet_electrification_%': round((len(fleet[fleet['vehicle_type'] == 'bev']) / len(fleet)) * 100, 1) if len(fleet) > 0 else 0,
            'scenario': scenario_iterations,
            'year': scenario_year_iterations,
            'km_electrification_%': round(electrified_km_percentage, 1),
            'total_fleet_distance_km': total_km,
            'median_trip_distance_km': int(statistics.median(trips['trip_distance_km'].tolist()) if len(trips) > 0 else 0),
            'v2g_earnings_total_€': round(v2g_earnings, 2),
            'degradation_cost_€': round(degradation_cost_total, 2),
            # what the objective carried for the same wear. Differs from the line above by
            # however far the solver left the aging-weight variables off their curve at the
            # configured MIP gap (5.8) - an artefact of the formulation, not a cost
            'degradation_cost_in_objective_€': round(degradation_cost_in_objective, 2),
            'v2g_equivalent_full_cycles': round(v2g_efc_total, 3),
            'optimization_status': optimization_status,
            # how far the reported cost may still be above the best bound, and what the
            # run asked for. Without these the status word cannot be checked by a reader
            # and a sweep CSV cannot tell a proof from a 10 % acceptance (5.5)
            'mip_gap': None if achieved_gap is None else round(achieved_gap, 5),
            'mip_gap_target': optimization_MIPGap,
            # what the day costs: fuel, electricity, tolls, battery wear, the demand
            # charge and the roster, less V2G and V2V earnings. No modelling penalty is
            # in it - those are below, under their own names (5.8b)
            'operating_cost_€': round(operating_cost_eur, 2),
            'steering_penalties_€': round(steering_penalties_eur, 2),
            'charging_blocks': charging_blocks,
            'charging_block_penalty_€': round(charging_block_penalty_eur, 2),
            'chargers_concurrent_peak': chargers_concurrent_peak,
            'charger_use_penalty_€': round(charger_use_penalty_eur, 2),
            'charging_power_modulation': charging_power_modulation,
            'objective_€': round(objective_eur, 2),
            'objective_residual_€': round(objective_residual_eur, 2),
            # the apparatus itself, itemised. None of these is money the operator pays;
            # they are what makes the search pick one schedule over an equivalent other
            'penalty_vehicle_use_€': round(vehicle_use_penalty_eur, 2),
            'penalty_vehicle_id_order_€': round(vehicle_id_penalty_eur, 2),
            'penalty_charging_slots_€': round(charging_slot_penalty_eur, 2),
            'penalty_driver_heads_€': round(driver_head_cost_model, 2),
            # the per-head charge that is actually inside this schedule's objective. Equal
            # to penalty_driver_use unless the roster feedback pass (2.8c) kept a schedule
            # it had steered with a higher one, which is worth seeing rather than inferring
            'penalty_driver_head_price_€': penalty_driver_use,
            'penalty_crew_breach_€': round(crew_breach_penalty_eur, 2),
            'penalty_aging_spread_€': round(aging_spread_penalty_eur, 2),
            'energy_costs_€': round(energy_costs_total, 2),
            'toll_costs_€': round(toll_costs_total, 2),
            'v2g_status': v2g_status_iteration,
            'chargers_installed': len(charging_infrastructure),
            'chargers_installed_kW': [round(p) for p in charging_infrastructure],
            'chargers_used_ids': used_station_ids,
            'chargers_kW': [round(v) for v in used_LIS_peak_powers],
            'bev_kWh': [round(v) for v in used_bev_battery],
            # counted on trips driven, not on y_m: see 5.3
            'ice_amount': used_ice,
            'bev_amount': used_bev,
            # bevs that moved energy but drove nothing. They are not in the count above and
            # cost nothing in the objective (3.3.10), so they are reported explicitly: a
            # fleet size that ignores them still needs them bought to earn what it earned.
            'vehicles_v2g_only': len(v2g_only_vehicles),
            'vehicles_v2g_only_ids': v2g_only_vehicles,
            'v2g_earnings_non_driving_€': round(v2g_earnings_non_driving, 2),
            'depot_grid_peak_kW': round(site_peak_kW_value, 1),
            'depot_grid_peak_without_bev_kW': round(baseline_peak_kW_value, 1),
            'bev_peak_increment_kW': round(bev_peak_increment_kW, 1),
            'demand_charge_€': round(demand_charge_eur, 2),
            'public_charging_kWh': round(external_charging_kWh, 1),
            'public_charging_cost_€': round(external_charging_cost, 2),
            # conversion losses (1.4a): every kWh figure in this dict is metered, so the
            # two frames only reconcile with these
            # routes and empty running (3.3.16); None when route_chaining_status is 'off'
            'home_depot_location': None if day_routing is None else day_routing.depot_location,
            'routes_driven': routes_driven,
            'chains_used': chains_used,
            'direct_chains_used': direct_chains_used,
            'deadhead_km': round(deadhead_km, 1),
            'deadhead_approach_km': round(deadhead_approach_km, 1),
            'deadhead_return_km': round(deadhead_return_km, 1),
            'deadhead_chain_km': round(deadhead_chain_km, 1),
            # what that empty running cost, already inside energy_costs_€ and toll_costs_€
            # (5.5d) and broken out here so the price of the geography is readable on its
            # own rather than only as a share of the kilometres
            'deadhead_energy_cost_€': round(ice_deadhead_energy_cost, 2),
            'deadhead_toll_cost_€': round(deadhead_toll_cost, 2),
            'fleet_at_depot_share': (None if depot_steps_share is None
                                     else round(depot_steps_share, 3)),
            # drivers (1.4c), rostered from the finished schedule - not minimised by it
            'drivers_required': roster['driver_count'],
            'drivers_lower_bound': roster['driver_lower_bound'],
            # the total driver salary for the day: shift span x driver_hourly_rate_€ over
            # every driver the roster needs. Reported and inside operating_cost_€; NOT in
            # the objective, so no schedule was chosen against it (1.4c).
            'driver_cost_€': round(roster['cost_eur'], 2),
            # and the only driver term the objective did carry - the head charge, which is
            # a steering penalty and not a wage (5.5c-ii)
            'driver_cost_in_objective_€': round(driver_cost_in_objective, 2),
            'driver_cost_gap_€': round(roster['cost_eur'] - driver_cost_in_objective, 2),
            # how many heads the roster needs beyond what the objective was charged for.
            # Positive means the schedule was chosen against too cheap a crew.
            'drivers_beyond_model': drivers_over_model,
            'driver_paid_h': round(roster['paid_hours'], 2),
            'driver_driving_h': round(roster['driving_hours'], 2),
            # the Lenkzeit itself - driving only, no loading, yard time or Lenkzeitpause
            'driver_wheel_h': round(roster['wheel_hours'], 2),
            'driver_max_wheel_h_one_driver': round(roster['max_driver_wheel_h'], 2),
            'driver_break_h': round(roster['break_hours'], 2),
            'driver_vehicle_changes': roster['vehicle_changes'],
            'driver_hourly_rate_€': driver_hourly_rate_eur,
            # derived as working + mandatory break (2.1.1b), reported so a row says
            # which span it was planned against without the reader recomputing it
            'driver_max_shift_h': driver_max_shift_hours,
            'driver_max_working_h': driver_max_working_hours,
            'driver_mandatory_break_h': driver_mandatory_break_hours,
            # 'hard' or 'priced' (1.4c). With 'hard', driver_blocks_over_shift below is 0
            # by construction and a non-zero value is a fault rather than a finding.
            'driver_shift_limit': driver_shift_limit,
            'driver_blocks_over_shift': len(roster['over_shift']),
            'driver_blocks_over_driving': len(roster['over_driving']),
            'driver_longest_block_h': round(roster['longest_block_h'], 2),
            'driver_longest_block_wheel_h': round(roster['longest_block_wheel_h'], 2),
            'driver_duty_blocks': len(roster['blocks']),
            'crew_breach_h': round(crew_breach_h, 2),
            'trips_removed': len(dropped_trips or []),
            'trips_removed_ids': [d['trip_ID'] for d in (dropped_trips or [])],
            'trips_removed_km': round(sum(d['trip_distance_km']
                                          for d in (dropped_trips or [])), 1),
            'warm_start_used': warm_start_used,
            'driver_max_driving_h': driver_max_driving_hours,
            'drivers_peak_in_model': (None if peak_drivers_model is None
                                      else round(peak_drivers_model, 2)),
            'v2v_status': v2v_status,
            'v2v_kWh': round(v2v_kWh, 1),
            'v2v_steps': v2v_steps,
            'v2v_saved_grid_fees_€': round(v2v_saved_grid_fees, 2),
            'v2v_saved_selling_fees_€': round(v2v_saved_selling_fees, 2),
            'v2v_saved_total_€': round(v2v_saved_total, 2),
            'v2v_forgone_v2g_€': round(v2v_forgone_v2g, 2),
            'charging_efficiency': charging_efficiency,
            'discharging_efficiency': discharging_efficiency,
            'charging_loss_kWh': round(charging_loss_kWh, 1),
            'discharging_loss_kWh': round(discharging_loss_kWh, 1),
            'conversion_loss_cost_€': round(conversion_loss_cost, 2),
            'battery_charged_kWh': round(battery_charged_kWh, 1),
            'battery_discharged_kWh': round(battery_discharged_kWh, 1),
            'external_time_penalty_€': round(external_time_penalty_eur, 2),
            'external_steps_penalised': external_steps_penalised,
            'external_steps_in_lenkzeitpause': external_steps_in_break,
            # the date the PV curve was generated for - without it a result row does not
            # say which season its solar yield belongs to
            'disposition_date': date_disposition.strftime('%d.%m.%Y'),
            # 'pvgis' / 'cache', or 'synthetic' when the run had to do without
            # a real curve - see pv_allow_synthetic_profile
            'pv_profile_source': pv_profile_source,
            # trips the working hours could not hold, which therefore kept their own
            # window from the order data - the setting does not apply to these
            'trips_outside_work_hours': list(trips_outside_work_hours or []),
            'pv_generation_kWh': round(float(sum(pv_generation_kW)) * STEP_HOURS, 1),
            'pv_surplus_kWh': round(pv_surplus_kWh, 1),
            'pv_charging_kWh': round(pv_charging_kWh, 1),
            'pv_charging_cost_€': round(pv_energy_cost, 2),
            # the opportunity price those kWh were billed at, volume-weighted, and what
            # the plant saved against buying the same energy from the grid. With the
            # default (None -> the grid curve) the saving is 0 by construction: the whole
            # PV advantage in the energy bill is exactly the retail-minus-opportunity
            # spread, and at the buy price there is no spread.
            'pv_opportunity_price_€/kWh': round(pv_price_avg, 4),
            'pv_energy_saving_€': round(pv_energy_saving_eur, 2),
            'grid_charging_kWh': round(grid_charging_kWh, 1),
        }

    else:
        if model.status == gp.GRB.TIME_LIMIT:
            optimization_status             = "timelimit"
        elif model.status == gp.GRB.CUTOFF:
            optimization_status             = "cutoff"
        elif model.status == gp.GRB.SUBOPTIMAL:
            optimization_status             = "suboptimal"
        else:
            optimization_status             = "infeasible"

        if write_outputs:
            print('\n\nRESULTS')
            print('optimization status:     ', optimization_status,'\n')

        # A solve that produced no schedule has no figures, and the row says so with None.
        #
        # It used to say 999999. That is a number, and everything downstream treats it as
        # one: a sweep CSV read into pandas averages it, a plot scales its y-axis to it, a
        # regression fits it, and a reader skimming a table sees an expensive day rather
        # than a failed one. Worse, it is indistinguishable from a real result without
        # cross-checking optimization_status first, which nothing did by default.
        #
        # None becomes an empty cell in the CSV and NaN when pandas reads it back, so mean
        # and max skip it, matplotlib leaves a gap, and the failure survives the round trip
        # instead of being laundered into data. The columns stay exactly as they are, so a
        # sweep still has one shape whichever way a row went - which is what the sentinel
        # was there for, and the only part of it worth keeping.
        #
        # The fields that are *parameters* rather than results keep their values: they were
        # known before the solve and are still true after it failed.
        return {
            'iteration_number': 1,
            'fleet_size': len(vehicles),
            'fleet_electrification_%': round((len(fleet[fleet['vehicle_type'] == 'bev']) / len(fleet)) * 100, 1) if len(fleet) > 0 else 0,
            'scenario': scenario_iterations,
            'year': scenario_year_iterations,
            'km_electrification_%': None,
            'total_fleet_distance_km': None,
            'median_trip_distance_km': int(statistics.median(trips['trip_distance_km'].tolist()) if len(trips) > 0 else 0),
            'v2g_earnings_total_€': None,
            'degradation_cost_€': None,
            'degradation_cost_in_objective_€': None,
            'v2g_equivalent_full_cycles': None,
            'optimization_status': optimization_status,
            'mip_gap': None,
            'mip_gap_target': optimization_MIPGap,
            'operating_cost_€': None,
            'steering_penalties_€': None,
            'objective_€': None,
            'objective_residual_€': None,
            'penalty_vehicle_use_€': None,
            'penalty_vehicle_id_order_€': None,
            'penalty_charging_slots_€': None,
            'penalty_driver_heads_€': None,
            'penalty_driver_head_price_€': penalty_driver_use,
            'penalty_crew_breach_€': None,
            'penalty_aging_spread_€': None,
            'energy_costs_€': None,
            'toll_costs_€': None,
            'v2g_status': v2g_status_iteration,
            'chargers_installed': len(charging_infrastructure),
            'chargers_installed_kW': [round(p) for p in charging_infrastructure],
            'chargers_used_ids': [],
            'chargers_kW': [],
            'bev_kWh': [],
            'ice_amount': None,
            'bev_amount': None,
            'depot_grid_peak_kW': None,
            'demand_charge_€': None,
            'disposition_date': date_disposition.strftime('%d.%m.%Y'),
            # 'pvgis' / 'cache', or 'synthetic' when the run had to do without
            # a real curve - see pv_allow_synthetic_profile
            'pv_profile_source': pv_profile_source,
            'trips_outside_work_hours': list(trips_outside_work_hours or []),
            'pv_generation_kWh': round(float(sum(pv_generation_kW)) * STEP_HOURS, 1),
            'pv_surplus_kWh': round(float(sum(pv_charging_available_kWh)), 1) if pv_charging_available_kWh else 0.0,
            'pv_charging_kWh': None,
            'pv_charging_cost_€': None,
            'pv_opportunity_price_€/kWh': None,
            'pv_energy_saving_€': None,
            'home_depot_location': None if day_routing is None else day_routing.depot_location,
            'routes_driven': None,
            'chains_used': None,
            'direct_chains_used': None,
            'deadhead_km': None,
            'deadhead_approach_km': None,
            'deadhead_return_km': None,
            'deadhead_chain_km': None,
            'deadhead_energy_cost_€': None,
            'deadhead_toll_cost_€': None,
            'fleet_at_depot_share': None,
            'drivers_required': None,
            'drivers_lower_bound': None,
            'driver_cost_€': None,
            'driver_cost_in_objective_€': None,
            'driver_cost_gap_€': None,
            'drivers_beyond_model': None,
            'driver_paid_h': None,
            'driver_driving_h': None,
            'driver_wheel_h': None,
            'driver_max_wheel_h_one_driver': None,
            'driver_break_h': None,
            'driver_vehicle_changes': None,
            'driver_hourly_rate_€': driver_hourly_rate_eur,
            # derived as working + mandatory break (2.1.1b), reported so a row says
            # which span it was planned against without the reader recomputing it
            'driver_max_shift_h': driver_max_shift_hours,
            'driver_max_working_h': driver_max_working_hours,
            'driver_mandatory_break_h': driver_mandatory_break_hours,
            'driver_shift_limit': driver_shift_limit,
            'driver_blocks_over_shift': None,
            'driver_blocks_over_driving': None,
            'driver_longest_block_h': None,
            'driver_longest_block_wheel_h': None,
            'driver_duty_blocks': None,
            'crew_breach_h': None,
            'trips_removed': len(dropped_trips or []),
            'trips_removed_ids': [d['trip_ID'] for d in (dropped_trips or [])],
            'trips_removed_km': round(sum(d['trip_distance_km']
                                          for d in (dropped_trips or [])), 1),
            'warm_start_used': warm_start_used,
            'driver_max_driving_h': driver_max_driving_hours,
            'drivers_peak_in_model': None,
            # parameters rather than results, so they are known even when the run failed -
            # a sweep CSV keeps the same columns whichever way a row went
            'v2v_status': v2v_status,
            'v2v_kWh': None,
            'v2v_steps': None,
            'v2v_saved_grid_fees_€': None,
            'v2v_saved_selling_fees_€': None,
            'v2v_saved_total_€': None,
            'v2v_forgone_v2g_€': None,
            'charging_efficiency': charging_efficiency,
            'discharging_efficiency': discharging_efficiency,
            'charging_loss_kWh': None,
            'discharging_loss_kWh': None,
            'conversion_loss_cost_€': None,
            'battery_charged_kWh': None,
            'battery_discharged_kWh': None,
            'grid_charging_kWh': None,
        }



# 5.9 the summary CSV: one file per run, one row per solve
#
# Every run produces one of these, whatever started it - a single disposition from the web
# interface, an asset-sizing range, a scenario sweep, or the command-line batch. Before,
# only the command-line batch wrote one, so a sweep run from the interface left its numbers
# nowhere except on screen: the results/ directory held the figures of the last solve and
# nothing that compared the solves to each other.
#
# The shape is the one the old potential_analysis_results files had - a row per solve, the
# swept axes as columns, the per-solve results beside them - with the columns brought up to
# what postprocess() now returns. Names changed with it: v2g_earnings_total_EUR rather than
# v2g_revenue_total, bev_amount rather than BEV_amount, and the driver, PV, V2V, deadhead
# and charger families are new since the old format was fixed. `use_case` is gone; nothing
# sets it any more.
#
# Run-level context comes first and repeats on every row. That is redundant in a database
# sense and right here: these files are read one at a time in a spreadsheet, and a row that
# cannot say which run and which fleet it came from is a row that gets quoted out of
# context.
def safe_name_part(text):
    """`text` reduced to something safe to put in a filename.

    Scenario names, years and the v2g setting all end up in the summary's name, and they
    are free text from the settings rather than identifiers. A scenario called "2030: high"
    or "worst/best" would raise on Windows the moment the file is opened - and because the
    summary is written *after* the solving, that would throw away a run that had already
    finished. Anything outside letters, digits, dot, plus and minus becomes an underscore,
    runs of underscores collapse, and the result is trimmed of the leading and trailing
    dots and spaces Windows also refuses.
    """
    import re
    cleaned = re.sub(r'[^A-Za-z0-9.+-]+', '_', str(text)).strip('._ ')
    return cleaned or 'na'


def run_summary_columns(records, run_mode, stamp, design=None):
    """The rows of one run's summary, context first, in a stable column order."""
    # the stamp carries a uniqueness suffix after the clock (1.0b), so read the time off
    # the prefix rather than parsing the whole string
    began = run_stamp_started_at(stamp)
    started = stamp if began is None else began.strftime('%Y-%m-%d %H:%M:%S')
    context = {
        'run_stamp': stamp,
        'run_mode': run_mode,
        'run_started': started,
        'solves_in_run': len(records),
        'mip_gap_setting': optimization_MIPGap,
        'fleet_operation_mode': fleet_operation_mode,
        'auto_sizing': auto_sizing,
        # which sheet of costs_dataset.xlsx priced this run (1.4b2). Reported rather than
        # left to be inferred: two rows made on different bases are not comparable, and
        # nothing else in the file says which one a row is.
        #
        # Answered for run_mode, not for the module's run_kind. They agree on every path
        # that matters, but run_mode is the one the caller asserted and the one this row
        # is filed under, so the column cannot end up disagreeing with its own header.
        'energy_price_basis': resolve_energy_price_basis(run_mode),
    }
    if design:
        # a sizing run answers with one fleet for the whole range, so the fleet is run-level
        # context and the rows below it are the days that fleet had to serve
        context.update({
            'design_fleet_size': design.get('fleet_size'),
            'design_ice_bought': design.get('ice_bought'),
            'design_bev_bought': design.get('bev_bought'),
            'design_fleet_bought_ids': design.get('fleet_bought_ids'),
            'design_capital_EUR': design.get('fleet_capital_EUR'),
            'design_ownership_per_day_EUR': design.get('ownership_cost_per_day_EUR'),
            'design_objective_EUR': design.get('objective_EUR'),
            # the same total without the steering penalties the search needed (5.8b) -
            # the one to quote as the cost of the design
            'design_total_cost_clean_EUR': design.get('total_cost_clean_EUR'),
            'design_operating_clean_EUR': design.get('operating_cost_clean_horizon_EUR'),
            'design_steering_penalties_EUR': design.get('steering_penalties_horizon_EUR'),
            'design_gap': design.get('mip_gap'),
            'design_gap_basis': design.get('gap_basis'),
            'design_search_exhausted': design.get('search_exhausted'),
            'design_search_limit_hit': design.get('search_limit_hit'),
            'design_fleets_evaluated': design.get('fleets_evaluated'),
            # counted, not inferred from the number of candidates (3.5.0) - what the
            # search actually spent, which is the figure a runtime claim rests on
            'design_day_solves': design.get('day_solves'),
            'design_day_solves_abandoned': design.get('day_solves_abandoned'),
            'design_min_fleet_size': design.get('min_fleet_size'),
            'design_chargers_needed': design.get('chargers_needed'),
        })
    rows = []
    for position, record in enumerate(records, 1):
        row = dict(context)
        row['solve_number'] = position
        row.update(record or {})
        rows.append(row)
    return rows


def write_run_summary(records, run_mode='disposition', stamp=None, design=None,
                      results_dir=None):
    """Write one run's summary CSV and return its path (None when there is nothing to write).

    stamp defaults to the run stamp opened by begin_output_run() (1.0b), so the summary
    carries the same prefix as the figures and schedule of the same run. A caller that
    drives several solves through separate module namespaces - the web interface rebuilds
    one per solve - has to pass its own, or every solve would stamp the file differently.

    Nothing this function can fail at is worth a finished run. It is called after the
    solving, from four places, none of which wrapped it: a bad character in a scenario
    name, a full disk or a file open in Excel would have raised through the caller and
    discarded results that took minutes or hours to produce. So every failure is caught,
    reported, and turned into None - the run keeps its answer and the operator is told the
    summary is missing, which is the right way round.
    """
    try:
        return _write_run_summary(records, run_mode, stamp, design, results_dir)
    except Exception as exc:
        print(f"WARNING: the run finished but its summary CSV could not be written "
              f"({type(exc).__name__}: {exc}). The results themselves are unaffected.",
              flush=True)
        return None


def _write_run_summary(records, run_mode, stamp, design, results_dir):
    """The body of write_run_summary, so the guard above has something to guard."""
    rows = run_summary_columns(records or [], run_mode, stamp or output_run_stamp, design)
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    scenarios = sorted({safe_name_part(r.get('scenario')) for r in records if r.get('scenario')})
    years = sorted({safe_name_part(r.get('year')) for r in records if r.get('year')})
    v2g = sorted({safe_name_part(r.get('v2g_status')) for r in records if r.get('v2g_status')})
    # Keep the scenario sweep distinguishable from a single disposition run. The actual
    # run mode remains in the CSV as well, but the filename is what operators see first.
    family = ('sizing' if run_mode == 'sizing'
              else 'sweep' if run_mode == 'sweep'
              else 'disposition')
    name = (f"{family}_results_"
            f"n={len(records)}_"
            f"sc={'-'.join(scenarios) or 'na'}_"
            f"y={'-'.join(years) or 'na'}_"
            f"v2g={'-'.join(v2g) or 'na'}_"
            f"MIPgap={safe_name_part(optimization_MIPGap)}.csv")
    # always prefixed, even when no run opened a stamp: the interface lists these by
    # globbing "*_<family>_results_*.csv", so an unprefixed file would sit in results/ and
    # never appear. A run with no stamp is one nobody opened a generation for, which is a
    # caller bug rather than a naming case - it gets a stamp of its own here instead.
    stamped = f"{stamp or output_run_stamp or new_run_stamp()}_{name}"
    target = (Path(results_dir) / stamped) if results_dir else project_path('results', stamped)
    ensure_result_data_dir()
    # utf-8 and not the platform default: the column names carry EUR signs, and the old
    # files in results/archive show what the platform default did to them
    frame.to_csv(target, index=False, encoding='utf-8')
    return target


# 6 MAIN
#
# 6.0 ONE SOLVE OR SEVERAL, and why the command line has to tell them apart.
#
# This entry point runs the product of the variation lists in 1.3 (scenario x year x v2g x
# day). With one entry in each that product is a single day planned once - a DISPOSITION
# run, the same thing the interface's Disposition tab does - and with more than one it is a
# SWEEP. Everything that distinguishes the two follows from run_kind (1.4b1), which this
# entry point is one of the two places that set:
#
#   figures     Every solve writes its figures to the SAME filenames: plot_suffix (5.1) is
#               only ever set by a design run, so plot_disposition_schedule.png,
#               plot_SoC_charging_power.png and the rest are one set of names shared by
#               every solve. Run one after another they overwrite each other and only the
#               last survives; run through the Pool they do it concurrently, so a file can
#               also be read half-written. That is why a sweep writes none of them - and it
#               costs a sweep nothing, because every number it reports comes back in the
#               returned record and lands in the summary CSV.
#
#               With exactly one solve there is nobody to collide with. So a command-line
#               disposition run writes its figures exactly as the interface does, and -
#               being a terminal run - opens them afterwards. That is what
#               `python main.py --no-interface` is for: the point of running it is to come
#               back and look at the output.
#
#   prices      A sweep moves the year, so it is priced off the yearly outlook; a single
#               day is priced off that day (1.4b2).
#
#   run_mode    What the summary calls itself (5.9), so a one-solve CSV is not filed under
#               'sweep' and compared against runs priced differently.
def sweep_solve(params):
    """One sweep solve: no figures, no report, prices off the yearly outlook.

    All three of those are run_kind (1.4b1), and it is set here rather than only in the
    parent because the Pool starts with 'spawn': a worker re-imports this module and would
    otherwise pick the module-level default back up. The parent sets it too, in 6.2a, for
    the sequential branch and for the summary.
    """
    global run_kind
    run_kind = 'sweep'
    return run_optimization(params)


def single_solve(params):
    """The one solve of a single-scenario command-line run - a disposition run (6.0).

    Nothing is suppressed: one solve owns the output filenames outright, so it writes its
    figures and prints its report like any other disposition run, and 1.4b1 says a
    disposition run always does. There is no switch left to consult.
    """
    global run_kind
    run_kind = 'disposition'
    return run_optimization(params)


if __name__ == '__main__':
        
    # 6.1 memory cleaning process
    def memory_cleaning():
        gc.collect()
        print('System memory cleaned!')
    schedule.every(12).hours.do(memory_cleaning)
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(7200) # every 7200 seconds
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # 6.2 multicompute execution
    multiprocessing.freeze_support()
    multiprocessing.set_start_method('spawn', force=True)
    
    ensure_runtime_context()

    param_combinations = list(itertools.product(
        scenario,
        scenario_year,
        v2g_status,
        range(1, trips_dataset_amount + 1)
    ))

    # 6.2a which kind of run this is, decided by how many solves the parameter block asks
    #      for (6.0), and written down once here. Everything else - figures, the terminal
    #      report, the price basis, what the summary calls itself - is read off it.
    #
    #      run_host stays 'terminal', which is its default and the truth: this is the
    #      command line, so the figures a disposition run writes are also shown (1.4b1).
    #
    #      Set in the PARENT as well as in the worker functions, and it has to be: the Pool
    #      starts with 'spawn', so a worker re-imports this module and never sees what was
    #      assigned here - while the parent solves nothing in the parallel branch but does
    #      write the summary. A summary reporting 'daily' for a sweep whose workers ran on
    #      'yearly' is the one piece of this nobody could check afterwards.
    single_run = len(param_combinations) == 1
    solve_one = single_solve if single_run else sweep_solve
    run_kind = 'disposition' if single_run else 'sweep'

    # one stamp for the whole sweep: its single output is the summary below, and the
    # solves that fed it are one piece of work (1.0b).
    #
    # Held in a name, because run_optimization opens a generation of its own on every solve
    # that is not build_only. In the sequential branch below those calls happen in *this*
    # process, so output_run_stamp is whatever the last day's solve set by the time the
    # summary is written - the file was stamped with one arbitrary solve's stamp instead of
    # the sweep's, and only in the sequential case, because pool workers have their own
    # module state. The summary's name therefore depended on which branch ran.
    #
    # A single run is the other way round and takes no stamp here: its one solve opens the
    # generation its figures are written under, and the summary has to land under that same
    # prefix or the run's outputs would be split across two. None means "whichever the
    # solve opened" (5.9), which is what the interface's own single run passes.
    sweep_stamp = None if single_run else begin_output_run()

    results = []
    if multiprocessing_status == 'on':
        with multiprocessing.Pool(processes=max_parallel_workers) as pool:
            # imap, not imap_unordered: the results are numbered by position below, and
            # unordered hands them back in completion order, so iteration_number named
            # whichever solve happened to finish nth rather than the nth parameter
            # combination. Every row's scenario, year and day then belonged to a different
            # iteration_number than the one it was labelled with. imap still runs the
            # tasks in parallel; it only yields them in order.
            computed_results = list(tqdm.tqdm(pool.imap(solve_one, param_combinations), total=len(param_combinations)))
            for i, (result_dict, _) in enumerate(computed_results, 1):
                result_dict['iteration_number'] = i
                results.append(result_dict)
    else:
        for i, params in enumerate(tqdm.tqdm(param_combinations), 1):
            result_tuple = solve_one(params)
            result_dict = result_tuple[0]
            result_dict['iteration_number'] = i
            results.append(result_dict)

    # 6.3 save results - the same summary every other kind of run writes (5.9)
    summary_path = write_run_summary(results, run_mode=run_kind, stamp=sweep_stamp)
    filename = summary_path.name if summary_path is not None else '(nothing to write)'
    print(f'summary: results/{filename}', flush=True)

    # 6.3b and show them, which is the other half of what a terminal run is for (1.4b1).
    #      The figures are already on disk either way; this only opens the windows, and
    #      only when a window is something this process can open - a headless server gets
    #      the Agg backend and the files, which is the case --no-interface exists for.
    if writes_outputs():
        print(f'figures: results/{output_run_stamp}_plot_*.png', flush=True)
        show_figures()

    # 6.4 send slack bot message after completion (1.5b). Silent unless asked for, and
    #     says why when it was asked for and could not: a sweep that ran for an hour and
    #     then failed to announce itself should not look like one that announced itself.
    if slack_notification_status == 'on':
        _sent, _detail = send_slack_notification(
            f'HDV Optimization completed! Results: {len(results)} iterations, '
            f'saved as {filename}')
        print(f"slack notification: {_detail}")
