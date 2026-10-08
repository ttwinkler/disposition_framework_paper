"""HDV Disposition Optimization - web interface (launcher, model bridge and Streamlit app)."""

from __future__ import annotations

import html
import itertools
import json
import logging
import multiprocessing as mp
import os
import queue
import subprocess
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    import streamlit as st
    # Defining the cached helpers below outside a Streamlit runtime (which is exactly
    # what the launcher does before it spawns Streamlit) logs a "No runtime found"
    # warning per helper. The fallback it describes is correct for that process, so the
    # notice is noise in front of the launcher's own output.
    for _noisy in ('streamlit.runtime.caching.cache_data_api',
                   'streamlit.runtime.scriptrunner_utils.script_run_context'):
        logging.getLogger(_noisy).setLevel(logging.ERROR)
except ImportError:  # keep the module importable so launch() can explain the fix
    st = None


_PYPLOT = None


def _pyplot():
    """Import pyplot on first use, with the non-interactive backend.

    Deliberately not imported at the top of the module: importing this file to call
    launch(), run_single() or the result readers should not have to pay for matplotlib.
    Only the code paths that actually draw a figure do.
    """
    global _PYPLOT
    if _PYPLOT is None:
        import matplotlib
        matplotlib.use("Agg")  # figures are rendered to buffers, never to a window
        import matplotlib.pyplot as plt
        from hdv_figure_style import use_figure_style
        _PYPLOT = use_figure_style(plt)
    return _PYPLOT


def _cached(func):
    """st.cache_data where Streamlit is available, a no-op stand-in otherwise."""
    if st is None:
        func.clear = lambda: None
        return func
    return st.cache_data(show_spinner=False)(func)


def _cached_resource(func):
    """st.cache_resource where Streamlit is available, a plain memo otherwise.

    Streamlit re-executes this file on every widget interaction, so anything kept in a
    module global would be thrown away each time. This keeps live objects (the exec'd
    model namespace) alive across reruns, which is what importing a separate module
    used to do implicitly.
    """
    if st is not None:
        # max_entries=1: the key is the source file's timestamp, so every edit makes a new
        # entry and the old namespace is of no further use - keeping them would be a slow
        # leak of whole exec'd modules across an editing session
        return st.cache_resource(show_spinner=False, max_entries=1)(func)
    memo = {}

    def wrapper(*args, **kwargs):
        key = (args, tuple(sorted(kwargs.items())))
        if key not in memo:
            memo.clear()
            memo[key] = func(*args, **kwargs)
        return memo[key]

    wrapper.clear = memo.clear
    return wrapper



# 1 SETUP
# 1.1 project layout
#     this module lives in src/ next to the model, so the root is one level up
SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent
# three directories, by what a file is rather than when it was made - see the model's
# section 1.2. inputs/ is authored and never written to, data/ holds what the
# pipeline derives from it, results/ holds the answers.
USER_DATA_DIR = PROJECT_ROOT / "inputs"
WORKING_DATA_DIR = PROJECT_ROOT / "data"
RESULT_DATA_DIR = PROJECT_ROOT / "results"

MODEL_SOURCE = SRC_DIR / "hdv_disposition_optimization.py"

ENERGY_DATASET = USER_DATA_DIR / "costs_dataset.xlsx"
DEPOT_DATASET = USER_DATA_DIR / "depot_dataset.xlsx"
FLEET_DATASET = USER_DATA_DIR / "fleet_dataset.xlsx"
ORDER_DATASET = USER_DATA_DIR / "order_dataset.xlsx"

TRIPS_CSV = WORKING_DATA_DIR / "order_trips.csv"
COST_PARAMETER_ENERGY_CSV = WORKING_DATA_DIR / "cost_parameter_yearly.csv"
COST_PARAMETER_DAILY_CSV = WORKING_DATA_DIR / "cost_parameter_hourly.csv"
DEPOT_LOAD_PROFILE_CSV = WORKING_DATA_DIR / "depot_load_profile.csv"
DEPOT_PV_PARAMETER_CSV = WORKING_DATA_DIR / "depot_pv_parameters.csv"
DEPOT_CHARGING_CSV = WORKING_DATA_DIR / "depot_charging_stations.csv"
PV_PROFILE_CACHE_JSON = WORKING_DATA_DIR / "cache_pv_profile.json"

CSV_ENCODING = "utf-8"

# the date the sidebar opens on, same as the model's own date_disposition default
DEFAULT_DISPOSITION_DATE = date(2025, 11, 7)

# shown under the run button while Gurobi is working. Drawn as our own HTML, not
# Streamlit's st.spinner: that widget is a full-width row Streamlit styles itself,
# and those styles win over anything we put on [data-testid="stSpinner"].
SOLVER_SPINNER_TEXT = "Gurobi solver is running, this may take a while"


def solver_wait_html(status: Optional[str] = None) -> str:
    """The spinner, optionally carrying where in the run it has got to.

    A range is many solves behind one message, and without a count the wheel turning looks
    the same at the first day as at the twentieth - and the same as a hang. The position
    goes inside the sentence rather than into a second line of its own: one message that
    updates is easier to read than a static one with a caption changing underneath it.
    """
    suffix = f" ({html.escape(status)})" if status else ""
    return (
        '<div class="hdv-solver-wait">'
        '<div class="hdv-solver-wheel" aria-hidden="true"></div>'
        f'<div class="hdv-solver-text">{SOLVER_SPINNER_TEXT}{suffix} ....</div>'
        "</div>"
    )


def latest_run_status(job: Dict[str, Any], mode_key: str) -> Optional[str]:
    """The newest position the worker has sent, remembered between polls.

    The page polls once a second and the worker only speaks when it moves on, so most polls
    find nothing waiting. Keeping the last message in session state is what stops the count
    blinking out between two solves - a position that vanishes for twenty minutes and then
    comes back looks exactly like the hang it exists to rule out.

    The queue is drained rather than read once: several solves can finish between two polls
    and it is the last of them the page should show, not the first.
    """
    key = f"optimization_status_{mode_key}"
    latest = st.session_state.get(key)
    progress_queue = job.get("progress")
    while progress_queue is not None:
        try:
            latest = progress_queue.get_nowait()
        except queue.Empty:
            break
        except (OSError, ValueError):
            # the worker is gone and took its queue with it; what it last said still stands
            break
    if latest:
        st.session_state[key] = latest
    return latest


# the environment variable holding the Slack bot token, the same name the model reads in
# its section 1.5b. Named here only so the Settings tab can say whether it is set before
# the model namespace has been built; the token itself is never read by this module and
# never passed to the model - the model takes it from the environment when it sends.
SLACK_TOKEN_ENV = "SLACK_BOT_TOKEN"
SLACK_DEFAULT_CHANNEL = "python-updates"

# every derived input, with the script that produces it from the Excel datasets
DERIVED_INPUTS = {
    TRIPS_CSV: "src/hdv_trip_generation.py",
    COST_PARAMETER_ENERGY_CSV: "src/hdv_cost_parameter_generation.py",
    COST_PARAMETER_DAILY_CSV: "src/hdv_cost_parameter_generation.py",
    DEPOT_LOAD_PROFILE_CSV: "src/hdv_depot_load_profile_generation.py",
    DEPOT_PV_PARAMETER_CSV: "src/hdv_depot_load_profile_generation.py",
    DEPOT_CHARGING_CSV: "src/hdv_depot_load_profile_generation.py",
    PV_PROFILE_CACHE_JSON: "src/hdv_pv_profile_generation.py",
}


# 1.2 make the model modules importable from anywhere
def _ensure_src_on_path() -> None:
    """src/ must precede the project root so bare imports inside the model resolve."""
    for entry in (str(PROJECT_ROOT), str(SRC_DIR)):
        if entry in sys.path:
            sys.path.remove(entry)
    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(SRC_DIR))


_ensure_src_on_path()


def project_path(*parts: str) -> Path:
    """Return an absolute path under the project root."""
    return PROJECT_ROOT.joinpath(*parts)



# 2 DERIVED INPUTS
# 2.1 report which derived inputs are present
def missing_derived_inputs() -> List[Tuple[Path, str]]:
    """Derived files the model needs that have not been generated yet, or are stale.

    `cache_pv_profile.json` is treated as missing when it is present but no longer
    matches `order_dataset.xlsx` and `depot_dataset.xlsx` — the same fingerprint
    check `trips.csv` uses, except this one is rebuilt on sight rather than left
    for the operator.
    """
    missing = [(path, script) for path, script in DERIVED_INPUTS.items() if not path.exists()]
    if PV_PROFILE_CACHE_JSON.exists():
        try:
            from hdv_pv_profile_generation import pv_cache_matches_sources
            current = pv_cache_matches_sources()
        except Exception as exc:
            note_load_failure('missing_derived_inputs', exc)
            current = False
        if not current:
            missing.append((PV_PROFILE_CACHE_JSON, DERIVED_INPUTS[PV_PROFILE_CACHE_JSON]))
    return missing


def missing_primary_inputs() -> List[Path]:
    """Excel datasets in inputs/ that are absent."""
    return [p for p in (ENERGY_DATASET, DEPOT_DATASET, FLEET_DATASET, ORDER_DATASET)
            if not p.exists()]


# 2.2 regenerate the derived inputs from the Excel datasets
def prepare_inputs(force_routing: bool = False, make_plots: bool = False) -> Dict[str, Any]:
    """Run the generators that turn inputs/*.xlsx into the derived data/ files."""
    _ensure_src_on_path()

    from hdv_cost_parameter_generation import generate_cost_parameters
    from hdv_depot_load_profile_generation import generate_depot_load_profile
    from hdv_trip_generation import generate_trips
    from hdv_pv_profile_generation import generate_pv_profile_cache

    energy_df, v2g_df = generate_cost_parameters(make_plots=make_plots)
    profiles = generate_depot_load_profile(make_plots=make_plots)
    trips = generate_trips(force=force_routing)
    pv_cache = generate_pv_profile_cache()

    return {
        "years": (int(energy_df["Year"].min()), int(energy_df["Year"].max())),
        # the V2G curve is hourly over one operating day. It used to be a handful of named
        # 4h periods in a 'time' column, and this still asked for that column long after it
        # was gone - the rebuild button failed on a KeyError for a value nothing displayed.
        # Reported in the success message now, so a schema drift breaks something visible.
        "v2g_hours": int(len(v2g_df)),
        "depot_sheets": list(profiles),
        "pv_plant": load_pv_site_parameters(),
        "charging_stations": len(load_charging_stations()),
        "trips": len(trips),
        "days": int(trips["day_ID"].nunique()),
        "pv_days": int(len(pv_cache.get("profiles") or {})),
        "pv_trip_dates": int(len(pv_cache.get("trip_dates") or [])),
    }



# 3 MODEL NAMESPACE
# 3.1 execute the model source into an isolated namespace, once per server process
@_cached_resource
def _exec_model_source(source_mtime: float) -> Dict[str, Any]:
    """exec the model into a fresh namespace and keep it across Streamlit reruns.

    Keyed on the source file's timestamp, which is the whole reason the parameter exists -
    it is never read. Without it the namespace was cached for the life of the server
    process with no key at all, so editing the model changed nothing until the app was
    restarted: Streamlit hot-reloads *this* file when it changes and the model stayed as it
    was, leaving the two halves of the app disagreeing.

    That is not hypothetical. Renaming the figures left a running app looking for the old
    filenames while the model wrote the new ones, and exactly one figure of five happened
    to still match - which reads as "the run only produced one figure" and is nothing of
    the kind. Same keying as read_result_csv() uses for the CSVs.
    """
    source = MODEL_SOURCE.read_text(encoding="utf-8")

    # never let the batch entry point of the model run inside the web process
    marker = "if __name__ == '__main__':"
    if marker in source:
        source = source.split(marker, 1)[0]

    ns = {"__name__": "hdv_opt_web", "__file__": str(MODEL_SOURCE)}
    try:
        exec(compile(source, str(MODEL_SOURCE), "exec"), ns)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"{exc}. The model's 'src' directory is not on Python's import path. "
            "Start the app from the 02_Modell folder with 'python main.py'."
        ) from exc
    ns[_DEFAULTS_KEY] = _model_defaults(ns)
    return ns


# the parameters as the model file declares them, taken once while the namespace is still
# untouched. load_optimization_namespace() restores from this before it applies a run's
# overrides - see there for why that matters.
_DEFAULTS_KEY = "__model_defaults__"


def _model_defaults(ns: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot every plain configuration value in a freshly exec'd model namespace.

    Only data, and only the kinds of data the parameter block at the top of the model is
    written in. Functions, classes and modules are skipped because they are never
    overridden and copying them is either meaningless or impossible; the derived state
    build_runtime_context() computes is skipped for the same reason it is recomputed on
    every load - it is output, not input.
    """
    import copy

    snapshot: Dict[str, Any] = {}
    for key, value in ns.items():
        if key.startswith("__"):
            continue
        if isinstance(value, (str, int, float, bool, type(None), tuple, list, dict, set)):
            try:
                snapshot[key] = copy.deepcopy(value)
            except Exception:
                pass          # anything that will not copy is not a parameter
    return snapshot


def load_optimization_namespace(overrides: Optional[Dict[str, Any]] = None,
                                reuse: bool = True) -> Dict[str, Any]:
    """Load the optimization source, apply overrides, rebuild derived state.

    Returns the namespace dict containing all top-level names and functions.

    A missing derived input is not an error here: the model's own
    ensure_derived_inputs() builds whatever is absent in data/ when
    build_runtime_context() runs below.
    """
    if not MODEL_SOURCE.exists():
        raise FileNotFoundError(f"Cannot find the optimization source at {MODEL_SOURCE}")

    _ensure_src_on_path()

    if not reuse:
        _exec_model_source.clear()
    ns = _exec_model_source(MODEL_SOURCE.stat().st_mtime)

    # 3.1b back to the model's own defaults before this run's overrides go on.
    #
    # The namespace is cached for the life of the process (3.1), so without this an
    # override outlives the run that set it: a parameter this call does not mention keeps
    # whatever the *previous* call put there. Two runs in one session were therefore not
    # comparable unless every key either run touched was sent by both - and the UI only
    # sends the keys its own widgets own, so anything set from elsewhere stuck silently.
    #
    # Restoring beats re-exec'ing the source, which is the obvious alternative and costs
    # about three seconds a run; this is a dict copy of a few hundred small values and does
    # not measurably cost anything. Only the snapshot's own keys are restored, so functions
    # and classes are left alone - they were never overridden.
    import copy

    for key, value in ns.get(_DEFAULTS_KEY, {}).items():
        ns[key] = copy.deepcopy(value)

    # 3.2 apply the UI overrides on top of the model defaults
    for key, value in dict(overrides or {}).items():
        ns[key] = value

    # 3.3 let the model recompute everything that depends on those parameters
    ns["build_runtime_context"]()

    # 3.4 single UI runs are always executed in-process
    ns["multiprocessing_status"] = "off"

    return ns


# 3.5 the 4-tuple expected by run_optimization for a single scenario
#     the fleet is not part of it: the model always uses inputs/fleet_dataset.xlsx
def build_single_params(ns: Dict[str, Any], chosen_day: Optional[int] = None) -> Tuple:
    scenario = ns.get("scenario", ["best case"])[0]
    year = ns.get("scenario_year", [2025])[0]
    v2g = ns.get("v2g_status", ["on"])[0]

    available_days = sorted(ns["all_trips"]["day_ID"].unique())
    if chosen_day is None or int(chosen_day) not in available_days:
        chosen_day = available_days[0]

    return (scenario, year, v2g, int(chosen_day))


# 3.5b the stamp a run's outputs share
#
# The model opens one per run (its section 1.0b), but a sweep rebuilds the model namespace
# for every solve - date_disposition and the prices have to be re-derived each time - so
# each solve would open its own and the summary of the run would have nowhere to sit. The
# interface therefore takes the stamp once and hands it to the summary writer.
#
# Same shape as the model's own stamp (its 1.0b): a second-resolution clock for sorting,
# then the process id and a counter so that two runs started in the same second - which is
# ordinary here, because the interface answers requests as fast as they arrive - cannot
# write over each other's outputs. Mirrored rather than imported: loading the model
# namespace executes its whole configuration, which is far too much work for a filename.
_run_stamp_serial = itertools.count()


def new_run_stamp() -> str:
    return (f"{datetime.now().strftime('%y%m%d%H%M%S')}"
            f"-{os.getpid() % 0x10000:04x}{next(_run_stamp_serial) % 0x1000:03x}")


# 3.5c the scenario a run is made under
#
# These four are not preferences, they are the question: which price world, which year,
# whether V2G is allowed, whether a truck may charge away from the depot. They used to sit
# under 3 - Settings with the physical parameters, which put the thing most likely to change
# between two runs furthest from the button that starts them. They now render inside the
# run tab that uses them.
#
# Disposition and Asset Sizing each get their own copy, and two widgets cannot share a
# Streamlit key - so, as with the day range, neither widget is the setting: the entry in
# session state is, each copy writes into it, and each is seeded from it on the next run.
# Without that the two would drift and which one a run believed would come down to the
# order the tabs happen to render in.
#
# The Scenario Sweep tab has no copy on purpose: a sweep varies these axes itself, and a
# fixed value beside the axis that overrides it is a contradiction on the page.
SCENARIO_STATE = {
    "run_scenario": "best case",
    "run_year": None,
    "run_v2g": "on",
    "run_external_charging": "on",
}

# The Settings tab's own case of the same thing, and a simpler one: a single widget owns
# this key, so it is the setting rather than a copy of it. It exists because the Solver
# section renders before the Fleet Operation one and its warm start applies to a crewed
# fleet only - reading the local would be reading a name that does not exist yet. On the
# first render the key is absent and the default below stands in, which is the same value
# the selectbox starts at; from then on session state holds the user's choice before the
# script reaches either section.
OPERATION_MODE_KEY = "settings_operation_mode"


def _sync_scenario(state_key: str, widget_key: str):
    st.session_state[state_key] = st.session_state[widget_key]


def scenario_controls(mode_key: str, scenario_years: List[int]) -> None:
    """The run-scenario selectboxes for one run tab - those that still decide something.

    A Disposition run is priced from `energy_daily` alone (model 1.4b2), and that sheet
    carries neither a year nor a low/medium/high band. Cost scenario and Scenario year
    therefore cannot move a single price on that tab, so they are not offered there: a
    control that changes nothing is worse than no control, because it reads as a
    comparison the run can make. An Asset Sizing run is priced off the outlook and keeps
    both. Either way the model is still handed a year and a scenario - they travel with
    the result as labels - from SCENARIO_STATE, which is where they already lived.
    """
    years = scenario_years or [2025]
    if st.session_state.get("run_year") not in years:
        st.session_state["run_year"] = years[0]
    fields = [
        ("run_scenario", "Cost scenario", ["best case", "worst case"], None),
        ("run_year", "Scenario year", years, None),
        ("run_v2g", "V2G", ["on", "off"], None),
        ("run_external_charging", "External charging", ["on", "off"],
         "Whether a truck may charge at a public station away from the depot. The energy is "
         "priced at `public_charging_price_EUR/kWh` from `costs_dataset.xlsx`, and the "
         "driver time it costs is priced separately under *3 - Settings -> External charging "
         "& driving breaks*. Off means the day has to be run on depot charging alone."),
    ]
    if mode_key == "disposition":
        fields = [field for field in fields
                  if field[0] not in ("run_scenario", "run_year")]
    columns = st.columns(len(fields))
    for column, (state_key, label, options, help_text) in zip(columns, fields):
        widget_key = f"{state_key}_{mode_key}"
        current = st.session_state.setdefault(state_key, SCENARIO_STATE[state_key])
        st.session_state[widget_key] = current if current in options else options[0]
        with column:
            st.selectbox(label, options=options, key=widget_key, help=help_text,
                         on_change=_sync_scenario, args=(state_key, widget_key))


# 3.6 run exactly one scenario
def run_single(overrides: Dict[str, Any]) -> Tuple[Dict[str, Any], Any, Tuple[bool, str]]:
    """Run one optimization scenario with the supplied overrides.

    run_kind 'disposition' is what makes this a disposition run, and two rules follow from
    it in the model (its 1.4b1 / 1.4b2) rather than from anything set here:

      it writes its figures into results/ - a disposition run always does, and this is the
      run whose whole output they are. run_host 'interface' is the other half: the Results
      tab displays those PNGs itself, so the model saves them and opens no window.

      it is priced off `energy_daily` entire - the two curves with their own shape and
      level, the public charger and the diesel as the single numbers they are, no
      rescaling onto a year. Every price in the result traces back to a cell in that
      sheet, and the scenario year and band reach none of them.

    Returns the result, the vehicle types, and the outcome of the Slack notification as
    (sent, detail). The notification is sent from here rather than left to the model's
    own section 6.4, because that lives under the batch entry point and _exec_model_source
    cuts the source off before it - a run started in this interface would otherwise never
    announce itself, however the switch was set.
    """
    settings: Dict[str, Any] = {"run_kind": "disposition", "run_host": "interface",
                                "auto_sizing": "off"}
    settings.update(overrides or {})

    chosen_day = settings.pop("chosen_day", None)
    ns = load_optimization_namespace(settings)
    params = build_single_params(ns, chosen_day=chosen_day)

    result_dict, vehicle_types = ns["run_optimization"](params)
    # one summary per run, the same file every other kind of run writes (model 5.9). The
    # model opened its own stamp for the figures of this solve, so the summary reuses it
    # and the whole run lands under one prefix.
    ns["write_run_summary"]([result_dict], run_mode="disposition")
    # send_slack_notification checks the switch itself and never raises, so a result that
    # took minutes cannot be lost to a failed notification
    notice = ns["send_slack_notification"](_slack_run_summary(result_dict, params))
    return result_dict, vehicle_types, notice


def _optimization_worker(result_queue, progress_queue, mode_key, overrides, day_dates,
                         sweep_specs):
    """Run one UI optimization outside Streamlit so its stop control stays clickable.

    progress_queue carries where the run has got to back to the page. Moving the solve out
    of Streamlit is what took that away: the callbacks used to write to a placeholder
    directly, and a child process has no placeholder to write to. So the position is
    formatted here, as a finished sentence, and the page only has to display it.

    Nothing put on that queue is allowed to matter. This process holds the only copy of a
    result that may have taken hours, and a status line that cannot be delivered is a
    cosmetic loss - so every send is guarded and none of them can end the run.
    """
    def say(status):
        try:
            progress_queue.put_nowait(status)
        except Exception:
            pass

    try:
        if day_dates and overrides.get("auto_sizing") == "on":
            # the namespace build and the candidate pool come before the first phase tick
            # and neither is instant, so the message starts with something rather than
            # sitting countless for the first few seconds
            say(design_status("prepare", 0, len(day_dates)))
            design, runs, notice = run_design_range(
                overrides, day_dates, day_offset=day_dates[0][0],
                progress=lambda phase, done, total: say(
                    design_status(phase, done, total)))
            payload = {"design": design, "runs": runs, "slack_notice": notice}
        elif day_dates:
            runs, notice = run_scenario_sweep(
                overrides, day_dates, sweep_specs or [{}],
                progress=lambda position, total, _spec: say(f"{position} of {total}"))
            payload = {"runs": runs, "slack_notice": notice}
        else:
            result, vehicle_types, notice = run_single(overrides)
            payload = {"result": result, "vehicle_types": list(vehicle_types),
                       "slack_notice": notice}
        result_queue.put({"ok": True, "mode_key": mode_key, **payload})
    except BaseException as exc:
        result_queue.put({"ok": False, "error": f"{exc}\n{traceback.format_exc()}"})


def _store_optimization_payload(mode_key, payload):
    """Store a completed child-process result in the same state as an inline run."""
    if payload.get("design") is not None:
        st.session_state[f"last_design_{mode_key}"] = payload["design"]
    runs = payload.get("runs")
    if runs is not None:
        if not runs:
            return "The range held no days to solve."
        st.session_state[f"last_runs_{mode_key}"] = runs
        st.session_state[f"last_result_{mode_key}"] = runs[-1]["result"]
        st.session_state[f"last_vehicle_types_{mode_key}"] = runs[-1]["vehicle_types"]
        solved = sum(1 for run in runs
                     if str(run["result"].get("optimization_status")) in SOLVED_STATUSES)
        return f"Finished {len(runs)} day(s) — {solved} optimal."
    st.session_state.pop(f"last_runs_{mode_key}", None)
    st.session_state[f"last_result_{mode_key}"] = payload["result"]
    st.session_state[f"last_vehicle_types_{mode_key}"] = payload["vehicle_types"]
    return "Optimization finished. Go to the Results section"


@_cached
def cached_duration_filtered_trip_counts(cache_signature, day_specs):
    """Count trips by day using only the canonical duration filter."""
    from hdv_trip_generation import MAX_TRIP_DURATION_H, MIN_TRIP_DURATION_H

    if not TRIPS_CSV.exists():
        return tuple(0 for _day, _date_text in day_specs)
    trips = pd.read_csv(TRIPS_CSV, encoding=CSV_ENCODING,
                        usecols=["day_ID", "trip_duration_h"])
    trips = trips[trips["trip_duration_h"].between(
        MIN_TRIP_DURATION_H, MAX_TRIP_DURATION_H, inclusive="both")]
    counts = trips.groupby("day_ID").size()
    return tuple(int(counts.get(day, 0)) for day, _date_text in day_specs)


# 3.6c the design run: one fleet for every day of the range
def design_status(phase: str, done: int, total: Optional[int]) -> str:
    """Where a design run has got to, in a few words for the spinner.

    A sizing run is not N solves in a row, so it cannot honestly be reported as "3 of 20".
    It walks three phases and only two of them have a known length:

      bound   one cheap solve per day, to floor the search (model 3.5.4)
      search  a lattice of candidate fleets, each costed over the days it needs - how
              many is the answer, not an input, so there is no denominator to give. This
              is the same reason day_solves is counted rather than inferred (model 3.5.0).
      solve   the winning fleet re-solved on every day, which is what the results show

    Naming the phase is what makes a count with no total readable rather than puzzling.
    """
    if phase == 'prepare':
        return f"reading the inputs, {total} day(s) to size"
    if phase == 'bound':
        # done == 0 is the phase starting, before the first day has come back
        return ("bounding the days" if not done
                else f"bounding day {done} of {total}")
    if phase == 'search':
        return f"searching fleets, {done} costed"
    if phase == 'solve':
        return f"final solve, day {done} of {total}"
    return f"{done} of {total}" if total else str(done)


def run_design_range(overrides: Dict[str, Any], day_dates: List[Tuple[int, str]],
                     day_offset: int, progress=None) -> Tuple[Dict[str, Any],
                                                              List[Dict[str, Any]],
                                                              Tuple[bool, str]]:
    """Size one fleet against the whole range in a single solve.

    Unlike run_day_range this is *not* a day at a time: the ownership decision is shared by
    every day, which is the only way a truck bought for Tuesday can be free on Wednesday.
    See the model's section 3.5.

    day_offset is the first raw day_ID of the range; the model renumbers the range from 1,
    so a raw day becomes raw - offset + 1.

    run_kind 'sizing' carries both rules the model needs (its 1.4b1 / 1.4b2): the days of
    a design run write no figures - they would all write the same filenames - and the
    prices come from the scenario year and band of `energy_yearly`, with the intraday
    shape of `energy_daily` rescaled onto the two curves. The ownership decision is
    shared by the whole range and has to answer to the year it is made for, while the
    shape has to survive or the arbitrage the fleet is partly sized for prices at zero.
    """
    settings: Dict[str, Any] = dict(overrides or {})
    settings.pop("chosen_day", None)
    settings["auto_sizing"] = "on"
    settings["run_kind"] = "sizing"
    settings["run_host"] = "interface"
    # one namespace for the whole range - each day passes its own depot and PV curves into
    # the model, so the date this was built for does not reach the solve
    ns = load_optimization_namespace(settings)

    specs = [(raw_day - day_offset + 1, date_text) for raw_day, date_text in day_dates]
    design, day_results = ns["run_design_optimization"](
        specs,
        ns.get("scenario", ["best case"])[0],
        ns.get("scenario_year", [2025])[0],
        ns.get("v2g_status", ["on"])[0],
        show_progress=False,
        progress=progress,
    )
    runs = []
    for (raw_day, date_text), result in zip(day_dates, day_results):
        runs.append({"day": raw_day, "date": date_text, "result": result,
                     "vehicle_types": []})
    # the fleet is run-level context and the rows are the days it had to serve (model 5.9)
    ns["write_run_summary"]([r["result"] for r in runs], run_mode="sizing", design=design)
    notice = ns["send_slack_notification"](_slack_design_summary(design, day_dates))
    return design, runs, notice


def _slack_design_summary(design: Dict[str, Any], day_dates: List[Tuple[int, str]]) -> str:
    return " | ".join([
        f"HDV design run finished - {design['days']} day(s), "
        f"{day_dates[0][1]} to {day_dates[-1][1]}",
        f"fleet: {design['ice_bought']} ice + {design['bev_bought']} bev",
        f"capital {design['fleet_capital_EUR']:,.0f} EUR",
        f"gap {design['mip_gap']:.1%}",
    ])


# 3.6b every day of a range, one solve each
def run_day_range(overrides: Dict[str, Any], day_dates: List[Tuple[int, str]],
                  progress=None) -> Tuple[List[Dict[str, Any]], Tuple[bool, str]]:
    """Solve each day of `day_dates` in turn and return one record per day.

    day_dates is [(day_ID in data/order_trips.csv, 'DD.MM.YYYY'), ...] over the whole range,
    in order. The model renumbers the range from 1 (order_data_days, section 2.1), so the
    n-th entry here is the model's day n - that renumbering is the only reason this can
    walk the range by position.

    The namespace is rebuilt per day rather than once for the range. date_disposition is
    what the depot's metered load and the PVGIS curve are read for, and build_runtime_context()
    reads both once; holding the namespace and only changing the day number would plan every
    day of the range against the first one's weather and the first one's site load.

    One notification for the range, not one per day - a message per solve on a five-day
    range is a message nobody reads.
    """
    return run_scenario_sweep(overrides, day_dates, [{}], progress=progress)


# the levers a sweep may vary on top of the days, and the model parameter each one sets.
# Anything not listed here is held at whatever the sidebar says, which is what keeps a
# sweep a comparison: one thing moves at a time unless you ask for more.
SWEEP_AXES = (
    ("year", "scenario_year", lambda v: [int(v)]),
    ("scenario", "scenario", lambda v: [v]),
    ("v2g", "v2g_status", lambda v: [v]),
    ("external_charging", "external_charging_status", lambda v: v),
)


def sweep_combinations(day_dates: List[Tuple[int, str]], years, price_scenarios,
                       v2g_modes, external_modes) -> List[Dict[str, Any]]:
    """Every (scenario, day) the sweep will solve, in the order it will solve them.

    Days innermost, so one scenario's days come out together and a table of the results
    reads down a scenario rather than across all of them.
    """
    combos = []
    for year in years:
        for price in price_scenarios:
            for v2g in v2g_modes:
                for external in external_modes:
                    for raw_day, date_text in day_dates:
                        combos.append({
                            "year": int(year), "scenario": price, "v2g": v2g,
                            "external_charging": external,
                            "day": raw_day, "date": date_text,
                        })
    return combos


def run_scenario_sweep(overrides: Dict[str, Any], day_dates: List[Tuple[int, str]],
                       scenario_specs: List[Dict[str, Any]],
                       progress=None) -> Tuple[List[Dict[str, Any]], Tuple[bool, str]]:
    """Solve every (scenario, day) combination in turn and return one record per solve.

    day_dates is [(day_ID in data/order_trips.csv, 'DD.MM.YYYY'), ...] over the whole range,
    in order. The model renumbers the range from 1 (order_data_days, section 2.1), so the
    n-th entry is the model's day n - that renumbering is the only reason this can walk the
    range by position.

    scenario_specs is the list of scenario settings to run each day under; [{}] means "just
    the sidebar's". A namespace is rebuilt per solve rather than per range: date_disposition
    decides the depot's metered load and the PVGIS curve, and the scenario axes decide the
    prices, so holding one namespace would plan every combination against the first one's
    weather and the first one's prices.

    One notification for the whole sweep, not one per solve.

    run_kind 'sweep' carries both rules the model needs (its 1.4b1 / 1.4b2): no solve
    writes figures - they would all write the same filenames, and every number is in the
    summary CSV anyway - and the prices come from each solve's own year and band in
    `energy_yearly`, with the intraday shape of `energy_daily` rescaled onto the two
    curves. The level is the axis being compared; the shape is held constant across the
    comparison rather than removed from it.
    """
    # "off" and not overridable: a sweep is many solves writing one set of filenames
    stamp = new_run_stamp()
    settings: Dict[str, Any] = {"auto_sizing": "off"}
    settings.update(overrides or {})
    # not overridable: a sweep is a sweep, and what that means for its figures and its
    # prices is the model's 1.4b1 / 1.4b2 rather than anything this page decides
    settings["run_kind"] = "sweep"
    settings["run_host"] = "interface"
    settings.pop("chosen_day", None)   # the sweep decides the day, one at a time

    position_of_day = {raw_day: i + 1 for i, (raw_day, _d) in enumerate(day_dates)}
    plan = scenario_specs if any(s.get("day") for s in scenario_specs) else [
        {**spec, "day": raw_day, "date": date_text}
        for spec in scenario_specs for raw_day, date_text in day_dates
    ]

    runs: List[Dict[str, Any]] = []
    namespace = None
    for index, spec in enumerate(plan, start=1):
        raw_day, date_text = spec["day"], spec["date"]
        if progress is not None:
            progress(index, len(plan), spec)
        per_run = {**settings, "date_disposition": date_text}
        for key, parameter, wrap in SWEEP_AXES:
            if spec.get(key) is not None:
                per_run[parameter] = wrap(spec[key])
        namespace = load_optimization_namespace(per_run)
        params = build_single_params(namespace, chosen_day=position_of_day[raw_day])
        result_dict, vehicle_types = namespace["run_optimization"](params)
        runs.append({
            "day": raw_day, "date": date_text, "result": result_dict,
            "vehicle_types": list(vehicle_types),
            # the scenario this solve belongs to, so a table of them can be read
            "scenario": spec.get("scenario"), "year": spec.get("year"),
            "v2g": spec.get("v2g"), "external_charging": spec.get("external_charging"),
        })

    if namespace is None:
        return runs, (False, "nothing to sweep")
    # one summary for the whole sweep, not one per solve: the point of a sweep is the
    # comparison between its solves, so they belong in one table. The stamp is the one
    # taken at the top of this function, because each solve built its own namespace and
    # opened its own (model 5.9 / 1.0b).
    namespace["write_run_summary"](
        [r["result"] for r in runs], run_mode="sweep", stamp=stamp)
    return runs, namespace["send_slack_notification"](_slack_range_summary(runs))


# 3.6a what the notification says. Enough to tell one run from another without opening
#      the interface: which day, whether it solved, and the headline numbers.
def _slack_range_summary(runs: List[Dict[str, Any]]) -> str:
    """One line for a whole range: how many days, how many solved, what they cost."""
    if not runs:
        return "HDV design run finished - no days in the range"
    solved = [r for r in runs
              if str(r["result"].get("optimization_status")) in SOLVED_STATUSES]
    costs = [r["result"].get("energy_costs_€") for r in solved]
    costs = [c for c in costs if isinstance(c, (int, float)) and not is_missing(c)]
    fleets = [r["result"].get("fleet_size") for r in solved
              if isinstance(r["result"].get("fleet_size"), int)]
    parts = [f"HDV design run finished - {len(runs)} day(s), "
             f"{runs[0]['date']} to {runs[-1]['date']}",
             f"{len(solved)} of {len(runs)} solved"]
    if fleets:
        parts.append(f"largest fleet needed: {max(fleets)}")
    if costs:
        parts.append(f"energy {sum(costs):,.0f} EUR over the range")
    return " | ".join(parts)


def _slack_run_summary(result: Dict[str, Any], params: Tuple) -> str:
    scenario, year, v2g, day = params
    status = result.get("optimization_status", "unknown")
    date_text = result.get("disposition_date", f"day {day}")
    energy = result.get("energy_costs_€")
    drivers = result.get("drivers_required")
    parts = [f"HDV disposition run finished - {date_text} ({scenario}, {year}, V2G {v2g})",
             f"status: {status}"]
    if isinstance(energy, (int, float)):
        parts.append(f"energy {energy:,.0f} EUR")
    if isinstance(drivers, int):
        parts.append(f"{drivers} drivers")
    return " | ".join(parts)


# 3.6b the intraday depot curves the model derives for one disposition date
def depot_day_profiles(overrides: Dict[str, Any]) -> Dict[str, List[float]]:
    """Baseline load, PV generation and PV surplus [kW] on the 30-min grid.

    Read back from the model's own build_runtime_context() rather than recomputed here,
    so what the interface plots is exactly what the MILP is given. The PVGIS year
    behind it is cached in data/cache_pv_profile.json for every day in trips.csv,
    so asking for a date twice costs nothing.
    """
    settings = dict(overrides or {})
    settings.pop("chosen_day", None)  # a run argument, not a model parameter
    ns = load_optimization_namespace(settings)
    step_hours = float(ns["STEP_HOURS"])
    return {
        "baseline_kW": [float(v) for v in ns["depot_baseline_load_kW"]],
        "pv_kW": [float(v) for v in ns["pv_generation_kW"]],
        # the model carries the surplus as energy per step; as a curve it reads better in kW
        "pv_surplus_kW": [float(v) / step_hours for v in ns["pv_charging_available_kWh"]],
        "step_hours": step_hours,
    }


# 3.7 the days available in the current trip set
def available_trip_days() -> List[int]:
    """day_ID values present in data/order_trips.csv (empty if it has not been built)."""
    if not TRIPS_CSV.exists():
        return []
    try:
        trips = pd.read_csv(TRIPS_CSV, encoding=CSV_ENCODING, usecols=["day_ID"])
    except Exception as exc:
        note_load_failure(TRIPS_CSV, exc)
        return []
    return sorted(int(d) for d in trips["day_ID"].unique())


# 3.8 the scenario years covered by the generated cost parameters
def trip_dates_by_day() -> Dict[int, Any]:
    """The calendar date each routed day falls on, from data/order_trips.csv.

    The disposition date is not a free choice for a single day: the trips of day 7 were
    driven on a particular date, and that date is what decides the depot's metered load and
    the PV yield the day is planned against. Reading it here rather than asking for it
    removes a way to plan a January day against July sunshine.
    """
    path = TRIPS_CSV
    if not path.exists():
        return {}
    try:
        frame = pd.read_csv(path, usecols=['day_ID', 'trip_date'], encoding=CSV_ENCODING)
    except Exception as exc:
        note_load_failure(path, exc)
        return {}
    frame['trip_date'] = pd.to_datetime(frame['trip_date'], errors='coerce')
    frame = frame.dropna(subset=['trip_date'])
    if frame.empty:
        return {}
    # a day should carry one date; if the order data disagrees, the earliest is the day
    return {int(day): group['trip_date'].min()
            for day, group in frame.groupby('day_ID')}


def trip_counts_by_day() -> Dict[int, int]:
    """How many trips each routed day holds - shown beside the date being planned."""
    path = TRIPS_CSV
    if not path.exists():
        return {}
    try:
        frame = pd.read_csv(path, usecols=['day_ID'], encoding=CSV_ENCODING)
    except Exception as exc:
        note_load_failure(TRIPS_CSV, exc)
        return {}
    return {int(day): int(count) for day, count in frame['day_ID'].value_counts().items()}


def available_scenario_years() -> List[int]:
    if not COST_PARAMETER_ENERGY_CSV.exists():
        return [2025]
    try:
        years = pd.read_csv(COST_PARAMETER_ENERGY_CSV, encoding=CSV_ENCODING, usecols=["Year"])
    except Exception as exc:
        note_load_failure(COST_PARAMETER_ENERGY_CSV, exc)
        return [2025]
    return sorted(int(y) for y in years["Year"].unique())



# 4 RESULT ARTEFACTS
# 4.1 previously computed batch result tables
def get_latest_summary_results(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every run-summary CSV in results/, newest first. limit=None means all of them.

    The frame is not read here. Listing is a directory scan; reading every summary would
    parse each of them on every rerun to fill a dropdown from which exactly one is opened.
    Each entry carries a `load()` instead, and the caller reads the one it shows.
    """
    if not RESULT_DATA_DIR.is_dir():
        return []
    # the leading run stamp (model 1.0b) sits in front of the descriptive name, so the
    # patterns have to allow it - and sorting by name then puts the newest run first, which
    # mtime does not: a design run writes its summary when it finishes, hours after the
    # stamp it carries. Two families rather than one wildcard, so this cannot start
    # collecting some other CSV that happens to have "results" in its name.
    files = sorted(
        [path for pattern in ("*_disposition_results_*.csv", "*_sizing_results_*.csv",
                      "*_sweep_results_*.csv")
         for path in RESULT_DATA_DIR.glob(pattern)],
        reverse=True)
    if limit is not None:
        files = files[:limit]
    return [{"path": str(path), "name": path.name, "mtime": path.stat().st_mtime,
             "load": (lambda p=path: read_result_csv(p))}
            for path in files]


# 4.2 the detailed schedule of the last single run
#
# Run outputs carry the stamp of the run that wrote them (model section 1.0b), so the name
# is <YYMMDDHHMMSS>_disposition_schedule.csv and the fixed path is only what older runs
# left behind. Newest-by-name rather than by mtime: the stamp is the run's start and sorts
# lexically, where mtime is when the file happened to be flushed - which for a design run
# writing one set per day over hours is a different ordering.
def _newest_output(suffix: str) -> Optional[Path]:
    """The newest stamped output of this kind, or None.

    Stamped only. There used to be a fallback to the bare filename, for the outputs runs
    wrote before they carried a stamp - and what it actually did was present a figure from
    an arbitrarily old run as the current one, with nothing on the page saying so. A run
    that has not produced this output yet has not produced it; that is what None means.
    """
    if not RESULT_DATA_DIR.is_dir():
        return None
    stamped = sorted(RESULT_DATA_DIR.glob(f"*_{suffix}"), reverse=True)
    return stamped[0] if stamped else None


def load_schedule_csv() -> Optional[pd.DataFrame]:
    path = _newest_output("disposition_schedule.csv")
    if path is None:
        return None
    try:
        return pd.read_csv(path, encoding=CSV_ENCODING)
    except Exception as exc:
        note_load_failure('load_schedule_csv', exc)
        return None


# 4.3 generated figures
#
# The figures the optimization itself writes, captioned, in the order they are shown. One
# list rather than a literal at each use: the Latest and the Previous sections show the
# same kinds of output and a second copy is how they drift into showing different sets.
# The grid design (src/hdv_grid_plots.py) is the one the model draws, and now the only
# one: the bar, Gantt and curve versions behind DRAW_LEGACY_SCHEDULE_FIGURES were a
# second view of figures already listed here and have been removed.
MODEL_FIGURES: Tuple[Tuple[str, str], ...] = (
    ("disposition schedule", "plot_disposition_schedule.png"),
    ("driver roster", "plot_driver_schedule.png"),
    ("depot power overview", "plot_depot_power_overview.png"),
    ("state of charge and power", "plot_SoC_charging_power.png"),
    ("run cost parameters", "plot_cost_parameter_hourly.png"),
)


def run_stamp_of(filename: str) -> str:
    """The run stamp a generated filename starts with, or '' if it carries none.

    Model section 1.0b puts <YYMMDDHHMMSS-PPPPSSS>_ in front of every file a run writes.
    The stamp holds no underscore and the descriptive part always begins with one, so the
    first underscore is the boundary.
    """
    stem, separator, _rest = str(filename).partition("_")
    return stem if separator else ""


def find_plots_for_stamp(stamp: str) -> Dict[str, Path]:
    """The figures of one particular run, found by the stamp its filenames share.

    find_latest_plots answers "the newest of each kind", which is right for the run just
    made and wrong for a summary picked off disk weeks later - it would pair that run's
    numbers with whatever figure happened to be written last, and nothing on the page
    would say so. Every output of a run carries the same stamp, so a run's own figures are
    exactly the ones that share it, and a run that wrote none has none to show.
    """
    if not RESULT_DATA_DIR.is_dir() or not stamp:
        return {}
    plots: Dict[str, Path] = {}
    for key, filename in MODEL_FIGURES:
        if key == "run cost parameters":
            path = WORKING_DATA_DIR / filename
            if path.is_file():
                plots[key] = path
            continue
        stem = filename[: -len(".png")]
        # a design run writes one set per day (model 5.1's plot_suffix), so this takes the
        # last of them - the same day the Latest section shows, for the same reason
        matches = sorted(RESULT_DATA_DIR.glob(f"{stamp}_{stem}*.png"))
        if matches:
            plots[key] = matches[-1]
    return plots


def find_latest_plots(limit: int = 6, model_only: bool = False) -> Dict[str, Path]:
    """Known model figures first, then the most recently written ones.

    With model_only the result is just the figures the optimization itself writes.
    That is what "the figures of this run" means; everything else in results/ comes from
    the input preparation, and those are the largest figures there - a browser that has
    to fetch and lay them out on every rerun feels the difference.
    """
    if not RESULT_DATA_DIR.is_dir():
        return {}

    plots: Dict[str, Path] = {}
    for key, filename in MODEL_FIGURES:
        path = (WORKING_DATA_DIR / filename
                if key == "run cost parameters"
                else _newest_output(filename))
        if path is not None and path.is_file():
            plots[key] = path
    if model_only:
        return plots

    recent = sorted(
        [p for p in RESULT_DATA_DIR.iterdir() if p.suffix.lower() == ".png"],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    already_shown = set(plots.values())
    for path in recent:
        if len(plots) >= limit:
            break
        # the two figures above are also in `recent`, and keying on the file stem would
        # add them a second time under their raw filename - the Run tab would embed each
        # of them twice and the browser would fetch and lay out the same figure again
        if path in already_shown:
            continue
        plots.setdefault(path.stem.replace("_", " "), path)
        already_shown.add(path)
    return plots


# 4.4 input data previews
# 4.0 why a load that failed is not the same as a file that is not there
#
# Every loader below answers "no data" with an empty frame, and until now it answered the
# same way whether the file was missing, half-written, or valid but encoded wrongly. The
# interface then says "not built yet" and offers to build it - which is the right advice
# for one of those three and misleading for the other two, and in every case the exception
# that would have identified the problem is gone.
#
# So failures are recorded rather than swallowed. The loaders still degrade to empty, so a
# broken file cannot take the page down, and 2 - Inputs shows what went wrong underneath.
_load_problems: Dict[str, str] = {}


def note_load_failure(path, exc) -> None:
    """Record that a file exists but could not be read."""
    _load_problems[str(path)] = f"{type(exc).__name__}: {exc}"


def load_problems() -> Dict[str, str]:
    """{path: reason} for every file a loader found and could not read this session."""
    return dict(_load_problems)


@st.cache_data(show_spinner=False)
def _read_csv_cached(path: str, mtime: float, **kwargs) -> pd.DataFrame:
    """pd.read_csv, memoised on (path, mtime) so a rerun does not re-parse the file.

    Streamlit reruns the whole script on every widget interaction, and these loaders sit on
    the Inputs tab where sliders and expanders are. data/order_trips.csv alone is 5427 rows,
    re-parsed on each of those. mtime is in the key rather than ignored, so editing a file
    still shows the new content - the cache is keyed on the file's identity, not just its
    name.
    """
    return pd.read_csv(path, **kwargs)


def read_result_csv(path: Path, **kwargs) -> pd.DataFrame:
    """Cached read of a results/ CSV, by path and modification time."""
    return _read_csv_cached(str(path), path.stat().st_mtime, encoding=CSV_ENCODING, **kwargs)


def load_trips_preview(rows: int = 10) -> pd.DataFrame:
    if not TRIPS_CSV.exists():
        return pd.DataFrame()
    # nrows, not .head(): the preview is ten rows and the file has thousands
    return read_result_csv(TRIPS_CSV, nrows=rows)


def trips_overview_figure():
    """One figure that answers "what is in the order book" for data/order_trips.csv.

    Three panels rather than one, because the three measures have nothing in common but
    the file they came from: hours, kilometres and hours-per-trip share no axis, and
    putting two of them on one pair of scales would invent a relationship the data does
    not have. Each panel is a single series, so each is named by its own title and none
    needs a legend.

    What each is for:

      trips per day         how much work each day of the range carries, and how far
                            apart the quiet and the busy days are. A truck serves one trip
                            at a time, so the busiest day is what a design run has to buy
                            for (1.2d) and what decides how hard that run will be.
      trip distance         whether a battery truck can do the work at all. The right-hand
                            tail is the part that decides which bev types are candidates.
      trip duration         the shape of the generated set against the duration filter in
                            src/hdv_trip_generation.py. The floor is drawn from the data
                            rather than imported, so the panel keeps telling the truth if
                            the filter is changed and the file rebuilt.

    Returns None when the file is missing or unreadable, so the caller can say so instead
    of drawing an empty frame.
    """
    if not TRIPS_CSV.exists():
        return None
    try:
        trips = read_result_csv(
            TRIPS_CSV, usecols=['day_ID', 'trip_distance_km', 'trip_duration_h'])
    except Exception as exc:
        note_load_failure(TRIPS_CSV, exc)
        return None
    if trips.empty:
        return None

    plt = _pyplot()
    ink = '0.35'          # annotation and axis ink: text tokens, never the series colour
    series = 'tab:blue'   # one hue throughout - every panel is the same trip set
    figure, (top, middle, bottom) = plt.subplots(3, 1, figsize=(8, 7.5))

    # -- per day: how many jobs each day carries, ordered by day so it reads left to right
    per_day = trips.groupby('day_ID').size().sort_index()
    top.fill_between(per_day.index, per_day.values, color=series, alpha=0.18, linewidth=0)
    top.plot(per_day.index, per_day.values, color=series, linewidth=1.3)
    top.set_title(f'Trips per day  ({len(per_day)} days)', fontsize=10, loc='center')
    top.set_xlabel('day in dataset')
    top.set_ylabel('trips')
    top.set_xticks(range(int(per_day.index.min()), int(per_day.index.max()) + 1))
    top.set_xlim(per_day.index.min(), per_day.index.max())
    top.set_ylim(bottom=0)

    # -- distance and duration: what one trip looks like
    middle.hist(trips['trip_distance_km'], bins=40, color=series, rwidth=0.92)
    middle.set_title(f'Trip distance  ({len(trips):,} trips)', fontsize=10, loc='center')
    middle.set_xlabel('trip distance [km]')
    middle.set_ylabel('trips')

    bottom.hist(trips['trip_duration_h'], bins=40, color=series, rwidth=0.92)
    shortest = float(trips['trip_duration_h'].min())
    longest = float(trips['trip_duration_h'].max())
    # the bounds go in the title, not next to the rule: the shortest trips are also the
    # most numerous, so a label anchored at the floor lands underneath the tallest bars
    bottom.set_title(f'Trip duration  (kept {shortest:.2f}-{longest:.2f} h)',
                     fontsize=10, loc='center')
    bottom.set_xlabel('trip duration [h]')
    bottom.set_ylabel('trips')
    bottom.axvline(shortest, color=ink, linewidth=1.0, linestyle='--')

    for axis in (top, middle, bottom):
        axis.grid(True, alpha=0.3)
        axis.set_axisbelow(True)   # the grid is scaffolding, so it goes behind the data
        axis.tick_params(labelsize=8, colors=ink)
        for spine in ('top', 'right'):
            axis.spines[spine].set_visible(False)
    figure.tight_layout()
    figure.savefig(WORKING_DATA_DIR / 'plot_trip_overview.png', dpi=150, bbox_inches='tight')
    return figure


# the sheet the model reads with auto-sizing off, by its exact name - the same constant as
# the model's FLEET_SHEET_EXISTING (section 1.2c). Named here rather than imported so the
# preview keeps working before the model can be executed at all.
FLEET_SHEET_EXISTING = "existing_fleet"


def fleet_sheet_name() -> Optional[str]:
    """FLEET_SHEET_EXISTING if the workbook has it, else None."""
    try:
        return (FLEET_SHEET_EXISTING
                if FLEET_SHEET_EXISTING in pd.ExcelFile(FLEET_DATASET).sheet_names
                else None)
    except Exception as exc:
        note_load_failure(FLEET_DATASET, exc)
        return None


def load_fleet_preview() -> pd.DataFrame:
    """The fleet roster of inputs/fleet_dataset.xlsx, as the model reads it.

    Deliberately a plain read instead of importing the model: the preview must work
    before the derived inputs exist, and the model needs them at import time.

    Read by sheet name, not by position: the workbook also carries a synthetic roster, and
    a sheet added in front of the existing one would otherwise become "the fleet" here
    while the model went on reading the right one.
    """
    try:
        sheet = fleet_sheet_name()
        fleet = pd.read_excel(FLEET_DATASET) if sheet is None else \
            pd.read_excel(FLEET_DATASET, sheet_name=sheet)
        fleet.columns = [str(c).strip().lower() for c in fleet.columns]
        if "vehicle_type" in fleet.columns:
            fleet["vehicle_type"] = fleet["vehicle_type"].astype(str).str.strip().str.lower()
        return fleet
    except Exception as exc:
        note_load_failure(FLEET_DATASET, exc)
        return pd.DataFrame({"note": [f"fleet_dataset.xlsx could not be read: {exc}"]})


def load_pv_cache_summary() -> Dict[str, Any]:
    """Metadata of data/cache_pv_profile.json, or empty if it is missing/legacy."""
    if not PV_PROFILE_CACHE_JSON.exists():
        return {}
    try:
        payload = json.loads(PV_PROFILE_CACHE_JSON.read_text(encoding=CSV_ENCODING))
    except Exception as exc:
        note_load_failure(PV_PROFILE_CACHE_JSON, exc)
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        return {"legacy": True, "entries": len(payload) if isinstance(payload, dict) else 0}
    plant = payload.get("plant") or {}
    return {
        "fingerprint": payload.get("source_fingerprint") or "",
        "n_days": len(payload.get("profiles") or {}),
        "n_trip_dates": len(payload.get("trip_dates") or []),
        "rep_year": payload.get("rep_year"),
        "peak_kW": plant.get("pv_peak_power_kW"),
    }


def load_pv_site_parameters() -> Dict[str, float]:
    """The depot PV plant, as the model reads it from data/depot_pv_parameters.csv.

    Its five numbers are the user inputs of `depot_dataset.xlsx` (sheet 'generation')
    and drive the PVGIS query, so the interface shows them read-only - editing the PV
    plant means editing that sheet and preparing the derived inputs again.
    """
    if not DEPOT_PV_PARAMETER_CSV.exists():
        return {}
    try:
        table = pd.read_csv(DEPOT_PV_PARAMETER_CSV, encoding=CSV_ENCODING)
        if table.empty:
            return {}
        return {str(k): float(v) for k, v in table.iloc[0].items()}
    except Exception as exc:
        note_load_failure(DEPOT_PV_PARAMETER_CSV, exc)
        return {}


def load_charging_stations() -> pd.DataFrame:
    """The depot's charging stations, as the model reads them.

    One row per station from `depot_dataset.xlsx` (sheet 'charging'), used verbatim: the
    interface shows the roster read-only, exactly as it does the fleet and the PV plant.
    Add, remove or re-power a station by editing that sheet.
    """
    if not DEPOT_CHARGING_CSV.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(DEPOT_CHARGING_CSV, encoding=CSV_ENCODING)
    except Exception as exc:
        note_load_failure(DEPOT_CHARGING_CSV, exc)
        return pd.DataFrame({"note": [f"{DEPOT_CHARGING_CSV.name} could not be read: {exc}"]})


def load_primary_sheet(path: Path, sheet: str, header: Any = 0) -> pd.DataFrame:
    """One sheet of a inputs/*.xlsx, read as it sits on disk.

    Deliberately a plain read and not the model's: this is the *original* file, and the
    point of showing it is to show what was typed into it rather than what the model made
    of it. The one thing changed is a two-level header - costs_dataset.xlsx carries its
    price groups on the first row and low/medium/high on the second - which is flattened
    to `group · level`, because a MultiIndex renders as a column of blanks in a table.
    """
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_excel(path, sheet_name=sheet, header=header)
    except Exception as exc:
        note_load_failure('load_primary_sheet', exc)
        return pd.DataFrame({"note": [f"{path.name} (sheet '{sheet}') could not be read: {exc}"]})
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [
            " · ".join(str(part) for part in column
                       if part is not None and not str(part).startswith("Unnamed"))
            for column in frame.columns
        ]
    return frame


def load_cost_parameters() -> pd.DataFrame:
    if not COST_PARAMETER_ENERGY_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(COST_PARAMETER_ENERGY_CSV, encoding=CSV_ENCODING)


def load_v2g_parameters() -> pd.DataFrame:
    if not COST_PARAMETER_DAILY_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(COST_PARAMETER_DAILY_CSV, encoding=CSV_ENCODING)


# 5 LAUNCHER
# 5.1 tell "run by Streamlit" apart from "run as a plain script"
def running_under_streamlit() -> bool:
    """True when this file is being executed by `streamlit run` (or AppTest)."""
    if st is None:
        return False
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


# 5.2 start Streamlit on this very file
DEFAULT_PORT = 8501

# how long the server waits, with no browser attached, before it stops itself [s].
# Not zero: a reload, a navigation or a slow network drops the session for a moment and
# reconnects, and a server that exited on the first quiet second would die every time the
# page was refreshed. Ten seconds is long enough to ride those out and short enough that a
# closed tab does not leave a Gurobi process holding a licence.
EXIT_WHEN_IDLE_GRACE_S = 10.0
EXIT_WHEN_IDLE_ENV = 'HDV_EXIT_WHEN_IDLE'


@_cached_resource
def _start_exit_watchdog() -> bool:
    """Start the watchdog once per server process (the cache is what makes it once)."""
    _watch_for_closed_browser()
    return True


def _watch_for_closed_browser(grace_s: float = EXIT_WHEN_IDLE_GRACE_S) -> None:
    """Stop the server once the last browser tab has gone.

    `streamlit run` is a server: closing the tab leaves it listening, and `python main.py`
    therefore never returns. That is the wrong shape for this tool - it is a desktop
    application that happens to render in a browser, and leaving it running holds a Gurobi
    licence and a few hundred MB for nobody.

    Streamlit exposes no supported hook for "the last client disconnected", so this polls
    the session manager instead. Everything it touches is private API and guarded: if a
    future version moves it, the watchdog gives up quietly and the only consequence is the
    behaviour we had before - a server that outlives its tab.
    """
    import threading
    import time

    def watch() -> None:
        try:
            from streamlit.runtime import exists as runtime_exists, get_instance
        except Exception:
            return

        seen_a_browser = False
        idle_since: Optional[float] = None
        while True:
            time.sleep(1.0)
            try:
                if not runtime_exists():
                    continue
                sessions = get_instance()._session_mgr.list_active_sessions()
            except Exception:
                return              # API moved; leave the server alone rather than guess

            if sessions:
                seen_a_browser = True
                idle_since = None
                continue
            if not seen_a_browser:
                continue            # still starting up - the first tab has yet to connect
            now = time.monotonic()
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= grace_s:
                print('\nbrowser closed - shutting the interface down.', flush=True)
                # os._exit rather than sys.exit: this is a daemon thread inside Streamlit's
                # own event loop, and a raised SystemExit here would be swallowed
                os._exit(0)

    threading.Thread(target=watch, name='hdv-exit-when-idle', daemon=True).start()


def port_is_taken(port: int) -> bool:
    """True when something already listens on `port` of the loopback interface."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def launch(port: int = DEFAULT_PORT, headless: bool = False) -> int:
    """Start the Streamlit app and block until it is closed.

    Returns the Streamlit exit code, or 1 if Streamlit itself is missing or the port
    is already in use.
    """
    if st is None:
        # named explicitly rather than pointed at a requirements file: there is none in
        # this repository, and a missing streamlit is exactly the moment a reader cannot
        # afford to be sent after a file that is not there
        print("ERROR: streamlit is not installed in this environment.\n"
              '       pip install "streamlit>=1.28"')
        return 1

    # Refuse to start rather than advertise a URL somebody else answers. A Streamlit
    # server left running by an earlier session keeps the port, and the browser then
    # shows that old server - including its errors, e.g. a "no such file" for a script
    # that has since been renamed. That looks like a fault in this app and is not one.
    if port_is_taken(port):
        print(
            f"ERROR: port {port} is already in use. http://localhost:{port} would show\n"
            f"       whatever is running there - most often a Streamlit server left over\n"
            f"       from an earlier session, whose errors then look like this app's.\n"
            f"\n"
            f"       Use another port:   python main.py --port {port + 1}\n"
            f"       Or free this one (Windows):\n"
            f"         Get-NetTCPConnection -State Listen -LocalPort {port} | "
            f"Select OwningProcess\n"
            f"         Stop-Process -Id <OwningProcess> -Force",
            flush=True,
        )
        return 1

    command = [sys.executable, '-m', 'streamlit', 'run', str(Path(__file__).resolve()),
               '--server.port', str(port), '--logger.level', 'error']
    if headless:
        command += ['--server.headless', 'true']

    # The server is started as a child process, so "stop when the browser closes" has to
    # be decided inside it - the parent only waits. The flag travels in the environment
    # because Streamlit passes no arguments of its own through to the script.
    environment = dict(os.environ)
    environment.setdefault(EXIT_WHEN_IDLE_ENV, '0' if headless else '1')

    try:
        completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False,
                                   env=environment)
    except KeyboardInterrupt:
        print('\nweb interface stopped.')
        return 0
    return completed.returncode



# 6 WEB INTERFACE
# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def is_missing(value) -> bool:
    """True for a figure a failed solve did not produce.

    The model returns None for every result field of a solve that came back without a
    schedule (its 5.9). It used to return 999999, and older CSVs in results/ still carry
    that, so both are read as missing here - the sentinel by value, because there is no
    other way to know, and only above the threshold so a genuine large cost is not hidden.
    """
    if value is None:
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number != number or number >= 999999      # NaN, or the retired sentinel


def fmt_eur(value):
    """Format a number as euros, passing missing figures and text through as text."""
    if is_missing(value):
        return "n/a"
    try:
        return f"{float(value):,.2f} €"
    except (TypeError, ValueError):
        return str(value)


def fmt_kwh(value):
    """Format a number as kWh, passing missing figures and text through as text."""
    if is_missing(value):
        return "n/a"
    try:
        return f"{float(value):,.0f} kWh"
    except (TypeError, ValueError):
        return str(value)


def fmt_station_powers(powers):
    """'2 x 100 kW, 3 x 300 kW, 5 x 600 kW' - readable for a heterogeneous roster.

    Listing every station individually stops being informative past a handful of them,
    and the interesting property of the list is which power tiers the depot has.
    """
    tiers = {}
    for power in powers:
        tiers[power] = tiers.get(power, 0) + 1
    return ", ".join(f"{count} × {power:g} kW" for power, count in sorted(tiers.items()))


def safe_metric(label, value, help_text=None, delta=None):
    try:
        # delta_color 'off' because the arrow's red/green reading does not apply here:
        # a bigger number is not automatically worse
        st.metric(label, value, delta=delta, delta_color='off', help=help_text)
    except Exception:
        st.write(f"**{label}:** {value}" + (f" ({delta})" if delta else ""))


def metric_row(*cells):
    """One results row of equal boxes, as many columns as there are cells.

    Empty slots are not opened: a row of three fills the row with three boxes
    rather than leaving a hole.
    """
    cells = [cell for cell in cells if cell]
    if not cells:
        return
    cols = st.columns(len(cells))
    for col, cell in zip(cols, cells):
        with col:
            if isinstance(cell, dict):
                safe_metric(**cell)
            else:
                safe_metric(*cell)


def spec_grid_html(items: List[Dict[str, Any]]) -> str:
    """Compact labelled boxes for the run summary, as HTML we lay out ourselves."""
    boxes = []
    for item in items:
        label = html.escape("" if item.get("label") is None else str(item["label"]))
        value = html.escape("" if item.get("value") is None else str(item["value"]))
        boxes.append(
            '<div class="hdv-spec-box">'
            f'<div class="hdv-spec-label">{label}</div>'
            f'<div class="hdv-spec-value">{value}</div>'
            "</div>"
        )
    return '<div class="hdv-spec-grid">' + "".join(boxes) + "</div>"


def fmt_list_metric(values, unit=""):
    """A list of numbers or ids as a compact metric value, or '-' if there is none."""
    if values is None:
        return "-"
    if isinstance(values, (str, int, float)) and not isinstance(values, bool):
        text = f"{values:g}" if isinstance(values, float) else str(values)
        return f"{text} {unit}".strip() if unit else text
    try:
        items = list(values)
    except TypeError:
        return str(values)
    if not items:
        return "-"
    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in items):
        if unit == "kW" and len(items) > 1:
            return fmt_station_powers(items)
        order, counts = [], {}
        for x in items:
            key = float(x)
            if key not in counts:
                order.append(key)
                counts[key] = 0
            counts[key] += 1
        parts = []
        for key in order:
            n = counts[key]
            label = f"{key:g} {unit}".strip() if unit else f"{key:g}"
            parts.append(label if n == 1 else f"{n} × {label}")
        return ", ".join(parts)
    return ", ".join(str(x) for x in items)


def clock_axis(axis, hours: float = 24.0):
    """Label a time-of-day x axis in hh:mm, one tick every two hours.

    The same grid and the same wording the schedule figures use (hdv_figure_style), so a
    curve of the depot's day and a grid of the fleet's day can be read against each other
    without converting a decimal hour in the head.
    """
    from hdv_figure_style import HOUR_TICK_STRIDE, TIME_AXIS_LABEL, hhmm

    ticks = [h for h in range(0, int(hours) + 1, HOUR_TICK_STRIDE)]
    axis.set_xticks(ticks)
    axis.set_xticklabels([hhmm(h) for h in ticks])
    axis.set_xlabel(TIME_AXIS_LABEL)


def show_figure(figure):
    """Render a matplotlib figure and release it so the app does not leak memory."""
    st.pyplot(figure, width='stretch')
    _pyplot().close(figure)


# a run the solver stopped early keeps its incumbent - a real schedule with every
# number in it, only without the proof that nothing better exists
# A solve that produced a usable schedule. Three of them, because the model distinguishes
# a proved optimum from one accepted at optimization_MIPGap from a time-limited incumbent
# (its 5.5); all three carry a real schedule and every figure of it, so all three count as
# solved here. Which one it was is in optimization_status, and how far from the bound in
# mip_gap.
SOLVED_STATUSES = ("optimal", "gap-optimal", "incumbent")

# There is no figures-and-CSV switch any more, and no shared session-state entry behind it.
#
# It used to be offered twice - under 3 - Settings and again under the day range on 4 - Run -
# because a multi-day run wanted it per run. The switch is gone because what it controlled
# was never safe to turn on for more than one solve: plot_suffix (model section 5.1) is only
# set by a design run, so every other solve writes results/disposition_schedule.csv and its
# figures under the *same* names. A sweep of N solves is N writers of one set of files.
#
# So a sweep never writes them (the model routes its own sweep through sweep_solve() for the
# same reason), a sizing run already turned them off itself, and a single disposition run -
# one solve, one writer, no one to collide with - always writes them. None of that is a
# choice the user has to make, which is why there is no longer a control for it.

# Two numbers, not one. INLINE_ROWS is where a table stops being worth showing whole - a
# roster of ten trucks or a sheet of twenty scenario years is read at a glance and folding
# it away only adds a click. PREVIEW_ROWS is how much of a table past that point is shown
# before the fold, which wants to be small: the preview exists to show the columns and the
# shape of the numbers, not to be a second table.
INLINE_ROWS = 30
PREVIEW_ROWS = 10


def _arrow_safe_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a display copy with columns that Arrow can serialize consistently."""
    if frame is None or frame.empty:
        return frame
    safe = frame.copy()
    if "date" in safe.columns:
        safe["date"] = safe["date"].astype("string")
    for column in safe.select_dtypes(include=["object"]).columns:
        types = safe[column].dropna().map(type).unique()
        if len(types) > 1:
            safe[column] = safe[column].astype("string")
    return safe


def _display_dataframe(frame: pd.DataFrame, **kwargs):
    """Display a dataframe after normalizing mixed-type presentation columns."""
    st.dataframe(_arrow_safe_dataframe(frame), **kwargs)


def show_table(frame, *, rows: int = PREVIEW_ROWS, inline: int = INLINE_ROWS,
               unit: str = "rows", empty: str = None):
    """A table whole when it is short, and a preview with the rest in an expander when not.

    The depot's metered year is 17 000 half hours and the order book is 10 000 lines.
    Dropped straight onto the page either one buries everything under it, and a page where
    the reader has to scroll past a year of meter readings to reach the next file is a page
    nobody reads twice.
    """
    if frame is None or frame.empty:
        st.info(empty or "Not built yet - it appears as soon as the model runs.")
        return
    if len(frame) <= inline:
        _display_dataframe(frame, width='stretch', hide_index=True)
        return
    _display_dataframe(frame.head(rows), width='stretch', hide_index=True)
    st.caption(f"First {rows} of {len(frame):,} {unit}.")
    with st.expander(f"Show all {len(frame):,} {unit}", expanded=False):
        _display_dataframe(frame, width='stretch', hide_index=True, height=420)


@_cached
def cached_trip_days():
    return available_trip_days()


@_cached
def cached_trip_dates():
    return trip_dates_by_day()


@_cached
def cached_trip_counts():
    return trip_counts_by_day()


@_cached
def cached_scenario_years():
    return available_scenario_years()


@_cached
def cached_fleet_preview():
    return load_fleet_preview()


@_cached
def cached_pv_site_parameters():
    return load_pv_site_parameters()


@_cached
def cached_charging_stations():
    return load_charging_stations()


# the primary datasets, sheet by sheet. Keyed on the path and sheet name so the four files
# below share one cache entry each rather than one per call site; depot_dataset's
# 'consumption' is 17k rows and order_dataset is 10k, and neither is worth re-reading on
# every widget interaction.
@_cached
def cached_primary_sheet(path_text: str, sheet: str, two_level_header: bool = False):
    return load_primary_sheet(Path(path_text), sheet, header=[0, 1] if two_level_header else 0)


# the two sheets of costs_dataset.xlsx are read through the generator's own reader rather
# than with a `header=` guess of this page's own. They do not have the same shape -
# 'energy_yearly' bands every year low/medium/high under its headings, 'energy_daily'
# states one series per price under a note row naming the day - and a guess that is right
# for one eats a row of the other. One reader, so what the page shows and what a run reads
# cannot come to disagree about where a sheet's data starts.
@_cached
def cached_energy_sheet(sheet: str, source_mtime: float):
    _ensure_src_on_path()
    from hdv_cost_parameter_generation import read_sheet_table
    return read_sheet_table(sheet, ENERGY_DATASET)


@_cached
def cached_energy_sheet_note(sheet: str, source_mtime: float):
    """Whatever the sheet says above its headings - the daily sheet names its date."""
    _ensure_src_on_path()
    from hdv_cost_parameter_generation import sheet_note
    try:
        return sheet_note(sheet, ENERGY_DATASET)
    except Exception:
        return ""


def energy_sheet(sheet: str):
    """One energy sheet as a flat table, re-read when the workbook changes."""
    stamp = ENERGY_DATASET.stat().st_mtime if ENERGY_DATASET.exists() else 0.0
    try:
        return cached_energy_sheet(sheet, stamp)
    except Exception:
        return pd.DataFrame()


def energy_sheet_note(sheet: str):
    stamp = ENERGY_DATASET.stat().st_mtime if ENERGY_DATASET.exists() else 0.0
    return cached_energy_sheet_note(sheet, stamp)


def clear_input_caches():
    cached_trip_days.clear()
    cached_trip_dates.clear()
    cached_trip_counts.clear()
    cached_scenario_years.clear()
    cached_fleet_preview.clear()
    cached_pv_site_parameters.clear()
    cached_charging_stations.clear()
    cached_primary_sheet.clear()


# 6.0 look and feel
#     The palette lives in .streamlit/config.toml; this is the part Streamlit's theme
#     settings cannot reach - how prominent the two working tabs are, and the translucent
#     panels that let the page's own background show through instead of stacking opaque
#     grey boxes on grey.
#
#     Everything here is presentation. Every selector is allowed to miss: if a Streamlit
#     release renames a test id, the rule simply stops applying and the interface falls
#     back to the stock theme rather than breaking.
_THEME_CSS = """
<style>
  :root {
    --hdv-accent: #4CC2FF;
    --hdv-glass: rgba(255, 255, 255, 0.035);
    --hdv-glass-strong: rgba(255, 255, 255, 0.06);
    --hdv-line: rgba(255, 255, 255, 0.09);
  }

  /* a quiet vertical wash instead of a flat fill, so the translucent panels have
     something to be translucent against */
  [data-testid="stAppViewContainer"] {
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(76, 194, 255, 0.07), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(120, 160, 255, 0.05), transparent 55%),
      #0B0F14;
  }
  [data-testid="stHeader"] { background: transparent; }

  /* Streamlit reserves a deep top margin for a toolbar this app does not show, so the
     page opened with a band of nothing above the headline. Tightened here, and the space
     it freed given to the gap between the headline and the step buttons - which is the
     one place a gap does work, separating the name of the tool from its controls. */
  .block-container, [data-testid="stMainBlockContainer"] {
    padding-top: 2.2rem !important;
  }
  [data-testid="stMain"] h1 {
    margin-top: 0 !important;
    padding-top: 0 !important;
    margin-bottom: 3.4rem !important;
  }

  /* ---- the two working tabs carry the interface, so they look like it ---- */
  .stTabs [data-baseweb="tab-list"] {
    gap: 6px;
    padding: 6px;
    border-radius: 14px;
    background: var(--hdv-glass);
    border: 1px solid var(--hdv-line);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }
  .stTabs [data-baseweb="tab"] {
    height: auto;
    padding: 10px 18px;
    border-radius: 10px;
    font-size: 0.95rem;
    letter-spacing: 0.2px;
    color: rgba(230, 237, 243, 0.62);
    background: transparent;
    transition: background 120ms ease, color 120ms ease;
  }
  /* The five steps are the spine of the interface, so they read as buttons rather than
     as a menu: full width, split evenly, and large enough to be the obvious next thing
     to click.
     The horizontal padding is smaller than the type is large on purpose - the buttons
     are flex: 1 1 0, so padding and text compete for the same width, and 'Start Here' is
     long enough that generous padding would wrap it. white-space: nowrap keeps a label on
     one line whatever the window does. */
  .stTabs [data-baseweb="tab"] {
    position: relative;
    font-size: 1.45rem;
    font-weight: 700;
    padding: 20px 18px;
    white-space: nowrap;
    color: rgba(230, 237, 243, 0.86);
    border: 1px solid var(--hdv-line);
    background: var(--hdv-glass);
    flex: 1 1 0;
    justify-content: center;
  }
  /* The run-type choice: the same shape as the step buttons above it, centred under
     them. Streamlit renders a segmented control as [data-testid="stButtonGroup"] with
     one [data-testid="stBaseButton-segmented_control"] per option. */
  [data-testid="stButtonGroup"] {
    display: flex !important;
    /* the group only wraps its buttons, so it has to span the block before centring
       inside it means anything */
    width: 100% !important;
    justify-content: center !important;
    gap: 12px;
    margin: 10px 0 6px 0;
  }
  /* prefix match, because the selected button is stBaseButton-segmented_controlActive */
  [data-testid^="stBaseButton-segmented_control"] {
    font-size: 1.05rem !important;
    font-weight: 650 !important;
    padding: 18px 20px !important;
    flex: 1 1 0;
    justify-content: center;
    border-radius: 12px !important;
    border: 1px solid var(--hdv-line) !important;
    background: var(--hdv-glass) !important;
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
  }
  [data-testid^="stBaseButton-segmented_control"]:hover {
    background: var(--hdv-glass-strong) !important;
  }
  [data-testid="stBaseButton-segmented_controlActive"],
  [data-testid^="stBaseButton-segmented_control"][aria-checked="true"] {
    background: linear-gradient(180deg, rgba(76, 194, 255, 0.26),
                                        rgba(76, 194, 255, 0.10)) !important;
    border-color: rgba(76, 194, 255, 0.60) !important;
    color: #FFFFFF !important;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.10),
                0 8px 24px rgba(76, 194, 255, 0.14);
  }

  /* a tab strip nested inside another one is a detail view, not a step. The descendant
     selector distinguishes them without knowing anything about the DOM depth. */
  .stTabs .stTabs [data-baseweb="tab"] {
    font-size: 0.92rem;
    font-weight: 500;
    padding: 8px 18px;
    flex: 0 0 auto;
    border: none;
    background: transparent;
  }
  .stTabs [data-baseweb="tab"]:hover { background: var(--hdv-glass-strong); }
  /* after the size rules and at least as specific as them, or the prominence styling
     above would win and the selected tab would look the same as the unselected one */
  .stTabs [data-baseweb="tab"][aria-selected="true"] {
    background: linear-gradient(180deg, rgba(76, 194, 255, 0.26), rgba(76, 194, 255, 0.10));
    border: 1px solid rgba(76, 194, 255, 0.60);
    color: #FFFFFF !important;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.10),
                0 8px 24px rgba(76, 194, 255, 0.14);
  }
  .stTabs [data-baseweb="tab"][aria-selected="true"]::before {
    content: "";
    position: absolute;
    left: 14px; right: 14px; bottom: 6px;
    height: 2px;
    border-radius: 2px;
    background: var(--hdv-accent);
  }
  .stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {
    display: none;                      /* the pill is the indicator */
  }

  /* ---- translucent panels: metrics, expanders, tables, alerts ---- */
  [data-testid="stMetric"],
  [data-testid="stExpander"] details,
  [data-testid="stDataFrame"],
  [data-testid="stTable"] {
    background: var(--hdv-glass);
    border: 1px solid var(--hdv-line);
    border-radius: 12px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }
  [data-testid="stMetric"] {
    padding: 14px 16px;
    transition: border-color 140ms ease, background 140ms ease;
    /* one metric per column, stretched to the tallest in the row so a box
       without a delta still lines up with its neighbours */
    box-sizing: border-box;
    height: 100%;
    min-height: 8.75rem;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
  }
  [data-testid="stHorizontalBlock"] {
    align-items: stretch !important;
  }
  [data-testid="stHorizontalBlock"] > div {
    display: flex !important;
    flex-direction: column !important;
  }
  [data-testid="stHorizontalBlock"] > div > div {
    height: 100%;
    display: flex;
    flex-direction: column;
  }
  [data-testid="stMetric"]:hover {
    background: var(--hdv-glass-strong);
    border-color: rgba(76, 194, 255, 0.35);
  }
  [data-testid="stMetricValue"] {
    font-weight: 640;
    letter-spacing: -0.4px;
    white-space: normal !important;
    overflow-wrap: anywhere;
    line-height: 1.25;
  }
  [data-testid="stMetricLabel"] {
    text-transform: uppercase;
    font-size: 0.70rem;
    letter-spacing: 0.7px;
    opacity: 0.62;
  }

  /* compact spec boxes above the Run button: same glass cards as the results
     metrics, smaller type, four to a row, only as many cells as facts */
  .hdv-spec-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.65rem;
    width: 100%;
    margin: 0.35rem 0 1.05rem 0;
  }
  .hdv-spec-box {
    background: var(--hdv-glass);
    border: 1px solid var(--hdv-line);
    border-radius: 12px;
    padding: 0.55rem 0.7rem 0.6rem 0.7rem;
    text-align: center;
    min-height: 4.4rem;
    display: flex;
    flex-direction: column;
    justify-content: center;
    box-sizing: border-box;
  }
  .hdv-spec-label {
    text-transform: uppercase;
    font-size: 0.62rem;
    letter-spacing: 0.7px;
    opacity: 0.62;
    line-height: 1.2;
  }
  .hdv-spec-value {
    font-size: 0.92rem;
    font-weight: 640;
    letter-spacing: -0.2px;
    line-height: 1.3;
    margin-top: 0.22rem;
    overflow-wrap: anywhere;
  }

  /* alerts keep their meaning but stop being solid blocks */
  [data-testid="stAlert"] {
    background: var(--hdv-glass);
    border: 1px solid var(--hdv-line);
    border-left-width: 3px;
    border-radius: 10px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }

  /* ---- sidebar: a pane beside the page rather than a second page ---- */
  [data-testid="stSidebar"] {
    background: rgba(11, 15, 20, 0.72);
    border-right: 1px solid var(--hdv-line);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
  }
  [data-testid="stSidebar"] h2 {
    font-size: 0.74rem;
    text-transform: uppercase;
    letter-spacing: 1.1px;
    opacity: 0.55;
    margin-top: 1.4rem;
    border-top: 1px solid var(--hdv-line);
    padding-top: 1.1rem;
  }

  /* ---- the primary action is the only saturated control on the page ---- */
  .stButton button[kind="primary"] {
    background: linear-gradient(180deg, rgba(76, 194, 255, 0.95), rgba(46, 156, 220, 0.95));
    border: 1px solid rgba(76, 194, 255, 0.55);
    color: #06131C;
    font-weight: 650;
    border-radius: 10px;
    box-shadow: 0 6px 22px rgba(76, 194, 255, 0.22);
  }
  .stButton button[kind="primary"]:hover { filter: brightness(1.08); }
    .hdv-stop-action {
        width: 100%;
        box-sizing: border-box;
        padding: 0.55rem 1rem;
        border-radius: 10px;
        background: linear-gradient(180deg, #ef6a6a, #c83f48);
        border: 1px solid rgba(255, 140, 140, 0.7);
        color: #FFFFFF;
        font-weight: 650;
        text-align: center;
        box-shadow: 0 6px 22px rgba(200, 63, 72, 0.28);
    }
    /* Streamlit puts st-key-* on the widget wrapper itself, not on a child of .stButton */
    .stButton[class*="st-key-stop_"] button,
    [class*="st-key-stop_"] button,
    .stButton div[class*="st-key-stop_"] button {
        background: linear-gradient(180deg, #ef6a6a, #c83f48) !important;
        border-color: rgba(255, 140, 140, 0.7) !important;
        color: #FFFFFF !important;
        font-weight: 650;
        box-shadow: 0 6px 22px rgba(200, 63, 72, 0.28);
    }
    .stButton[class*="st-key-stop_"] button:hover,
    [class*="st-key-stop_"] button:hover,
    .stButton div[class*="st-key-stop_"] button:hover {
        filter: brightness(1.08);
    }
  .stButton button[kind="secondary"] {
    background: var(--hdv-glass);
    border: 1px solid var(--hdv-line);
    border-radius: 10px;
  }

  /* Inside a step, headings and the prose that introduces them are centred, so each tab
     reads as a page with a title rather than as a column of left-aligned fragments.
     Deliberately not everything: a widget label centred over its input is harder to scan,
     and a table or a code block centred is simply wrong. Those are put back below. */
  [data-baseweb="tab-panel"] h1,
  [data-baseweb="tab-panel"] h2,
  [data-baseweb="tab-panel"] h3,
  [data-baseweb="tab-panel"] h4,
  [data-baseweb="tab-panel"] h5,
  [data-baseweb="tab-panel"] h6,
  [data-baseweb="tab-panel"] [data-testid="stHeadingWithActionElements"],
  [data-baseweb="tab-panel"] [data-testid="stCaptionContainer"],
  [data-baseweb="tab-panel"] .stMarkdown p {
    text-align: center;
  }
  /* a heading carrying a help icon lays its parts out in a row, so centring the text
     inside it is not enough - the row itself has to centre */
  [data-baseweb="tab-panel"] [data-testid="stHeadingWithActionElements"] {
    display: flex;
    justify-content: center;
    align-items: baseline;
    gap: 0.4rem;
  }
  /* ... and the exceptions */
  [data-baseweb="tab-panel"] [data-testid="stWidgetLabel"] p,
  [data-baseweb="tab-panel"] [data-testid="stWidgetLabel"] label,
  [data-baseweb="tab-panel"] table,
  [data-baseweb="tab-panel"] th,
  [data-baseweb="tab-panel"] td,
  [data-baseweb="tab-panel"] li,
  [data-baseweb="tab-panel"] pre,
  [data-baseweb="tab-panel"] code,
  [data-baseweb="tab-panel"] [data-testid="stDataFrame"],
  [data-baseweb="tab-panel"] [data-testid="stExpander"] p {
    text-align: left;
  }
  /* an info or warning box is a sentence addressed to the reader, not a label, so it
     centres with the rest of the prose */
  [data-baseweb="tab-panel"] [data-testid="stAlert"] p,
  [data-baseweb="tab-panel"] [data-testid="stAlertContentInfo"],
  [data-baseweb="tab-panel"] [data-testid="stAlertContentWarning"],
  [data-baseweb="tab-panel"] [data-testid="stAlertContentSuccess"],
  [data-baseweb="tab-panel"] [data-testid="stAlertContentError"] {
    text-align: center;
  }
  [data-baseweb="tab-panel"] [data-testid="stAlert"] { justify-content: center; }
  /* the expander titles stay left too - they are labels for what is inside them */
  [data-baseweb="tab-panel"] [data-testid="stExpander"] summary { text-align: left; }

  /* Step 5 is a page of prose, and prose reads worse centred. It opts out wholesale -
     headings, paragraphs, tables and lists alike. */
  .st-key-help_panel,
  .st-key-help_panel * {
    text-align: left !important;
  }
  .st-key-help_panel [data-testid="stHeadingWithActionElements"] {
    justify-content: flex-start !important;
  }

  /* the disposition date is the one control of that tab, so it sits in the
     middle of the page like the heading above it rather than as a full-width
     left-aligned field */
  .st-key-disposition_date {
    display: flex !important;
    flex-direction: column;
    align-items: center;
    width: 100%;
  }
  .st-key-disposition_date [data-testid="stWidgetLabel"],
  .st-key-disposition_date label {
    justify-content: center !important;
    width: 100%;
  }
  .st-key-disposition_date [data-testid="stWidgetLabel"] p,
  .st-key-disposition_date [data-testid="stWidgetLabel"] label {
    text-align: center !important;
  }
  .st-key-disposition_date [data-testid="stDateInput"] {
    width: 100%;
    max-width: 22rem;
    margin-left: auto;
    margin-right: auto;
  }

  hr { border-color: var(--hdv-line); }
  [data-testid="stCaptionContainer"] { opacity: 0.72; }

  /* run wait: our own wheel, not Streamlit's. The native spinner is a 100%-wide
     row whose emotion classes beat [data-testid="stSpinner"] rules, which is why
     it sat on the left. This block is a full-width column we own. */
  [data-testid="stHtml"] .hdv-solver-wait,
  .hdv-solver-wait {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    width: 100%;
    text-align: center;
    margin: 1.8rem 0 1.4rem 0;
    gap: 0.95rem;
  }
  .hdv-solver-wheel {
    width: 3rem;
    height: 3rem;
    box-sizing: border-box;
    border: 3.5px solid rgba(230, 237, 243, 0.18);
    border-top-color: var(--hdv-accent);
    border-radius: 50%;
    animation: hdv-spin 0.8s linear infinite;
  }
  @keyframes hdv-spin {
    to { transform: rotate(360deg); }
  }
  .hdv-solver-text {
    color: rgba(230, 237, 243, 0.88);
    font-size: 1.02rem;
    line-height: 1.4;
  }
    .hdv-derived-wait {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        width: 100%;
        text-align: center;
        margin: 4rem 0 2rem 0;
        gap: 1.25rem;
    }
    .hdv-derived-wheel {
        width: 5rem;
        height: 5rem;
        box-sizing: border-box;
        border: 5px solid rgba(230, 237, 243, 0.18);
        border-top-color: var(--hdv-accent);
        border-radius: 50%;
        animation: hdv-spin 0.8s linear infinite;
    }
    .hdv-derived-text {
        color: rgba(230, 237, 243, 0.88);
        font-size: 1.18rem;
        line-height: 1.4;
    }
  [data-testid="stProgress"] {
    max-width: 28rem;
    margin-left: auto;
    margin-right: auto;
  }
</style>
"""


def _apply_theme() -> None:
    """Inject the presentation layer. Purely cosmetic - safe to remove."""
    st.markdown(_THEME_CSS, unsafe_allow_html=True)


def render_app():
    """Build the Streamlit page. Only called when Streamlit executes this file."""
    st.set_page_config(
        page_title="HDV Disposition Optimizer",
        # the browser tab's icon. An emoji rather than a file, so it needs no asset beside
        # the source and renders from the system font at whatever size the browser wants
        page_icon="🚛",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    # started once per server process, not once per rerun
    if os.environ.get(EXIT_WHEN_IDLE_ENV, '0') == '1':
        _start_exit_watchdog()
    _apply_theme()
    st.title("Heavy-Duty-Vehicle Fleet Disposition Optimizer",
             text_alignment="center", help=(
        "Interface for `src/hdv_disposition_optimization.py` - MILP assignment, "
        "charging and V2G scheduling. All inputs come from the Excel datasets in `inputs/`."
    ))
    # -----------------------------------------------------------------------------
    # Input status - only inputs/ is irreplaceable; everything in results/ is buildable
    # -----------------------------------------------------------------------------

    missing_primary = missing_primary_inputs()

    if missing_primary:
        st.error(
            "Missing primary input dataset(s) in `inputs/`: "
            + ", ".join(p.name for p in missing_primary)
            + ". These Excel files are the only inputs of the model and cannot be regenerated."
        )

    # Derived inputs are built on sight rather than demanded from the user: the sidebar
    # below needs the trip days and the scenario years, so this happens before it renders.
    # Only reachable when inputs/ is complete - without the Excel files there is nothing to
    # derive from, and the error above already says so.
    missing_derived = [] if missing_primary else missing_derived_inputs()
    if missing_derived:
        wait = st.empty()
        wait.html(
            '<div class="hdv-derived-wait">'
            '<div class="hdv-derived-wheel" aria-hidden="true"></div>'
            '<div class="hdv-derived-text">Building the derived Model Inputs at first Startup...</div>'
            '</div>',
            width="stretch",
        )
        try:
            prepare_inputs(force_routing=False, make_plots=False)
            clear_input_caches()
            st.session_state.pop("pv_curve", None)
        except Exception as exc:
            st.error(f"The derived inputs could not be built: {exc}")
            st.exception(exc)
        finally:
            wait.empty()
        missing_derived = missing_derived_inputs()

    trip_days = cached_trip_days()
    trip_dates = cached_trip_dates()
    trip_counts = cached_trip_counts()
    scenario_years = cached_scenario_years()


    # -----------------------------------------------------------------------------
    # Sidebar - run configuration
    # -----------------------------------------------------------------------------

    # Data every tab reads, fetched once before any of them renders. Tabs execute in
    # source order whichever one is on screen, so a lookup left inside a settings group
    # would be unbound for any tab above it.
    fleet_preview = cached_fleet_preview()
    stations_preview = cached_charging_stations()
    pv_site = cached_pv_site_parameters()

    # The three figures the Run tab puts above its button. Derived here rather than inside
    # the table that happens to display them: they belong to two different tabs, and tying
    # them to a layout meant rearranging the Inputs tab could leave the Run tab unbound.
    fleet_size = len(fleet_preview) if "vehicle_id" in fleet_preview.columns else 0
    bev_count = (int((fleet_preview["vehicle_type"] == "bev").sum())
                 if "vehicle_type" in fleet_preview.columns else 0)
    electrification_pct = round(100 * bev_count / fleet_size) if fleet_size else 0
    # from the derived station list, not from the Excel sheet beside it: this is what the
    # model will actually be given, and a stale derived file should show as itself
    station_powers = ([float(x) for x in stations_preview["charger_power_kW"]]
                      if "charger_power_kW" in stations_preview.columns else [])

    # ---------------------------------------------------------------- THE TABS ----
    # Numbered, because the order is not obvious from the names: read the page that says
    # what this is, then the inputs have to be there before the settings mean anything, the
    # settings before a run, and a run before there is a result to read. The numbers are the
    # shortest way to say so, and starting them at Start Here makes reading it the first
    # step rather than an aside.
    #
    # The tabs are unpacked in display order; the `with` blocks below still run in source
    # order, which is what the shared lookups above depend on.
    tab_help, tab_inputs, tab_settings, tab_run, tab_results = st.tabs(
        ["1  ·  Start Here", "2  ·  Inputs", "3  ·  Settings", "4  ·  Run", "5  ·  Results"]
    )

    # ------------------------------------------------------------- 3 · SETTINGS ----
    with tab_settings:
        st.subheader("Settings", help=(
            "Every parameter of a run, grouped. These are shared by both ways of "
            "running - what changes between them is only the day and whether the fleet "
            "is fixed or sized. The Configuration panel at the bottom shows exactly "
            "what will be sent to the model."
        ))
        # One column, not two. The sections are long and several of them are lists
        # of related numbers; side by side, the eye has to choose a side at every
        # scroll position and the shorter column leaves a ragged gap beside the
        # longer one. Stacked, the order on the page is the order they are in - and
        # only the first is open, so the list of headings stays short enough to scan.
        with st.expander("Solver", expanded=True):
            # A field rather than a slider: the useful range spans two orders of
            # magnitude and the cost of the last point of it is enormous - on day 2,
            # 11 % is reached in 164 s and 10 % in 877 s - so the setting wants a
            # number typed deliberately, not one dragged past on the way somewhere.
            mip_gap_pct = st.number_input(
                "MIP gap (%)", min_value=0.01, max_value=50.0, value=10.0, step=0.5,
                format="%.2f",
                help="How far from proven optimal the solver may stop. 10 % means the schedule is "
                     "within 10 % of the best possible cost - not that it is 10 % worse, only that "
                     "the proof stops there. Lower is slower, often steeply so: on the shipped day "
                     "11 % is reached in 164 s and 10 % in 877 s, so the last percentage point costs "
                     "81 % of the runtime.",
            )
            mip_gap = mip_gap_pct / 100.0

            # The other end of the same trade. The gap above says how good the answer
            # has to be; this says how long it may take to get there, and a run that
            # hits it returns its incumbent rather than nothing - a real schedule with
            # every number in it, only without the proof that nothing beats it.
            # Defaults to 0 - no cap - which is the model's own default
            # (optimization_time_limit_s, its 1.3) and the honest one: a capped solve
            # returns an incumbent of unknown quality, and a study that quotes it has
            # quoted a schedule the solver never finished proving. Set minutes here when a
            # sweep must not be held indefinitely by one difficult day, and read the
            # achieved gap of every row rather than only the cost.
            runtime_cap_min = st.number_input(
                "Runtime cap per solve (minutes, 0 = none)",
                min_value=0, max_value=600, value=0, step=5,
                help="Wall clock for one solve. A design run charges this per *day*, so "
                     "a seven-day range can take seven times it - the budget for the "
                     "fleet search itself is on **4 - Run**, under Asset Sizing. Set to 0 "
                     "to let the solver run until it reaches the MIP gap above.",
            )

            # Here rather than under Drivers, where it used to sit: it decides how the day
            # is *solved* and not what the crew may do, and the two settings above are the
            # rest of that decision. It still only applies to a crewed fleet, so the fleet
            # mode has to be known before the selectbox that sets it has rendered - hence
            # the session-state read, the same way the scenario axes are shared (3.5c).
            crewed_now = st.session_state.get(OPERATION_MODE_KEY, "crewed") == "crewed"
            driver_warm = st.checkbox(
                "Warm start the crew solve", value=True, disabled=not crewed_now,
                help="Solve once without the crew rules, then hand that schedule to the "
                     "constrained solve as a starting point. The crew constraints tie every step "
                     "of a vehicle to every earlier one, so finding a first feasible schedule is "
                     "the hard part; repairing a relaxed one is far quicker than building one from "
                     "nothing. On day 2 this is the difference between an optimal answer in ~5 min "
                     "and a 78 % gap after 25.\n\n"
                     "Applies to a crewed fleet only — an autonomous one has no crew rules to "
                     "relax, so there is nothing to warm start from. Set the fleet under "
                     "*Drivers*.",
            )
            # auto_size is set per tab (off for a disposition, on for fleet sizing)


        with st.expander("Operating Hours", expanded=False):
            # the model runs on a 30-min grid, so only half-hour boundaries are offered
            DAY_TIMES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)] + ["24:00"]
            work_start = st.selectbox(
                "Earliest trip start", options=DAY_TIMES, index=DAY_TIMES.index("06:00"),
                help="Trips that can be scheduled after this time of day are. A trip whose own "
                     "window from the order data leaves it no room inside the working hours may "
                     "start earlier.",
            )
            work_end = st.selectbox(
                "Latest trip end", options=DAY_TIMES, index=DAY_TIMES.index("18:00"),
                help="Trips that can be finished by this time of day are. A trip that does not fit "
                     "inside the working hours keeps its own window instead, so it may run beyond "
                     "them. Depot charging and V2G are never restricted by the working hours.",
            )
            work_hours_valid = DAY_TIMES.index(work_start) < DAY_TIMES.index(work_end)
            if not work_hours_valid:
                st.error("The earliest start must be before the latest end.")
            else:
                work_span_h = (DAY_TIMES.index(work_end) - DAY_TIMES.index(work_start)) * 0.5
                st.caption(
                    f"{work_start} – {work_end} ({work_span_h:g} h).",
                    help="Trips are scheduled inside this window wherever their own window from the "
                         "order data allows it. A trip that cannot fit is scheduled by its own window "
                         "instead rather than making the day infeasible - the run summary names any "
                         "trip that had to.",
                )


        with st.expander("Energy Pricing", expanded=False):
            st.caption("What these do", help=(
                "Grid charging and diesel prices are taken from `costs_dataset.xlsx` for the "
                "selected scenario and year - see **2 · Inputs**. They are not editable here. "
                "They price grid energy only; the depot's own PV has its own price under "
                "*Energy Pricing*."
            ))
            peak_power_price = st.number_input(
                "DSO demand charge (€/kW/year)", min_value=0, max_value=500, value=17, step=1,
                help="Annualized grid demand charge on the highest power the depot draws from the "
                     "public grid. Apportioned to one day (/365) and billed on the **increment "
                     "the fleet causes** — the day's peak with the BEVs minus the peak the site "
                     "would have drawn without them. The depot pays the baseline either way, so "
                     "it is not a cost of the disposition. The term is signed: if V2G pulls the "
                     "peak below the no-BEV level, it shows up as a saving.",
            )
            site_peak = st.number_input("Grid connection limit (kW)", min_value=500, max_value=5000, value=2000, step=100)
            grid_overhead_ct = st.number_input(
                "Grid energy buying fees (ct/kWh)", min_value=0.0, max_value=100.0, value=15.0,
                step=0.5, format="%.1f",
                help="Everything charged on top of `electricity_spot_price_€/kWh` when the depot buys a "
                     "kWh: grid fees, levies, taxes, supplier margin. Depot charging from the grid "
                     "is billed at **spot + this**, step by step.",
            )
            pv_sell_overhead_ct = st.number_input(
                "Grid energy selling fees (ct/kWh)", min_value=0.0, max_value=100.0, value=0.0,
                step=0.5, format="%.1f",
                help="Everything deducted from the spot price on **any** kWh the site sells: "
                     "direct-marketing or platform fees, EEG deductions. It sets one sell price at "
                     "**spot − this**, used for all of them — own PV charged into a truck (the "
                     "revenue given up by not selling it), V2G into arbitrage, and V2G into "
                     "flexibility.",
            )
            grid_overhead = float(grid_overhead_ct) / 100.0
            pv_sell_overhead = float(pv_sell_overhead_ct) / 100.0
            pv_price_label = f"spot − {pv_sell_overhead_ct:.1f} ct/kWh"
            plot_run_cost_parameters = st.checkbox(
                "Generate run cost parameter plot", value=False,
                help="Writes the selected run's energy and V2G price curves to "
                     "`plot_cost_parameter_hourly.png` in `data/`. Disabled by default.",
            )


        with st.expander("Truck Tolls", expanded=False):
            diesel_toll = st.number_input("ICE truck toll (€/km)", min_value=0.0, max_value=1.0, value=0.183, step=0.005, format="%.3f")
            bev_toll = st.number_input("BEV truck toll (€/km)", min_value=0.0, max_value=1.0, value=0.0, step=0.005, format="%.3f")


        with st.expander("Conversion Losses", expanded=False):
            st.caption("What these do", help=(
                "Losses between the depot meter and the traction battery, one figure per "
                "direction. Everything the model bills, sells and peak-shaves is metered energy; "
                "the SoC is battery energy. These two numbers are what separates them."
            ))
            charging_efficiency_pct = st.number_input(
                "Charging efficiency (%)", min_value=50.0, max_value=100.0, value=97.0, step=0.5,
                format="%.1f",
                help="Share of a kWh drawn at the meter that arrives as charge. Default 97 % is "
                     "the measured rated-current efficiency of a bidirectional ISO 15118-20 CCS2 / "
                     "CHAdeMO DC charger — Sevdari et al. (2025), *Sustainable Energy Technologies "
                     "and Assessments* 83, 104654. 100 % switches charging losses off.",
            )
            discharging_efficiency_pct = st.number_input(
                "Discharging efficiency (%)", min_value=50.0, max_value=100.0, value=93.0, step=0.5,
                format="%.1f",
                help="Share of a kWh taken out of the battery that reaches the grid. Default 93 % "
                     "from the same measurement. It is deliberately below the charging figure: "
                     "every measurement of bidirectional hardware finds the inverting direction "
                     "the worse of the two — first shown by Apostolaki-Iosifidou, Codani & Kempton "
                     "(2017), *Energy* 127, 730–742.",
            )
            st.caption(
                f"Round trip **{charging_efficiency_pct * discharging_efficiency_pct / 100:.1f} %**",
                help="— the spread a V2G slot has to beat before it earns anything.",
            )


        with st.expander("Battery degradation", expanded=False):
            st.caption("What these do", help=(
                "The warranted equivalent full cycles are not set here - every bev brings its own "
                "`vehicle_battery_warranty` from `fleet_dataset.xlsx`, see **2 · Inputs**."
            ))
            battery_price_share = st.number_input(
                "Battery price share", min_value=0.0, max_value=1.0, value=0.40, step=0.05,
                help="Share of the acquisition price attributed to the battery. Together with each "
                     "vehicle's warranted cycles from fleet_dataset.xlsx this gives its €/EFC.",
            )
            degradation_dist_penalty = st.number_input("Degradation distribution penalty", min_value=0.0, max_value=200.0, value=50.0, step=5.0)
            soc_weight = st.number_input("SoC weight factor", min_value=0.0, max_value=2.0, value=0.5, step=0.1)
            plot_degradation_weight_curve = st.checkbox(
                "Generate degradation weight curve plot", value=False,
                help="Writes `plot_degradation_weight_curve.png` to `results/` after a run. "
                     "Disabled by default because this diagnostic plot is not needed for the "
                     "optimization itself.",
            )
            monte_carlo = st.number_input("Monte-Carlo samples per trip (0=exact)", min_value=0, max_value=20, value=5, step=1)


        with st.expander("External charging & driving breaks", expanded=False):
            st.caption("What these do", help=(
                "Charging at a public station is priced at `public_charging_price_€/kWh` from "
                "`costs_dataset.xlsx` for the selected scenario and year. On top of the energy it "
                "costs the driver time — unless the truck has to stand still anyway because a "
                "statutory driving break is due, in which case the time was already lost to the "
                "break and the penalty is waived."
            ))
            penalty_ext_time = st.number_input(
                "Penalty per minute of external charging (€/min)", min_value=0, max_value=50, value=10, step=1,
                help="What a minute at a public station costs beyond the energy: driver time, the "
                     "detour, the tour not driven. Charged per 30-min step, so €10/min is "
                     "€300/step and will dominate the energy price — which is the point, but "
                     "check it against your own numbers.",
            )
            driving_time_before_break = st.number_input(
                "Driving time before a break is due (min)", min_value=0, max_value=720, value=270, step=15,
                help="Driving time after which a break becomes due. 270 min (4.5 h) is the "
                     "statutory figure under EU Regulation 561/2006. A trip only opens a "
                     "penalty-free window if its own driving time reaches this — the test is per "
                     "trip, not on driving accumulated across several trips.",
            )
            st.caption(
                "The length of that break is **Driver mandatory brake duration** under "
                "**Drivers** — it is the same Lenkzeitpause, so it is set once. It decides "
                "how long the penalty-free window here lasts as well as how much rest a long "
                "trip carries inside it and how far a driver's shift span exceeds their "
                "working time."
            )



        with st.expander("Home Depot & Routes", expanded=False):
            home_depot = st.text_input(
                "Home depot location", value="74635 Kupferzell Deutschland",
                help="The one place the fleet is based, written the way the trip locations are in "
                     "`order_dataset.xlsx` (postcode, town, country) so the two can be compared. "
                     "**Depot charging and V2G are only possible here** — a truck standing at a "
                     "customer yard can only use a public charger.",
            )
            route_chaining = st.checkbox(
                "Chain trips into routes", value=True,
                help="On: the day is planned as routes that leave the depot, run one or more trips "
                     "and come back. A trip may follow another that ends where it starts, or be "
                     "reached by an empty run; trips that start or end away from home get an "
                     "approach or return leg, and all of that empty running is fuelled, tolled and "
                     "charged for.\n\n"
                     "Off: the previous geography-free model — any trip may follow "
                     "any other and a truck may charge whenever it is not driving.",
            )
            location_tolerance_pct = st.number_input(
                "Same-place radius (% of median trip)", min_value=0.0, max_value=50.0, value=10.0,
                step=1.0, format="%.1f", disabled=not route_chaining,
                help="How close two locations have to be to count as one place, as a share of the "
                     "median trip distance. Worth checking rather than trusting: on the shipped "
                     "orders the median trip is ~109 km, so 10 % is a ~11 km radius — wide enough "
                     "that three neighbouring towns merge into the Kupferzell depot. The run prints "
                     "every cluster it merged.",
            )


        with st.expander("Drivers", expanded=False):
            st.caption("What these do", help=(
                "Rostered **after** the optimization, from the finished schedule. A driver is tied "
                "to a vehicle exactly while it is away from the home depot, so the day splits into "
                "one duty block per absence; drivers change vehicles and take their breaks between "
                "blocks, which is at the depot by construction. The cost is reported, not minimised "
                "— the MILP never trades driver hours against energy."
                "\n\n"
                "Three limits, three different quantities, and each one smaller than the "
                "last: the **shift span** is sign-on to sign-off including the depot waiting "
                "between two blocks, the **working duration** is the duty without that "
                "waiting, and the **driving time** is the Lenkzeit without the loading, yard "
                "time and mandatory break inside a block. The span is derived from the other "
                "two rather than set, so they cannot drift apart."
                "\n\n"
                "How much driving makes a break due (4.5 h) is set under *External charging & "
                "driving breaks*; how long the break lasts is set here, because it is also "
                "what separates the working duration from the shift span."
            ))
            # keyed so the Solver expander above can read the fleet mode: it renders first,
            # and the warm start there is a crewed-fleet setting
            operation_mode = st.selectbox(
                "Fleet operation", options=["crewed", "autonomous"], index=0,
                key=OPERATION_MODE_KEY,
                help="**crewed** — every vehicle carries a driver. The driving-time and "
                     "working-time limits apply as constraints, long trips and empty legs "
                     "carry their statutory break, and trips no driver could run legally are "
                     "removed before the solve. The wage is reported, not optimised.\n\n"
                     "**autonomous** — none of the above. No crew limits, no driver cost, no "
                     "mandatory breaks, and nothing removed: a truck that needs 13 h away and 11 h "
                     "of driving simply drives for 13 hours.\n\n"
                     "Run the same day both ways and the difference is the value of autonomy for "
                     "this fleet — which is not only the wage bill. On the shipped day 2 the "
                     "crewed run has to drop five Ruhr loads (1899 km) that an autonomous one "
                     "serves without comment.",
            )
            crewed = operation_mode == "crewed"
            driver_rate = st.number_input(
                "Driver hourly rate (€/h)", min_value=0.0, max_value=200.0, value=20.0, step=1.0,
                disabled=not crewed,
                format="%.2f",
                help="Paid on the **shift span**, sign-on to sign-off, so a break at the depot "
                     "inside a shift is paid. No minimum shift is applied.\n\n"
                     "**Reporting only.** The wage is not in the objective: at this rate over a "
                     "fleet away most of the day it was the largest term there by an order of "
                     "magnitude, and V2G earnings and battery wear — the things the model "
                     "weighs against each other — ended up inside the MIP gap of a figure that "
                     "barely changes between schedules. What the wage was really enforcing is "
                     "now a constraint: no absence may be longer than one lawful shift. "
                     "Changing this number changes the reported driver salary and operating "
                     "cost; it cannot change the schedule.",
            )
            driver_max_working = st.number_input(
                "Driver maximum working duration (h)", min_value=1.0, max_value=24.0,
                value=9.0, step=0.5, disabled=not crewed,
                format="%.1f",
                help="**Working time** — the duty one person may actually perform in a day, "
                     "breaks excluded. Not the same as the shift span below: the span also "
                     "counts the break, and the waiting between two duty blocks.",
            )
            driver_break = st.number_input(
                "Driver mandatory brake duration (h)", min_value=0.0, max_value=4.0,
                value=0.75, step=0.25, disabled=not crewed,
                format="%.2f",
                help="The Lenkzeitpause — 0.75 h (45 min) is the statutory figure under EU "
                     "Regulation 561/2006. One setting drives three things, because they are "
                     "the same break: the rest a long trip or empty leg carries inside it, the "
                     "penalty-free window it opens at a public station, and the gap between "
                     "working time and shift span below. Rounded up onto the 30-min grid for "
                     "the first two, so 0.75 h occupies two steps.",
            )
            driver_max_shift = float(driver_max_working) + float(driver_break)
            st.caption(
                f"**Shift span: {driver_max_shift:.2f} h** — derived, not set. The longest "
                f"continuous absence from the depot is the work a driver may do "
                f"({float(driver_max_working):.2f} h) plus the break they must take while "
                f"doing it ({float(driver_break):.2f} h). Stating it separately made it a "
                f"second opinion about the same day: the solver accepted absences the roster "
                f"then could not give to anybody.",
                help="Enforced on the 30-minute grid by flooring, so a span that does not "
                     "land on a step is rounded down rather than up — the model never allows "
                     "a longer absence than the limit does.",
            )
            driver_max_driving = st.number_input(
                "Max driving per driver (h)", min_value=0.5,
                max_value=float(driver_max_working), value=min(9.0, float(driver_max_working)),
                step=0.5, disabled=not crewed,
                format="%.1f",
                help="**Driving time** — the Lenkzeit, hours actually driving in one driver's "
                     "day, excluding breaks, waiting and charging. 9 h is the statutory "
                     "figure. Capped at the working duration above, because driving is part "
                     "of the working time rather than additional to it — the model rejects a "
                     "Lenkzeit larger than the Arbeitszeit that contains it.",
            )
            driver_feedback = st.checkbox(
                "Let the roster correct the schedule", value=True, disabled=not crewed,
                help="The solver counts crew by proxy — a flat charge on the day's peak "
                     "concurrency — and the roster is fitted afterwards under limits per "
                     "*person* the solver never saw, so it can need more drivers than the peak. "
                     "With this on, the day is solved once more against a head price the roster "
                     "agrees with and the better of the two schedules is kept (each scored "
                     "against the heads its own roster reaches, so it cannot pick a worse one). "
                     "Costs a second solve.",
            )
            # the warm start used to be here. It is under *Solver* now: it is a decision
            # about how the day is solved rather than about what a driver may do, and it
            # belongs beside the gap and the runtime cap that make the same trade.


        with st.expander("Battery Day Boundary", expanded=False):
            day_boundary_soc_pct = st.number_input(
                "Day start & target end SoC (%)", min_value=0, max_value=100, value=50, step=5,
                help="One level for both ends of the day: every bev starts at 00:00 with this "
                     "share of its own capacity and has to be back at it by 24:00. Driving and V2G "
                     "discharge therefore both have to be charged back before the day closes, "
                     "which makes the day repeatable.",
            )
            st.caption(
                f"Every bev starts and ends the day at **{day_boundary_soc_pct} %** of its own capacity from `fleet_dataset.xlsx`.",
                help="Ending above the target is allowed; ending below it is not.",
            )


        with st.expander("V2G Settings", expanded=False):
            v2v = st.checkbox(
                "Vehicle-to-vehicle (V2V)", value=True,
                help="When one truck discharges while another charges in the same half hour, both "
                     "at the depot, the energy crosses the yard instead of the meter. It is never "
                     "bought and never sold, so it pays neither the grid overhead on the way in "
                     "nor the marketing overhead on the way out — the two added together are the "
                     "saving per kWh.\n\n"
                     "The losses are unchanged: the energy still passes one truck's inverter and "
                     "the other's rectifier. V2V removes the fees, not the physics. It also "
                     "competes with own PV for the same charging demand, since both are local "
                     "supply saving the same fees.",
            )
            # The two selling channels as two switches rather than one three-way box.
            # They are independent facts about the market the depot has access to -
            # "we can trade on the spot curve", "we have a flexibility contract" - and
            # a box listing both/arbitrage/flexibility asked the reader to work out
            # that "both" was the two of them together.
            v2g_arbitrage = st.checkbox(
                "V2G arbitrage", value=True,
                help="Sells a discharged kWh at the electricity spot price **minus the selling "
                     "overhead** — the same intraday curve the truck buys on, so the earning is "
                     "the spread between charging cheap and discharging dear. The depot buys at "
                     "spot **+** the grid overhead and sells at spot **−** the selling overhead, "
                     "so that gap, plus the conversion losses, is what a spread has to clear.",
            )
            v2g_flexibility = st.checkbox(
                "V2G flexibility", value=True,
                help="Sells a discharged kWh at the bare `flexibility_spot_price_€/kWh`, with "
                     "**no** selling overhead deducted — that overhead prices the marketing of an "
                     "energy sale, and this channel is paid for the service rather than for the "
                     "energy.",
            )
            # both ticked is the model's 'both': each step sells into whichever of the two
            # pays more. A choice, not a sum - a kWh leaves the battery only once.
            v2g_price_mode = ("both" if v2g_arbitrage and v2g_flexibility else
                              "arbitrage" if v2g_arbitrage else
                              "flexibility" if v2g_flexibility else None)
            if v2g_price_mode == "both":
                st.caption(
                    "Each step sells into whichever channel pays more.",
                    help="A choice, not a sum: a kWh leaves the battery once and can only be "
                         "sold once.",
                )
            elif v2g_price_mode is None:
                # neither channel is a fleet that may discharge and cannot be paid for it,
                # which is V2G off however it is dressed up - said plainly rather than
                # sent to the model as a price mode it has no value for
                st.caption(
                    "No selling channel — this run is made with V2G off.",
                    help="A discharged kWh would have nowhere to be sold. Tick a channel, or "
                         "set V2G to off under *Scenario*, which is the same run.",
                )


        with st.expander("Penalties", expanded=False):
            penalty_vehicle = st.number_input("Penalty per used vehicle (€)", min_value=0, max_value=50, value=10, step=1)
            penalty_chg = st.number_input(
                "Penalty per charging / V2G step (€)", min_value=0, max_value=5, value=1, step=1,
                help="Charged per 30-min slot in which a vehicle occupies a charger or is "
                     "assigned to V2G. It also keeps the solver from occupying a vehicle for a "
                     "flow it never uses, which would show up as activity in the schedule.",
            )


        with st.expander("Notifications", expanded=False):
            # The token is never typed here and never stored by this app: the model
            # reads SLACK_BOT_TOKEN from the environment at the moment it sends (1.5b).
            # All this switch decides is whether it sends at all. A text box for the
            # token would put it into Streamlit's session state and into every
            # screenshot of this tab, which is the opposite of what an environment
            # variable is for.
            slack_token_set = bool(os.environ.get(SLACK_TOKEN_ENV, "").strip())
            slack_notify = st.checkbox(
                "Send a Slack message when a run finishes",
                value=False, disabled=not slack_token_set,
                help="Posted after the optimization returns, with the date, the status and "
                     "the headline costs. A run is never held up by it: if Slack refuses "
                     "the message the run still finishes and the reason is shown on "
                     "**4 · Run**.",
            )
            slack_channel = st.text_input(
                "Channel", value=SLACK_DEFAULT_CHANNEL, disabled=not slack_token_set,
                help="Without the leading #. The bot has to be a member of it — Slack "
                     "refuses a post to a channel it was never invited to.",
            )
            if slack_token_set:
                st.caption(
                    f"`{SLACK_TOKEN_ENV}` found in the environment.",
                    help="Read straight from the environment when the message is sent. "
                         "Neither this app nor the model keeps a copy of it.",
                )
            else:
                st.caption(
                    f"`{SLACK_TOKEN_ENV}` is not set — notifications unavailable.",
                    help=f"Set it as a Windows user environment variable named "
                         f"{SLACK_TOKEN_ENV} (or run  setx {SLACK_TOKEN_ENV} \"xoxb-...\") "
                         "and restart the interface: a process only sees the environment "
                         "it was started with.",
                )


    overrides: Dict[str, Any] = {
        "fleet_input_file": FLEET_DATASET,
        # the station list is not overridden here either: build_runtime_context() reads
        # it from this file, written from depot_dataset.xlsx (sheet 'charging')
        "depot_charging_station_file": DEPOT_CHARGING_CSV,
        # order_data_days / auto_sizing / date_disposition / chosen_day are added per tab
        "work_hours_start": work_start,
        "work_hours_end": work_end,
        # read from the shared entries the run tabs write into (3.5c), not from widgets in
        # this tab - those are gone, and the run tabs render after this dict is built
        "scenario": [st.session_state.get("run_scenario", "best case")],
        "scenario_year": [int(st.session_state.get("run_year") or 2025)],
        # with neither selling channel ticked a discharged kWh has nowhere to go, which is
        # V2G off however it is labelled - so it is sent as off rather than as a price mode
        # the model has no value for
        "v2g_status": [st.session_state.get("run_v2g", "on") if v2g_price_mode else "off"],
        "optimization_MIPGap": float(mip_gap),
        # None rather than 0: the model reads it as "no limit" and writes
        # GRB.INFINITY, where 0 would be a cap of zero seconds
        "optimization_time_limit_s": (float(runtime_cap_min) * 60.0
                                     if runtime_cap_min else None),
        "external_charging_status": st.session_state.get("run_external_charging", "on"),
        # The former Feature Flags group is gone from the interface. Those six parameters
        # are not set here any more either: the model's own defaults (section 1.4) apply,
        # which is what the group was showing anyway. Change them there, not here, so
        # there is one place that decides and not two that can disagree.
        "monte_carlo_samples_per_trip": int(monte_carlo),
        "home_depot_location": home_depot.strip(),
        "route_chaining_status": "on" if route_chaining else "off",
        "location_tolerance_share": float(location_tolerance_pct) / 100.0,
        "site_peak_limit_kW": int(site_peak),
        "depot_load_profile_file": DEPOT_LOAD_PROFILE_CSV,
        # the PV plant itself is not overridden here: build_runtime_context() reads it
        # from this file, which hdv_depot_load_profile_generation.py writes from
        # depot_dataset.xlsx (sheet 'generation'). Only its €/kWh is set in the UI.
        "depot_pv_parameter_file": DEPOT_PV_PARAMETER_CSV,
        # the two sides of the meter, both built from the spot curve
        "grid_energy_overhead_eur_per_kWh": grid_overhead,
        "energy_selling_overhead_eur_per_kWh": pv_sell_overhead,
        "fleet_operation_mode": operation_mode,
        "driver_hourly_rate_eur": float(driver_rate),
        # driver_max_shift_hours is deliberately absent: the model derives it from the two
        # below (its 2.1.1b) and would discard anything set here
        "driver_max_working_hours": float(driver_max_working),
        "driver_mandatory_break_hours": float(driver_break),
        "driver_max_driving_hours": float(driver_max_driving),
        "driver_roster_feedback": "on" if driver_feedback else "off",
        "driver_warm_start": "on" if driver_warm else "off",
        "diesel_truck_toll_eur_per_km": float(diesel_toll),
        "bev_truck_toll_eur_per_km": float(bev_toll),
        "peak_power_price_eur_per_kW": float(peak_power_price),
        # with V2G off above, this decides nothing; 'both' keeps it a value the model knows
        "v2g_price_mode": v2g_price_mode or "both",
        "v2v_status": "on" if v2v else "off",
        # not sent either: the model derives the minutes from
        # driver_mandatory_break_hours above, so one break has one setting
        "driving_time_before_break_minutes": int(driving_time_before_break),
        "battery_price_share": float(battery_price_share),
        "degradation_distribution_penalty": float(degradation_dist_penalty),
        "soc_weight_factor": float(soc_weight),
        "plot_degradation_weight_curve": bool(plot_degradation_weight_curve),
        "plot_run_cost_parameters": bool(plot_run_cost_parameters),
        "penalty_vehicle_use": int(penalty_vehicle),
        "penalty_charging_use": int(penalty_chg),
        "penalty_charging_external_time": int(penalty_ext_time),
        # one figure for both ends of the day: the 00:00 level and the 24:00 target
        "initial_soc_fraction": float(day_boundary_soc_pct) / 100.0,
        # meter <-> battery conversion losses, one per direction
        "charging_efficiency": float(charging_efficiency_pct) / 100.0,
        "discharging_efficiency": float(discharging_efficiency_pct) / 100.0,
        # the notification switch, not the token: that stays in the environment (1.5b)
        "slack_notification_status": "on" if slack_notify else "off",
        "slack_notification_channel": slack_channel.strip().lstrip("#") or SLACK_DEFAULT_CHANNEL,
    }


    # -----------------------------------------------------------------------------
    # Main
    # -----------------------------------------------------------------------------

    def render_run_controls(mode_key, overrides, headline=None, day_dates=None,
                            sweep_specs=None):
        """The run button and what the run will be given. Step 4.

        With day_dates the button solves a range rather than a day: as a design run when
        auto-sizing is on, otherwise one solve per entry of sweep_specs (which defaults to
        one per day). Without day_dates it is the single day in the overrides.
        """
        if headline:
            st.markdown(headline)
        finished_message = st.session_state.pop(
            f"optimization_finished_{mode_key}", None)
        # read rather than popped, unlike the success above: a failure is the one message
        # on this page that has to survive being acted on. Fixing what it names means
        # touching the controls, every one of which reruns the script - and a popped
        # message is gone on that rerun, usually before the traceback has been read.
        # The next run clears it, in the click branch below.
        failed_message = st.session_state.get(f"optimization_failed_{mode_key}")

        fleet_size_value = str(fleet_size) if fleet_size else "—"
        fleet_electrification_value = (f"{electrification_pct}% BEV"
                           if fleet_size else "—")
        charging_value = (f"{len(station_powers)} · {sum(station_powers):,.0f} kW"
                          if station_powers else "—")
        # which sheet this run is priced from (model 1.4b2). A disposition run reads
        # energy_daily and nothing else; a sizing run and a sweep read the year and band
        # of energy_yearly, with the day sheet's shape scaled onto the two curves.
        # Decided by the kind of run and not by a control, so it is shown rather than
        # offered - and it is shown, because on a disposition run it is the reason the
        # year and scenario are not offered above.
        price_basis_value = ("energy_daily only" if mode_key == "disposition"
                             else "energy_yearly level, energy_daily shape")
        tail = [
            dict(label="Fleet size", value=fleet_size_value),
            dict(label="Fleet Electrification", value=fleet_electrification_value),
            dict(label="Charging", value=charging_value),
                dict(label="Grid connection limit", value=f"{site_peak:,} kW"),
            dict(label="Working hours", value=f"{work_start} – {work_end}"),
            dict(label="Auto-sizing", value=overrides.get("auto_sizing", "off")),
                dict(label="Fleet operation", value=overrides.get("fleet_operation_mode", operation_mode)),
                dict(label="Energy prices", value=price_basis_value),
                dict(label="MIP gap", value=f"{mip_gap_pct:g}%"),
        ]
        # Route chaining is not shown on the Disposition tab. It is still on - the setting
        # lives under 3 - Settings and the run reads it - but one day planned against the
        # fleet as it stands is not where that switch is being weighed, and the grid is
        # worth more as a short list of what the run is being given than as a complete one.
        if mode_key != "disposition":
            tail.insert(7, dict(label="Route chaining",
                                value=overrides.get("route_chaining_status", "on")))
        if day_dates:
            day_specs = tuple(day_dates)
            cache_signature = TRIPS_CSV.stat().st_mtime_ns if TRIPS_CSV.exists() else None
            n_trips_by_day = cached_duration_filtered_trip_counts(
                cache_signature, day_specs)
            n_trips = sum(n_trips_by_day)
            head = [
                dict(label="Dates",
                     value=f"{day_dates[0][1]} – {day_dates[-1][1]}"),
                dict(label="Days", value=str(len(day_dates))),
                dict(label="Trips", value=f"{n_trips:,}"),
            ]
            if sweep_specs and len(sweep_specs) > len(day_dates):
                for item in tail:
                    if item["label"] == "Auto-sizing":
                        item.update(label="Solves", value=str(len(sweep_specs)))
                        break
        else:
            disp = overrides.get("date_disposition") or "—"
            raw_days = overrides.get("order_data_days") or []
            day_id = raw_days[0] if raw_days else "—"
            try:
                day_specs = ((int(day_id), str(disp)),)
                cache_signature = TRIPS_CSV.stat().st_mtime_ns if TRIPS_CSV.exists() else None
                n_trips = cached_duration_filtered_trip_counts(
                    cache_signature, day_specs)[0]
            except (TypeError, ValueError):
                n_trips = 0
            try:
                day_n, month_n, year_n = (int(part) for part in str(disp).split("."))
                date_value = date(year_n, month_n, day_n).strftime("%A") + f", {disp}"
            except Exception:
                date_value = str(disp)
            head = [
                dict(label="Date", value=date_value),
                dict(label="Day in Dataset", value=str(day_id)),
                dict(label="Trips", value=str(n_trips)),
            ]
        # 3.5c the scenario this run is made under, directly above the boxes that show
        # what the run will be given - so the setting and its consequence are read in one
        # place. A sweep has no copy: it varies these axes itself, and a fixed value beside
        # the axis that overrides it is a contradiction on the page.
        # A disposition run gets the subset of these that still decides something -
        # scenario_controls() drops the cost scenario and the year there, because
        # `energy_daily` has neither axis (model 1.4b2) and a control that moves no price
        # reads as a comparison the run can make.
        if mode_key in ("disposition", "sizing"):
            scenario_controls(mode_key, scenario_years)
        st.html(spec_grid_html(head + tail), width="stretch")

        run_disabled = bool(missing_derived or missing_primary or not work_hours_valid)
        if mode_key == "sweep":
            n_runs = len(sweep_specs) if sweep_specs else len(day_dates or [])
            label = f"Run Optimization ({n_runs} run{'s' if n_runs != 1 else ''})"
        elif mode_key == "sizing":
            n_days = len(day_dates or [])
            label = f"Run Optimization ({n_days} day{'s' if n_days != 1 else ''})"
        else:
            label = "Run Optimization"
        # keyed by mode: both tabs render this function on every rerun, so two buttons
        # with the same label and parameters would collide on their generated id
        wait_slot = st.empty()
        button_slot = st.empty()
        run_active = bool(st.session_state.get("optimization_running", False))
        clicked = False
        if run_active:
            job = st.session_state.get("optimization_job")
            @st.fragment(run_every=1)
            def poll_optimization_job():
                current_job = st.session_state.get("optimization_job")
                if current_job and current_job["process"].is_alive():
                    if st.button("Stop Optimization", type="secondary",
                                 width="stretch", key=f"stop_{mode_key}_button"):
                        current_job["process"].terminate()
                        current_job["process"].join(timeout=2)
                        st.session_state.pop("optimization_job", None)
                        st.session_state.pop(f"optimization_status_{mode_key}", None)
                        st.session_state["optimization_running"] = False
                        st.warning("Optimization stopped.")
                        st.rerun()
                    st.html(
                        solver_wait_html(latest_run_status(current_job, mode_key)),
                        width="stretch")
                    return
                if current_job:
                    try:
                        payload = current_job["queue"].get_nowait()
                    except queue.Empty:
                        payload = {"ok": False,
                                   "error": "The optimization process ended without a result."}
                    current_job["process"].join(timeout=2)
                    st.session_state.pop("optimization_job", None)
                    st.session_state.pop(f"optimization_status_{mode_key}", None)
                    st.session_state["optimization_running"] = False
                    if not payload.get("ok"):
                        # handed to session state and shown by the full rerun, not drawn
                        # here. This fragment repaints once a second, so anything written
                        # inside it lives exactly that long - which is why the failure used
                        # to flash past and leave a finished-looking page behind it.
                        st.session_state[f"optimization_failed_{mode_key}"] = (
                            payload.get("error") or "unknown error")
                        st.rerun()
                    st.session_state["last_run_mode"] = mode_key
                    _store_optimization_payload(mode_key, payload)
                    st.session_state[f"optimization_finished_{mode_key}"] = (
                        "Optimization finished. Results can be found in the Results tab.")
                    st.rerun()
                else:
                    st.session_state["optimization_running"] = False

            poll_optimization_job()
            return
        else:
            clicked = button_slot.button(label, type="primary", width='stretch',
                                         key=f"run_{mode_key}", disabled=run_disabled)
            if finished_message:
                st.success(finished_message)
            if failed_message:
                st.error(f"Run failed: {failed_message}")
        if clicked:
            st.session_state["optimization_running"] = True
            st.session_state.pop(f"optimization_finished_{mode_key}", None)
            # the previous failure describes the previous run, so it goes when the next
            # one starts - not when the page is next redrawn for some other reason
            st.session_state.pop(f"optimization_failed_{mode_key}", None)
            # 5 - Results renders whatever the last run left in session state, so the new
            # run clears it before it starts rather than after it finishes. A page still
            # showing the previous answer while the next one solves is the one way to read
            # the wrong numbers without anything looking wrong - and a run that fails part
            # way would otherwise leave the old result standing as if it were the new one.
            # Every mode is cleared, not just this one: the Results tab follows
            # last_run_mode, and leaving another mode's answer behind means switching to it
            # shows something the current inputs no longer describe.
            for stale in [key for key in list(st.session_state)
                          if key.startswith(("last_runs_", "last_result_", "last_design_",
                                             "last_vehicle_types_"))]:
                st.session_state.pop(stale, None)
            st.session_state.pop("last_run_mode", None)
            context = mp.get_context("spawn")
            result_queue = context.Queue()
            # a second queue rather than typed messages on the first: the result protocol is
            # "one payload, read once", and folding a stream of status lines into it would
            # make every poll sort one kind of message from the other before it could act
            progress_queue = context.Queue()
            # the previous run's last position, which is not this one's
            st.session_state.pop(f"optimization_status_{mode_key}", None)
            process = context.Process(
                target=_optimization_worker,
                args=(result_queue, progress_queue, mode_key, dict(overrides), day_dates,
                      sweep_specs),
                daemon=True,
            )
            st.session_state["optimization_job"] = {
                "process": process, "queue": result_queue,
                "progress": progress_queue, "mode_key": mode_key,
            }
            process.start()
            st.rerun()


    def render_design_result(design):
        """Render the fleet and infrastructure selected by an Asset Sizing run."""
        st.markdown("#### The Fleet this Range needs")
        st.caption(
            f"Objective {fmt_eur(design.get('objective_EUR'))} over "
            f"{design.get('days', '-')} day(s) — operating cost plus "
            f"{fmt_eur(design.get('ownership_cost_horizon_EUR'))} of ownership.",
            help="Both sides are money over the same number of days, which is the only way "
                 "the trade between buying a truck and running one means anything.",
        )

        # 3.4.5: the depot the schedules actually needed, as opposed to the one they were
        # offered. This is the charging-infrastructure answer of the run.
        needed = design.get("chargers_needed") or []
        st.markdown("#### The charging Infrastructure this Fleet needs")
        if not needed:
            st.info("No day of this range charged at the depot, so it needs no station.")
        else:
            chargers = pd.DataFrame([
                {"charger": index + 1, "rated for [kW]": kW}
                for index, kW in enumerate(needed)
            ])
            metric_row(
                dict(label="Chargers needed",
                     value=design.get("chargers_needed_count", len(needed)),
                     help_text="As many as the busiest day of the range had plugged "
                               "in at once. A truck takes the strongest free "
                               "station, so the stations a day uses are always its "
                               "strongest ones - which is what makes them "
                               "comparable across days by rank."),
                dict(label="Strongest station",
                     value=f"{design.get('chargers_needed_peak_kW', 0):,.0f} kW",
                     help_text="The hardest any one station was pushed on any day "
                               "of the range."),
            )
            _display_dataframe(chargers, width='stretch', hide_index=True,
                               height=min(38 * (len(chargers) + 1) + 3, 300))
            st.download_button(
                "Download the charging infrastructure as CSV", key="design_chargers_csv",
                data=chargers.to_csv(index=False).encode("utf-8"),
                file_name="design_charging_infrastructure.csv", mime="text/csv",
            )
            st.caption(
                f"Read off the schedules, not optimized. The run charged against "
                f"{design.get('design_stations_offered', '-')} × "
                f"{design.get('design_station_kW', 0):,.0f} kW so the depot could not limit "
                f"the fleet; this is what those days turned out to use.",
                help="No station is a decision variable and none carries a cost, so nothing "
                     "pushed the run towards fewer of them. Each row is the highest power "
                     "that station had to deliver on any day of the range.",
            )

    def render_range_sizing(runs):
        """How hard the busiest day of a design range worked the fleet, and the chargers.

        Only ever shown under a design run, so the fleet above is already decided and
        these per-day maxima describe the schedules rather than size anything. It used to
        take a `sized_by_design` flag and carry a second wording for the case where no
        design run had been made - per-day maxima presented as *the* sizing answer. That
        case is gone: a sweep varies scenarios against a fleet that is given, so its
        largest day says how hard that fleet worked and not what to buy, and offering the
        other reading was the thing that made it worth removing rather than re-labelling.
        """
        solved = [r for r in runs
                  if str(r["result"].get("optimization_status")) in SOLVED_STATUSES]
        if not solved:
            st.warning("No day of this range solved to optimality, so it sizes nothing.")
            return

        st.markdown("#### Busiest Day of the Range")

        def worst(key):
            values = [(r["result"].get(key) or 0, r["date"]) for r in solved]
            return max(values) if values else (0, "-")

        ice_max, ice_day = worst("ice_amount")
        bev_max, bev_day = worst("bev_amount")
        v2g_only_max, v2g_only_day = worst("vehicles_v2g_only")

        driven = "ICE driven"
        metric_row(
            dict(label=driven, value=ice_max,
                 delta=None if not ice_max else f"busiest {ice_day}",
                 help_text="The most diesel trucks any solved day of the range put on "
                           "a trip."),
            dict(label=driven.replace("ICE", "BEV"), value=bev_max,
                 delta=None if not bev_max else f"busiest {bev_day}",
                 help_text="The most battery trucks any solved day put on a trip, "
                           "counted on driving alone."),
            dict(label="Vehicles driven",
                 value=ice_max + bev_max,
                 help_text="The two maxima added. They may fall on different days - "
                           "a fleet that covers both has to carry both peaks, so "
                           "adding them is the safe reading, not the tight one."),
        )

        if v2g_only_max:
            st.warning(
                f"**{v2g_only_max} further bev(s)** moved energy without driving on at least "
                f"one day ({v2g_only_day}), earning "
                f"{fmt_eur(worst('v2g_earnings_non_driving_€')[0])} at the peak. They are "
                "**not** in the counts above and cost nothing in the objective, but they "
                "would still have to be bought to earn it. Add them to the fleet, or read "
                "the counts above as the fleet needed to *drive* the days."
            )

        # the charging infrastructure, read off the schedules rather than optimized: which
        # stations the days occupied and how hard each was pushed. The assignment is by
        # rank (strongest station to heaviest draw), so the stations used on a day are
        # always the top k - which makes the union across days the top K of the worst day.
        peaks = {}
        for r in solved:
            ids = r["result"].get("chargers_used_ids") or []
            kws = r["result"].get("chargers_kW") or []
            for charger_id, peak_kW in zip(ids, kws):
                peaks[charger_id] = max(peaks.get(charger_id, 0.0), float(peak_kW))
        if peaks:
            st.markdown("**Charging Infrastructure**")
            chargers = pd.DataFrame(
                [{"charger": charger_id, "peak power drawn [kW]": round(peak_kW, 1)}
                 for charger_id, peak_kW in peaks.items()]
            ).sort_values("peak power drawn [kW]", ascending=False)
            _display_dataframe(chargers, width='stretch', hide_index=True,
                               height=min(38 * (len(chargers) + 1) + 3, 300))
            installed = solved[-1]["result"].get("chargers_installed")
            st.caption(
                f"**{len(peaks)} charger(s)** were occupied across the range"
                + (f", of {installed} installed" if installed else "")
                + f"; the hardest-pushed reached "
                  f"{max(peaks.values()):,.0f} kW.",
                help="Read off the finished schedules, not optimized: no station is a "
                     "decision variable and none carries a cost, so nothing pushed the runs "
                     "towards fewer of them. A truck takes the strongest free station, so "
                     "the stations used on a day are always the strongest ones - this is "
                     "the highest power each had to deliver on any day of the range.",
            )

    # The axes a sweep can move, and how each is read. year is the only ordered one, so it
    # is the only one that may become a line; the rest are labels whose order means nothing
    # and which therefore have to be bars. disposition_date is last because it is what a
    # sweep varies when it varies nothing else - a range of days against fixed settings.
    SWEEP_AXES = (("year", "Year"), ("scenario", "Cost scenario"),
                  ("v2g_status", "V2G"), ("external_charging", "External charging"),
                  ("disposition_date", "Day"))
    # Slots 1-4 of the reference categorical order, unchanged and in order. Verified on the
    # adjacent pairlist, which is the one a line or a bar group is read on: worst
    # normal-vision dE 22.9 (floor 15), worst colour-blind dE 9.2 (target 8). Four is the
    # ceiling here for a reason - a fifth would have to come from somewhere that does not
    # clear those gates, so a sweep with more series folds them into the axis instead.
    SWEEP_SERIES_COLOURS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
    SWEEP_MEASURES = (("Operating cost", "operating_cost_€"),
                      ("Energy cost", "energy_costs_€"),
                      ("V2G earnings", "v2g_earnings_total_€"),
                      ("Degradation cost", "degradation_cost_€"),
                      ("Driver cost", "driver_cost_€"),
                      ("Toll cost", "toll_costs_€"),
                      ("Depot grid peak (kW)", "depot_grid_peak_kW"))

    def render_sweep_axis_chart(frame, key_prefix):
        """Cost against the sweep axes that actually moved.

        The fleet-size scatter beside this one answers a question a sweep does not ask: a
        sweep varies the scenario against a fleet that is *given*, so fleet size is the one
        thing it holds still and the points land on a vertical line. What moved is the
        year, the cost scenario, the V2G switch - and that is what the cost should be read
        against, because comparing those is the whole reason for running one.

        Which axis goes where is decided from the data rather than configured. An axis with
        one value is not an axis and is dropped; of the rest, year takes the x because it
        is the only one with an order, and the others become the series. Everything left
        over - the days, usually - is averaged into each point, so a cell is what a day of
        that combination costs and cells stay comparable when the day count does not.
        """
        if frame is None or frame.empty:
            return
        moved = [(key, label) for key, label in SWEEP_AXES
                 if key in frame.columns and frame[key].astype(str).nunique() > 1]
        if not moved:
            st.caption(
                "This sweep holds every axis fixed - one scenario, one year, one V2G "
                "setting, one day - so there is nothing to plot the cost against."
            )
            return

        options = [(label, column) for label, column in SWEEP_MEASURES
                   if column in frame.columns]
        if not options:
            return
        measure_label = st.selectbox(
            "Measure", [label for label, _ in options], index=0,
            key=f"sweep_measure_{key_prefix}",
            help="Every per-solve figure the sweep recorded is in the table above; these "
                 "are the ones worth reading against an axis.",
        )
        measure = dict(options)[measure_label]

        # year first if it moved, because a number on the x axis is the only one of these
        # that may be joined by a line without inventing an order
        x_key, x_label = next((pair for pair in moved if pair[0] == "year"), moved[0])
        series_keys = [pair for pair in moved if pair[0] != x_key
                       and pair[0] != "disposition_date"]
        # four series is what the palette validates for; beyond that the extra axis is
        # folded into the x and read as a grouping instead of a colour
        while len(series_keys) > 1 and _sweep_series_count(frame, series_keys) > 4:
            series_keys.pop()

        work = frame.copy()
        work["_measure"] = pd.to_numeric(work[measure], errors="coerce")
        work = work[work["_measure"].notna() & (work["_measure"].abs() < 999999)]
        if work.empty:
            st.caption(f"No solve of this sweep recorded a {measure_label.lower()}.")
            return
        work["_x"] = work[x_key].astype(str)
        work["_series"] = (work[[k for k, _ in series_keys]].astype(str).agg(" · ".join,
                                                                            axis=1)
                           if series_keys else "all solves")

        x_values = sorted(work["_x"].unique(), key=_sweep_sort_key)
        series_values = sorted(work["_series"].unique())
        numeric_x = all(_is_number(v) for v in x_values)

        st.markdown(f"#### {measure_label} across the Sweep")
        figure, axis = _pyplot().subplots(figsize=(7, 3.4))
        positions = list(range(len(x_values)))
        group_width = 0.8 / max(len(series_values), 1)

        for index, name in enumerate(series_values):
            colour = SWEEP_SERIES_COLOURS[index % len(SWEEP_SERIES_COLOURS)]
            part = work[work["_series"] == name]
            means = [part.loc[part["_x"] == x, "_measure"].mean() for x in x_values]
            if numeric_x:
                axis.plot(positions, means, color=colour, linewidth=2, marker='o',
                          markersize=7, label=name, zorder=3)
                # the solves behind each mean, so a point that averages a wide spread does
                # not read as a firm number
                for position, x in zip(positions, x_values):
                    each = part.loc[part["_x"] == x, "_measure"]
                    axis.scatter([position] * len(each), each, s=14, color=colour,
                                 alpha=0.35, linewidths=0, zorder=2)
            else:
                offset = (index - (len(series_values) - 1) / 2) * group_width
                axis.bar([p + offset for p in positions], means, width=group_width * 0.9,
                         color=colour, label=name, zorder=3)

        axis.set_xticks(positions)
        axis.set_xticklabels(x_values)
        axis.set_xlabel(x_label)
        axis.set_ylabel(measure_label + (" [kW]" if measure.endswith("kW") else " [€]"))
        axis.grid(True, axis='y', alpha=0.3)
        axis.set_axisbelow(True)
        if series_values and series_values != ["all solves"]:
            axis.legend(title=" · ".join(label for _, label in series_keys),
                        fontsize=8, title_fontsize=8, frameon=False,
                        loc='center left', bbox_to_anchor=(1.01, 0.5))
        show_figure(figure)

        days = frame["disposition_date"].nunique() if "disposition_date" in frame else 1
        if days > 1:
            st.caption(
                f"Each point is the mean over the {days} days of the range, so the cells "
                f"stay comparable. The faint marks behind a line are the individual solves."
                if numeric_x else
                f"Each bar is the mean over the {days} days of the range."
            )

    def _sweep_series_count(frame, series_keys):
        return frame[[k for k, _ in series_keys]].astype(str).agg(" · ".join,
                                                                  axis=1).nunique()

    def _is_number(value):
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            return False

    def _sweep_sort_key(value):
        return (0, float(value), "") if _is_number(value) else (1, 0.0, str(value))

    def render_sweep_charts(frame, key_prefix):
        """The two cross-solve charts: cost against fleet size, and the V2G spread.

        Only for a sweep, and that is a restriction rather than an omission. Both read
        *across* solves, so they need a run that produced several - a single disposition
        gives a scatter of one point and a histogram of one bar, which says less than the
        metric it is drawn from. A sizing range has many solves but one fleet by
        construction, so the fleet-size axis there is a vertical line through days that
        differ for a reason the chart does not show.
        """
        if frame is None or frame.empty:
            return
        # The cost against the axes the sweep moved. There used to be a scatter of cost
        # against fleet size beside this, and it could never say anything: a sweep varies
        # the scenario against a fleet that is *given*, so fleet size is the one quantity
        # it holds still and every point landed on one vertical line. What moved is what
        # the cost is worth reading against, which is what render_sweep_axis_chart draws.
        render_sweep_axis_chart(frame, key_prefix)

        if "v2g_earnings_total_€" in frame.columns:
            values = pd.to_numeric(frame["v2g_earnings_total_€"], errors="coerce").dropna()
            # NaN is how a failed solve arrives now; the < filter is only for the
            # 999999 sentinel older CSVs in results/ still carry (see is_missing)
            values = values[values < 999999]
            if not values.empty:
                st.markdown("#### V2G Earnings Distribution")
                figure, axis = _pyplot().subplots(figsize=(6, 3))
                axis.hist(values, bins=15, color="tab:green", alpha=0.7)
                axis.set_xlabel("V2G earnings [€]")
                axis.set_ylabel("solves [-]")
                axis.grid(True, alpha=0.3)
                show_figure(figure)

    def render_run_figures(plots, note_inputs=True):
        """The figures of one run, three to a row."""
        if not plots:
            return
        st.markdown("#### Generated Figures")
        columns = st.columns(min(3, len(plots)))
        for index, (name, path) in enumerate(plots.items()):
            with columns[index % len(columns)]:
                try:
                    st.image(str(path), caption=name, width='stretch')
                except Exception:
                    st.write(f"{name}: `{path}`")
        if note_inputs:
            st.caption(
                "The input-preparation diagnostics (`hdv_load_profiles_*.png`, `hdv_cost_parameter_*.png`) are written to `results/` as well.",
                help="they are not embedded here because every rerun would make the browser lay them out again.",
            )

    def render_latest_result(mode_key, label):
        """The last run of one mode, if there is one. Step 5."""
        if f"last_result_{mode_key}" not in st.session_state:
            st.info(f"No {label.lower()} run yet — start one on **4 · Run**.")
            return
        result = st.session_state[f"last_result_{mode_key}"]
        st.markdown(f"### {label} Run")

        # A range run leaves one result per day. The table is the answer a design run was
        # asked for - the fleet has to survive the worst day, not the average one - and the
        # picker below it opens any single day at the usual depth.
        runs = st.session_state.get(f"last_runs_{mode_key}") or []
        last_day_is_shown = True

        # the fleet a design run decided on. Shown whatever the day count: a range of one
        # day is still a design answer, and hiding it behind "more than one day" made the
        # headline result of the run disappear exactly when it was easiest to read.
        design = st.session_state.get(f"last_design_{mode_key}")
        if design:
            render_design_result(design)

        if len(runs) > 1:
            # a sweep varies more than the day, so the axes that actually moved become
            # columns. One that was held fixed is not shown: a column of one repeated
            # value is noise in a table whose point is the comparison.
            axis_columns = [key for key in ("year", "scenario", "v2g", "external_charging")
                            if len({r.get(key) for r in runs}) > 1]
            st.markdown("#### Every Solve of the Sweep" if axis_columns
                        else "#### Every Day of the Range")
            rows = []
            for r in runs:
                row = {"day": r["day"], "date": r["date"]}
                for key in axis_columns:
                    row[key.replace("_", " ")] = r.get(key)
                row.update({
                    "status": r["result"].get("optimization_status", "-"),
                    "fleet": r["result"].get("fleet_size"),
                    "drivers": r["result"].get("drivers_required"),
                    "energy €": r["result"].get("energy_costs_€"),
                    "V2G €": r["result"].get("v2g_earnings_total_€"),
                    "grid peak kW": r["result"].get("depot_grid_peak_kW"),
                    "km": r["result"].get("total_fleet_distance_km"),
                })
                rows.append(row)
            summary = pd.DataFrame(rows)
            # A restored or externally loaded run can contain date values with different
            # scalar types. Arrow rejects such mixed object columns even though pandas can
            # display them, so keep this presentation column explicitly textual.
            summary["date"] = summary["date"].astype("string")
            _display_dataframe(summary, width='stretch', hide_index=True,
                               height=min(38 * (len(summary) + 1) + 3, 420))
            st.download_button(
                "Download the sweep as CSV" if axis_columns
                else "Download the range summary as CSV", key=f"range_csv_{mode_key}",
                data=summary.to_csv(index=False).encode("utf-8"),
                file_name="scenario_sweep.csv" if axis_columns else "day_range_summary.csv",
                mime="text/csv",
            )
            # Only for a design run. "Sizing result for the range" is the question Asset
            # Sizing asks, and answering it under a sweep invites the per-day maxima to be
            # read as a fleet recommendation - which they are not: a sweep varies scenarios
            # against a fleet that is given, so the largest day tells you how hard that
            # fleet worked, not what to buy. With a design above, the same block is the
            # busiest day of a fleet already decided, which is worth showing.
            if design:
                render_range_sizing(runs)

            # the cross-solve view, above the picker that opens one of them: these compare
            # the solves to each other, so they belong with the table of all of them and
            # not inside the detail of whichever one is currently open
            if mode_key == "sweep":
                render_sweep_charts(pd.DataFrame([r["result"] for r in runs]),
                                    f"latest_{mode_key}")

            def solve_label(i):
                # the date alone stops identifying a solve the moment a scenario axis
                # moves, because every scenario runs the same days
                parts = [runs[i]['date'], f"day {runs[i]['day']}"]
                parts += [str(runs[i].get(key)) for key in axis_columns]
                return (" · ".join(parts)
                        + f" ({runs[i]['result'].get('optimization_status', '-')})")

            options = list(range(len(runs)))
            chosen = st.selectbox(
                "Solve to look at in detail" if axis_columns else "Day to look at in detail",
                options=options, index=len(runs) - 1, format_func=solve_label,
                key=f"range_day_{mode_key}",
                help="The metrics below are this solve's. The schedule table and the figures "
                     "are files in results/, which every solve of the run overwrote in turn, "
                     "so those are always the last one.",
            )
            result = runs[chosen]["result"]
            last_day_is_shown = chosen == len(runs) - 1
            st.markdown(f"#### {runs[chosen]['date']} in Detail")

        render_result_metrics(result)

        # results/ holds one schedule and one set of figures, and every day of a range
        # overwrote them in turn. Only the last day's survived, so anything else shown here
        # would belong to a different day than the metrics above it.
        if not last_day_is_shown:
            st.info(
                f"The schedule and figures below are **{runs[-1]['date']}**, the last day "
                f"solved — not {runs[chosen]['date']}. Every day of the range writes to the "
                "same files in `results/`, so only the last one is still there. Run that day "
                "on its own to see its schedule."
            )

        render_result_schedule(load_schedule_csv(), f"schedule_csv_{mode_key}")
        render_run_figures(find_latest_plots(model_only=True))

    def render_result_schedule(schedule_df, download_key):
        """The per-step schedule of one solve, first 30 rows and the whole file to take."""
        if schedule_df is None or schedule_df.empty:
            return
        st.markdown("#### Detailed Schedule")
        _display_dataframe(schedule_df.head(30), width='stretch', hide_index=True)
        st.download_button(
            "Download full schedule CSV", key=download_key,
            data=schedule_df.to_csv(index=False).encode("utf-8"),
            file_name="disposition_schedule.csv",
            mime="text/csv",
        )

    def render_result_metrics(result):
        """Every metric of one solve, in reading order.

        One function for the two places a solve is read: the run this session made, and a
        summary picked off disk afterwards. Those used to be a full page of metrics and a
        bare table of the same numbers, so the same run looked like two different things
        depending on whether the app had been restarted since it finished. A summary CSV
        carries the keys the in-memory result does - write_run_summary writes that same
        dict - so the only thing the two paths do not share is where the dict came from,
        and a missing key reads as "-" here either way.
        """
        status = str(result.get("optimization_status", "-"))
        if status not in SOLVED_STATUSES:
            st.warning(
                f"Solver status: **{status}**. No feasible schedule was found for this "
                "configuration - try a day with shorter trips, more or stronger stations "
                "in the `charging` sheet of `depot_dataset.xlsx`, or a larger / less "
                "electrified roster in `fleet_dataset.xlsx`."
            )

        let_out = result.get("trips_outside_work_hours") or []
        if let_out:
            st.info(
                f"**{len(let_out)}** trip(s) could not be fitted inside "
                f"{work_start}–{work_end} and were scheduled by their own window from "
                f"the order data instead: trip {', '.join(str(t) for t in let_out)}. "
                "They may therefore start earlier or finish later than the working hours."
            )

        peak = result.get("depot_grid_peak_kW")
        increment = result.get("bev_peak_increment_kW")
        without = result.get("depot_grid_peak_without_bev_kW")
        gap = result.get("mip_gap")
        metric_row(
            dict(label="Solver status", value=status,
                 delta=None if is_missing(gap) else f"{gap:.2%} gap",
                 help_text="**optimal** — proved. **gap-optimal** — accepted at the "
                           "configured MIP gap and still that far from the best bound. "
                           "**incumbent** — the clock ran out: a real schedule, no proof."),
            dict(label="Operating cost", value=fmt_eur(result.get("operating_cost_€")),
                 delta=(None if is_missing(result.get("steering_penalties_€"))
                        else f"+{result.get('steering_penalties_€'):,.0f} € steering"),
                 help_text="What the day costs: fuel, electricity, tolls, battery wear, "
                           "the demand charge and the driver salary, less V2G and V2V "
                           "earnings. The solver's steering penalties — per truck used, "
                           "per charger slot, per plug-in, per driver head, the id "
                           "tie-break — are **not** in it. They are modelling devices, "
                           "not money, and they are shown as the delta.\n\n"
                           "The objective is not the two added together either: the "
                           "driver salary is in this figure and not in the objective, so "
                           "`objective_€` equals this less the salary plus the steering "
                           "penalties. That is what `objective_residual_€` checks."),
            dict(label="Fleet size", value=result.get("fleet_size", "-")),
            dict(label="Energy cost", value=fmt_eur(result.get("energy_costs_€"))),
        )
        metric_row(
            dict(label="Electrification",
                 value=f"{result.get('fleet_electrification_%', '-')} %"),
            dict(label="V2G earnings", value=fmt_eur(result.get("v2g_earnings_total_€"))),
            dict(label="Degradation cost", value=fmt_eur(result.get("degradation_cost_€"))),
            dict(label="Toll cost", value=fmt_eur(result.get("toll_costs_€"))),
            dict(label="Depot grid peak",
                 value="n/a" if is_missing(peak) else f"{peak:,.1f} kW",
                 delta=None if increment is None else f"{increment:+,.1f} kW from the fleet",
                 help_text="Highest power drawn from the public grid on the optimized day: "
                           "depot baseline demand + depot chargers - bidirectional discharge "
                           f"- PV. Without any BEV the site would peak at "
                           f"{'n/a' if without is None else f'{without:,.1f} kW'}."),
        )
        ice_n = result.get("ice_amount")
        bev_n = result.get("bev_amount")
        ice_bev = "-" if ice_n is None and bev_n is None else f"{ice_n} / {bev_n}"
        metric_row(
            dict(label="Demand charge", value=fmt_eur(result.get("demand_charge_€")),
                 help_text="DSO demand charge for the day, on the increment the fleet causes: "
                           "peak power price / 365 x (grid peak with BEV - grid peak without). "
                           "Negative when V2G shaves the peak below the no-BEV level."),
            dict(label="Total distance",
                 value="n/a" if is_missing(result.get('total_fleet_distance_km'))
                 else f"{int(result.get('total_fleet_distance_km') or 0):,} km"),
            dict(label="V2G equivalent full cycles",
                 value=("-" if is_missing(result.get("v2g_equivalent_full_cycles"))
                        else result.get("v2g_equivalent_full_cycles")),
                 help_text="Battery cycles caused by V2G discharge - the only "
                           "cycling the model charges degradation for."),
            dict(label="ICE / BEV count", value=ice_bev,
                 help_text="Diesel trucks / battery trucks that drove this day."),
        )

        if result.get("pv_profile_source") == "synthetic":
            st.warning(
                "The depot PV curve of this run is the **simplified bell curve**, not "
                "PVGIS - the service was unreachable and `pv_allow_synthetic_profile` "
                "is on. Every PV figure below, and the energy costs that follow from "
                "them, are synthetic."
            )

        pv_saving = result.get("pv_energy_saving_€")
        pv_rate = result.get("pv_opportunity_price_€/kWh")
        metric_row(
            dict(label="PV generated", value=fmt_kwh(result.get("pv_generation_kWh")),
                 help_text="Yield of the depot plant on the disposition date, from "
                           "PVGIS for the plant in depot_dataset.xlsx (sheet "
                           "'generation')."),
            dict(label="PV left for trucks", value=fmt_kwh(result.get("pv_surplus_kWh")),
                 help_text="The part of that yield the depot's own baseline load "
                           "does not consume - the ceiling for charging from own "
                           "generation."),
            dict(label="PV into trucks", value=fmt_kwh(result.get("pv_charging_kWh")),
                 help_text="Depot charging covered by own PV, billed at spot "
                           "minus the selling overhead instead of spot plus the "
                           "grid overhead."),
            dict(label="PV energy cost", value=fmt_eur(result.get("pv_charging_cost_€")),
                 delta=None if pv_saving is None else f"{pv_saving:,.2f} € saved vs grid",
                 help_text="PV kWh into the trucks x their opportunity price, "
                           "spot minus the selling overhead"
                           + (f" ({pv_rate:.3f} €/kWh average)" if pv_rate is not None else "")
                           + ". The remaining depot charging "
                           f"({result.get('grid_charging_kWh')} kWh) is bought at "
                           "spot plus the grid overhead. The saving beside it is "
                           "the two overheads added, which is why it does not "
                           "depend on the market price."),
        )

        eta_ch = result.get("charging_efficiency")
        eta_dis = result.get("discharging_efficiency")
        metric_row(
            dict(label="Charging loss", value=fmt_kwh(result.get("charging_loss_kWh")),
                 help_text="Energy drawn at the meter that never reached a battery: "
                           f"(1 - {eta_ch:.0%}) x all charging."
                           if eta_ch is not None else None),
            dict(label="Discharging loss", value=fmt_kwh(result.get("discharging_loss_kWh")),
                 help_text="Charge the packs gave up beyond what the grid received: "
                           f"V2G kWh x (1/{eta_dis:.0%} - 1)."
                           if eta_dis is not None else None),
            dict(label="Round trip",
                 value="-" if eta_ch is None or eta_dis is None else f"{eta_ch * eta_dis:.1%}",
                 help_text="Charging x discharging efficiency - the spread a V2G "
                           "slot has to beat before it earns anything."),
            dict(label="Value of the losses",
                 value=fmt_eur(result.get("conversion_loss_cost_€")),
                 help_text="The lost kWh priced at the day's own average charging "
                           "price. Reported, not added: the objective already pays "
                           "for them through the metered energy."),
        )

        if result.get("home_depot_location"):
            loaded = result.get("total_fleet_distance_km") or 0
            dead = result.get("deadhead_km") or 0
            share = result.get("fleet_at_depot_share")
            metric_row(
                dict(label="Routes from the depot", value=result.get("routes_driven", "-"),
                     help_text="Each one leaves the home depot, runs its trips and "
                               "comes back. Fewer routes for the same trips means "
                               "more chaining and less empty running."),
                dict(label="Chained trips", value=result.get("chains_used", "-"),
                     delta=f"{result.get('direct_chains_used', 0)} direct",
                     help_text="Trips run straight after another instead of from "
                               "the depot. A direct chain starts where the previous "
                               "trip ended and costs no empty running at all."),
                dict(label="Empty running", value=f"{dead:,.0f} km",
                     delta=None if not loaded else f"{dead / loaded:.0%} of loaded km",
                     help_text="Approach, return and between-chain kilometres. "
                               "Carries no freight but burns fuel or charge, pays "
                               "toll and ages the battery."),
                dict(label="Fleet at the depot",
                     value="-" if share is None else f"{share:.0%}",
                     help_text="Share of all vehicle-steps spent standing at home. "
                               "This is the entire window in which depot charging "
                               "and V2G were possible at all."),
            )

        if result.get("drivers_required") is not None:
            over = result.get("driver_blocks_over_shift") or 0
            blocks = result.get("driver_duty_blocks")
            breach = result.get("crew_breach_h") or 0
            metric_row(
                dict(label="Drivers", value=result.get("drivers_required", "-"),
                     delta=None if not blocks else f"{blocks} duty blocks",
                     help_text="One driver per continuous absence of a vehicle "
                               "from the depot, packed into as few shifts as the "
                               "rules allow. They may change vehicles between "
                               "blocks, which happens at the depot."),
                dict(label="Driver salary", value=fmt_eur(result.get("driver_cost_€")),
                     delta="reported, not optimised",
                     help_text=f"The day's total wage bill: the shift span, sign-on to "
                               f"sign-off, at "
                               f"{result.get('driver_hourly_rate_€', 0):.2f} €/h over every "
                               "driver the roster needs. It is inside **Operating cost** "
                               "and it is **not** in the objective — at ~20 €/h over a "
                               "fleet away most of the day it was the largest term there "
                               "by an order of magnitude, so V2G earnings and battery wear "
                               "sat inside the MIP gap of a figure that barely changes "
                               "between schedules. What the wage was really enforcing — "
                               "bring the truck home — is the hard shift limit instead. "
                               "The objective's only driver term is a head charge of "
                               f"{result.get('driver_cost_in_objective_€', 0):,.2f} €, "
                               "counted under steering penalties."),
                dict(label="Paid hours", value=f"{result.get('driver_paid_h', 0):,.1f} h",
                     delta=f"{result.get('driver_break_h', 0):,.1f} h on break",
                     help_text="Sign-on to sign-off across all drivers. Breaks "
                               "at the depot inside a shift are paid."),
                dict(label="Crew limits broken",
                     value=f"{breach:,.1f} h" if breach else "none",
                     help_text="Hours by which the optimization had to exceed the "
                               "**driving** limit or the statutory break. Those two "
                               "are priced, not hard, because some trips cannot be "
                               "crewed legally from one depot at all — an approach leg "
                               "of 6 h leaves no legal day. The **shift** limit is not "
                               "among them: it is a hard constraint, so no absence "
                               "longer than one lawful shift is in the feasible set at "
                               "all. Zero means the whole schedule is inside the rules."),
                dict(label="Over-shift blocks", value=over,
                     delta=None if not over
                     else f"longest {result.get('driver_longest_block_h', 0):g} h",
                     help_text="Absences longer than one legal shift on their own. "
                               "**0 by construction**: the shift limit is enforced as a "
                               "hard constraint in the MILP, so a schedule containing "
                               "one is infeasible rather than expensive. Anything else "
                               "here means the run was made with "
                               "`driver_shift_limit = 'priced'`."),
            )
            beyond = result.get("drivers_beyond_model")
            if beyond:
                st.info(
                    f"**The roster needs {beyond} driver more than the objective counted "
                    f"heads for.** The solver charges heads on peak concurrency — how many "
                    f"vehicles are away at once — which is a lower bound on the head "
                    f"count. What actually decides it is the packing: a "
                    f"{result.get('driver_longest_block_h', 0):g} h absence cannot be "
                    f"split between two people, however much slack the "
                    f"{result.get('driver_max_working_h', 0):g} h working-time limit "
                    f"leaves in total. This is reported rather than hidden because it "
                    f"is the seam in a sequential vehicle-then-crew decomposition."
                    if beyond > 1 else
                    f"**The roster needs 1 driver more than the objective counted heads "
                    f"for.** Peak concurrency is a lower bound on the head count; the "
                    f"packing under the working-time and driving limits decides the rest."
                )
            if over:
                st.warning(
                    f"**{over} of {blocks} duty blocks exceed the "
                    f"{result.get('driver_max_shift_h', 0):g} h shift limit** "
                    f"(longest {result.get('driver_longest_block_h', 0):g} h). "
                    + ("This run was made with `driver_shift_limit = 'priced'`, so the "
                       "solver was allowed to buy its way past the limit. Set it back to "
                       "`'hard'` to forbid the absence outright."
                       if str(result.get("driver_shift_limit", "hard")) != "hard" else
                       "The shift limit is enforced as a hard constraint, so this should "
                       "not be reachable — read it as a fault in how the schedule was "
                       "read back, not as a finding about the day.")
                )

        if result.get("v2v_kWh") is not None:
            metric_row(
                dict(label="V2V energy", value=fmt_kwh(result.get("v2v_kWh")),
                     delta=None if not result.get("v2v_steps")
                     else f"in {result.get('v2v_steps')} step(s)",
                     help_text="Passed straight from a discharging truck to a "
                               "charging one at the depot, never crossing the "
                               "meter. Competes with own PV for the same "
                               "charging demand."),
                dict(label="V2V saved", value=fmt_eur(result.get("v2v_saved_total_€")),
                     help_text="Grid overhead not paid on the way in plus "
                               "marketing overhead not paid on the way out. The "
                               "spot price is in both and cancels, so the saving "
                               "does not depend on the market."),
                dict(label="of which grid fees",
                     value=fmt_eur(result.get("v2v_saved_grid_fees_€")),
                     help_text="Levies, taxes and network charges avoided. The "
                               "demand charge is NOT included: the site import "
                               "already nets discharge against charging, so the "
                               "peak was never inflated by these kWh."),
            )

        metric_row(
            dict(label="Chargers installed",
                 value=fmt_list_metric(result.get("chargers_installed_kW"), "kW"),
                 help_text="The depot's station list from depot_dataset.xlsx, sheet "
                           "charging, as powers."),
            dict(label="Chargers used (id)",
                 value=fmt_list_metric(result.get("chargers_used_ids")),
                 help_text="Station ids occupied on this day."),
            dict(label="Chargers used (kW)",
                 value=fmt_list_metric(result.get("chargers_kW"), "kW"),
                 help_text="Peak power each occupied station actually delivered."),
            dict(label="BEV batteries used",
                 value=fmt_list_metric(result.get("bev_kWh"), "kWh"),
                 help_text="Traction-battery size of each battery truck that drove."),
        )




    with tab_settings:
        st.divider()
        st.subheader("Effective Configuration", help=(
            "Exactly the values the sidebar sends to the model for the next run. "
            "Everything is controlled from the sidebar; this tab is the read-back."
        ))

        rows = []
        for key, value in overrides.items():
            rows.append({"parameter": key, "value": str(value)})
        _display_dataframe(pd.DataFrame(rows), width='stretch', hide_index=True, height=560)

        st.download_button(
            "Download configuration as CSV",
            data=pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
            file_name="run_configuration.csv",
            mime="text/csv",
        )



    # -------------------------------------------------------------- INPUT DATA ----

    # --------------------------------------------------------------- 2 · INPUTS ----
    with tab_inputs:
        # ======================================================= ORIGINAL INPUT FILES ==
        # The four Excel files, sheet by sheet and in the order the model needs them: what
        # drives, where it charges, what the site itself draws and makes, what energy costs,
        # and what has to be delivered. Shown from inputs/ directly rather than through the
        # derived copies - this section answers "what did we put in", and the section below
        # answers "what did the model make of it". They used to be the same section, which
        # meant a stale derived file could be read as the sheet it came from.
        st.subheader("Original Input Files", help=(
            "The four Excel files in `inputs/` are the only irreplaceable inputs and the only "
            "ones anybody edits. Nothing is ever written back into `inputs/`; everything in "
            "`results/` below is derived from these four and can be deleted at will."
        ))

        fleet_sheet = fleet_sheet_name()
        st.markdown("**Fleet** (`inputs/fleet_dataset.xlsx`, sheet `existing_fleet`, the trucks standing at the depot, dispatched as listed)")
        show_table(fleet_preview, empty="`fleet_dataset.xlsx` could not be read.")
        if fleet_size:
            st.caption(
                f"{fleet_size} vehicles — {bev_count} bev, {fleet_size - bev_count} ice.",
                help="The roster a run with auto-sizing **off** dispatches, read from the "
                     "existing-fleet sheet by name. With auto-sizing on the workbook is read "
                     "as it comes and the roster becomes a pool to draw from.\n\n"
                     "Used verbatim: consumption is kWh/100km for a bev and l/100km for an "
                     "ice, and every bev brings its own price and warranted cycles, which is "
                     "what prices its degradation.",
            )

        st.markdown("**Charging stations** (`inputs/depot_dataset.xlsx`, sheet `charging`, the chargers at the depot, one row per station with its kW)")
        show_table(cached_primary_sheet(str(DEPOT_DATASET), "charging"), unit="stations",
                   empty="`depot_dataset.xlsx` has no readable `charging` sheet.")
        if station_powers:
            st.caption(
                f"{len(station_powers)} stations, {sum(station_powers):,.0f} kW installed "
                f"({fmt_station_powers(station_powers)}).",
                help="The model offers every station as a charging opportunity in every 30-min "
                     "step, capped at that station's kW and at the truck's own charging power; one "
                     "truck per station and step. Nothing about the infrastructure is synthesized - "
                     "add or remove a row to change it.",
            )

        st.markdown("**Depot load** (`inputs/depot_dataset.xlsx`, sheet `consumption`, what the site draws without the trucks, on the 30-minute grid)")
        show_table(cached_primary_sheet(str(DEPOT_DATASET), "consumption"), unit="readings",
                   empty="`depot_dataset.xlsx` has no readable `consumption` sheet.")
        st.caption(
            "Read for the day and month of the run, with the year ignored.",
            help="That is what makes a run a winter or a summer day on the demand side as well "
                 "as on the generation side. A date the sheet has no reading for stops the run "
                 "rather than being filled in.",
        )

        st.markdown("**Depot PV plant** (`inputs/depot_dataset.xlsx`, sheet `generation`, size, tilt and orientation of the roof plant, as PVGIS is asked)")
        show_table(cached_primary_sheet(str(DEPOT_DATASET), "generation"),
                   empty="`depot_dataset.xlsx` has no readable `generation` sheet.")
        if pv_site:
            st.caption(
                f"Own PV into the trucks is priced at {pv_price_label}.",
                help="The revenue given up by not selling it, not its generation cost. The depot's "
                     "own load in sheet `consumption` is served from the plant first; only the "
                     "surplus can charge a truck.",
            )

        # this preview is about the plant, not about a run, so it asks for its own date
        preview_date = st.date_input(
            "PV curve date", value=DEFAULT_DISPOSITION_DATE, format="DD.MM.YYYY",
            key="pv_preview_date",
            help="Which day of the year to query PVGIS for. A run never uses this one: both "
                 "kinds take the date from the trip data, so the sunshine belongs to the "
                 "trips being planned.",
        )
        date_disposition = preview_date.strftime("%d.%m.%Y")
        st.markdown(f"**PV curve on {date_disposition}**")
        if st.button("Generate PV curve from PVGIS", disabled=bool(missing_derived)):
            with st.spinner("Querying PVGIS (cached in results/ after the first call) ..."):
                try:
                    # the date has to travel with the overrides. Without it the namespace
                    # keeps the model's own date_disposition and the curve drawn is that
                    # day's, under a heading naming the one picked here
                    st.session_state["pv_curve"] = (
                        date_disposition,
                        depot_day_profiles({**overrides,
                                            "date_disposition": date_disposition}))
                except Exception as exc:
                    st.error(f"PV curve failed: {exc}")
        curve_date, curve = st.session_state.get("pv_curve", (None, None))
        if curve is None:
            st.caption(
                "Not requested yet.",
                help="The curve is the model's own `pv_generation_kW`, taken from PVGIS for the "
                     "plant above and the day of year of the disposition date.",
            )
        else:
            if curve_date != date_disposition:
                st.warning(f"Shown for {curve_date} - generate again for {date_disposition}.")
            hours = [t * curve["step_hours"] for t in range(len(curve["pv_kW"]))]
            figure, axis = _pyplot().subplots(figsize=(8, 3))
            axis.fill_between(hours, curve["pv_surplus_kW"], color="tab:orange", alpha=0.25,
                              label="left for the trucks")
            axis.plot(hours, curve["pv_kW"], color="tab:orange", label="PV generation")
            axis.plot(hours, curve["baseline_kW"], color="tab:blue", label="depot baseline load")
            axis.set_ylabel("power [kW]")
            axis.set_xlim(0, 24)
            clock_axis(axis)
            axis.grid(True, alpha=0.3)
            axis.legend(fontsize=8)
            show_figure(figure)

        st.markdown("**Energy prices by year** (`inputs/costs_dataset.xlsx`, sheet `energy_yearly`, what electricity, diesel and grid fees cost per scenario year)")
        show_table(energy_sheet("energy_yearly"),
                   unit="years",
                   empty="`costs_dataset.xlsx` has no readable `energy_yearly` sheet.")
        st.caption(
            "*best case* takes the low bev prices against the high ice ones, *worst case* the "
            "reverse. **This sheet is the only place the year and the scenario band live**, "
            "and only Asset Sizing and Scenario Sweep runs are priced from it.",
            help="Set under **3 · Settings → Scenario**. The spot price here is the public "
                 "market price, carrying no grid fees, levies or taxes - those are the "
                 "overheads under *Energy Pricing*. Those two kinds of run move the year "
                 "and the scenario, so both set the level: the daily curve is rescaled so "
                 "its mean over the day matches the cell below, which keeps the intraday "
                 "spread the V2G arbitrage channel trades on, and the diesel and public "
                 "charging prices are taken from here as single numbers. A Disposition "
                 "run reads none of this - it is priced off `energy_daily` alone.",
        )

        _daily_note = energy_sheet_note("energy_daily")
        st.markdown("**Intraday prices** (`inputs/costs_dataset.xlsx`, sheet `energy_daily`, what electricity and flexibility cost across one operating day, hour by hour" + (f" — {_daily_note}" if _daily_note else "") + ")")
        show_table(energy_sheet("energy_daily"),
                   unit="hours",
                   empty="`costs_dataset.xlsx` has no readable `energy_daily` sheet.")
        st.caption(
            "One operating day, one series per price. **Disposition runs are priced from "
            "this sheet alone** — all four prices, exactly as written here. Every run "
            "takes its intraday shape from it."
            + (f" The sheet states **{_daily_note}**; nothing is read out of that line, "
               "so a run made for another date still uses these prices." if _daily_note else ""),
            help="A Disposition run plans a single day, so it takes these prices as they "
                 "stand: the electricity and flexibility curves hour by hour, and the "
                 "public charger and the diesel as the single numbers they are. The "
                 "spread between the hourly prices is what the charging windows and the "
                 "V2G arbitrage channel are decided on. Because this sheet has neither a "
                 "year nor a low/medium/high band, **the scenario and the year do not "
                 "affect a Disposition run's prices at all** — they stay on the result as "
                 "labels. Asset Sizing and Scenario Sweep runs keep the same shape but "
                 "rescale it onto the scenario year and band of `energy_yearly`, and take "
                 "the diesel and public charging prices from there too; those are the "
                 "runs to make when years or scenarios are to be compared.",
        )

        st.markdown("**Orders** (`inputs/order_dataset.xlsx`, sheet `orders`, the transport jobs to plan, with pickup, delivery and time window)")
        show_table(cached_primary_sheet(str(ORDER_DATASET), "orders"), unit="orders",
                   empty="`order_dataset.xlsx` has no readable `orders` sheet.")
        st.caption(
            "Geocoded and routed into `data/order_trips.csv` — the one derived file that is "
            "never rebuilt behind your back. The PVGIS year cache is the other way round: "
            "it rebuilds itself when this file or `depot_dataset.xlsx` changes.",
            help="Routing every order costs tens of minutes of rate-limited geocoding, so a "
                 "stale order_trips.csv is announced and left for you to refresh with the button "
                 "below rather than rebuilt on sight like the others. `cache_pv_profile.json` "
                 "is cheap (one PVGIS year) and is rebuilt on sight so it cannot silently "
                 "belong to a previous plant or order book.",
        )

        st.divider()
        # ======================================================== DERIVED INPUT FILES ==
        st.subheader("Derived Input Files", help=(
            "Everything the model reads out of `results/`, all of it built from the four "
            "files above. Deleting any of it is safe - a missing derived file is rebuilt on "
            "sight, which is why the only thing that has to be asked for is refreshing one "
            "that is already there."
        ))

        st.markdown("#### Status & Rebuild")

        if missing_derived:
            st.warning(
                "These derived inputs could not be built:\n\n"
                + "\n".join(f"- `{p.relative_to(PROJECT_ROOT)}` (from {s})"
                            for p, s in missing_derived)
            )
        else:
            st.success(
                "All derived inputs in `results/` are present. Anything missing from "
                "`results/` is rebuilt from `inputs/*.xlsx` by itself - deleting a derived "
                "file is safe, and the button below is only needed to refresh one that is "
                "still there."
            )

        # centred like everything else in a step: the control belongs to the page, not to
        # a column of it
        _, prep_middle, _ = st.columns([1, 2, 1])
        with prep_middle:
            force_routing = st.checkbox(
                "Force re-routing", value=False,
                help="Routing is skipped when data/order_trips.csv already matches the current "
                     "order_dataset.xlsx. Tick this to route again regardless.",
            )
            if st.button("Rebuild derived inputs", width='stretch', type="secondary"):
                with st.spinner("Reading inputs/*.xlsx and rebuilding results/ ..."):
                    try:
                        info = prepare_inputs(force_routing=force_routing, make_plots=True)
                        clear_input_caches()
                        st.session_state.pop("pv_curve", None)  # belongs to the old PV plant
                        plant = info.get("pv_plant") or {}
                        st.success(
                            f"{info['trips']} trips over {info['days']} days · "
                            f"cost parameters {info['years'][0]}-{info['years'][1]} · "
                            f"V2G curves over {info['v2g_hours']} h · "
                            f"depot load from {', '.join(info['depot_sheets'])} · "
                            f"PV plant {plant.get('pv_peak_power_kW', 0):,.0f} kWp · "
                            f"{info.get('charging_stations', 0)} charging stations · "
                            f"PVGIS cache {info.get('pv_days', 0)} calendar days "
                            f"({info.get('pv_trip_dates', 0)} trip dates)"
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Preparation failed: {exc}")
                        st.exception(exc)
            st.caption(
                "Rebuilds everything in `results/` from `inputs/`.",
                 help="Regenerates `cost_parameter_*.csv`, `depot_load_profile.csv`, "
                     "`depot_pv_parameters.csv`, `depot_charging_stations.csv`, "
                     "`order_trips.csv` and `cache_pv_profile.json`. Building the trips geocodes and "
                     "routes every order, which takes tens of minutes without "
                     "`cache_routing.json`; afterwards it is reused. The PVGIS cache is "
                     "rebuilt automatically whenever `order_dataset.xlsx` or "
                     "`depot_dataset.xlsx` no longer match its fingerprint.",
            )

        problems = load_problems()
        if problems:
            st.error(
                "Some files in `results/` are present but could not be read. They show as "
                "missing below, which is not what they are - rebuilding will not help "
                "until the reason is dealt with:\n\n"
                + "\n".join(f"- `{path}` — {reason}" for path, reason in problems.items())
            )

        st.markdown("#### The derived Files")

        st.markdown("**Trips** (`data/order_trips.csv`, the orders routed into drivable trips with distance and duration)")
        trips_preview = load_trips_preview(10)
        if trips_preview.empty:
            st.info("Not built yet - it appears as soon as the model runs.")
        else:
            _display_dataframe(trips_preview, width='stretch', hide_index=True)
            st.caption(f"Days available: {trip_days[0]}-{trip_days[-1]} ({len(trip_days)} days)"
                       if trip_days else "")
            # ten rows say what the columns are; they cannot say how much work the file
            # holds or how it is spread, and both decide what a run of it will cost
            overview = trips_overview_figure()
            if overview is not None:
                show_figure(overview)
                st.caption(
                    "The whole file at a glance. **Trips per day** is how much work each "
                    "day carries - a truck serves one trip at a time, so the busiest day of "
                    "a range is the one an Asset Sizing run has to buy for. **Trip distance** "
                    "is what decides whether a battery truck can do the work, and its "
                    "right-hand tail is the part that rules bev types in or out. **Trip "
                    "duration** shows the set against the duration filter in "
                    "`src/hdv_trip_generation.py`; the dashed rule is the shortest trip "
                    "the current filter kept, read from the file rather than from the "
                    "constant, so it stays honest if the filter changes and the file is "
                    "rebuilt."
                )

        st.markdown("**Energy cost parameters** (`data/cost_parameter_yearly.csv`, the yearly prices as the per-kWh and per-litre figures billed — the level an Asset Sizing or Scenario Sweep run is priced at)")
        cost_df = load_cost_parameters()
        if cost_df.empty:
            st.info("Not generated yet.")
        else:
            _display_dataframe(cost_df.head(8), width='stretch', hide_index=True)

        st.markdown("**Hourly price parameters** (`data/cost_parameter_hourly.csv`, the whole of sheet `energy_daily` in model units — the two V2G channel curves hour by hour, plus the public charging and diesel prices. The shape every run is priced on, and a Disposition run's level too)")
        v2g_df = load_v2g_parameters()
        if v2g_df.empty:
            st.info("Not generated yet.")
        else:
            _display_dataframe(v2g_df, width='stretch', hide_index=True)

        st.markdown("**Depot baseline load** (`data/depot_load_profile.csv`, the depot's own consumption on the model's 30-minute grid)")
        if DEPOT_LOAD_PROFILE_CSV.exists():
            depot_df = pd.read_csv(DEPOT_LOAD_PROFILE_CSV, encoding=CSV_ENCODING,
                                   parse_dates=["date"])
            diurnal = depot_df.groupby(depot_df["date"].dt.hour * 2
                                       + depot_df["date"].dt.minute // 30)["power_kW"].mean()
            figure, axis = _pyplot().subplots(figsize=(8, 3))
            axis.plot(diurnal.index * 0.5, diurnal.values, color="tab:blue")
            axis.set_ylabel("mean power [kW]")
            axis.set_xlim(0, 24)
            clock_axis(axis)
            axis.grid(True, alpha=0.3)
            show_figure(figure)
        else:
            st.info("Not generated yet.")

        st.markdown("**PVGIS year cache** (`data/cache_pv_profile.json`, a year of solar generation for this plant, sliced per run day)")
        pv_cache = load_pv_cache_summary()
        if not pv_cache:
            st.info("Not generated yet.")
        elif pv_cache.get("legacy"):
            st.warning(
                "The cache is in the old per-date format and will be rebuilt on the next "
                "run so it matches the current `order_dataset.xlsx` and `depot_dataset.xlsx`."
            )
        else:
            peak = pv_cache.get("peak_kW")
            peak_text = f"{peak:,.0f} kWp" if isinstance(peak, (int, float)) else "the depot plant"
            fp = (pv_cache.get("fingerprint") or "")[:12]
            st.caption(
                f"{pv_cache.get('n_days', 0)} calendar days of {pv_cache.get('rep_year', 'PVGIS')} "
                f"for {peak_text}, covering {pv_cache.get('n_trip_dates', 0)} trip dates"
                + (f" (fingerprint {fp})" if fp else "")
                + ".",
                help="One PVGIS seriescalc call for the representative year, sliced to every "
                     "day in `order_trips.csv`. Rebuilt automatically when `order_dataset.xlsx` or "
                     "`depot_dataset.xlsx` changes, the same way `order_trips.csv` stores a "
                     "fingerprint of the order book — except this file is not left stale.",
            )


    # ----------------------------------------------------------------- RESULTS ----


    # ------------------------------------------------------------------ 4 · RUN ----
    with tab_run:
        # The choice sits at the top of the step, in the same shape as the step buttons
        # above it, because it is the same kind of decision: which of these am I doing.
        # A segmented control rather than a radio - large targets side by side read as
        # buttons, where a radio reads as a form field.
        # centred by putting it in the middle of three columns rather than by CSS: the
        # control only wraps its own buttons, so flex-centring it inside a block that is
        # itself only as wide as its content does nothing
        _, mode_middle, _ = st.columns([1, 2, 1])
        with mode_middle:
            run_mode = st.segmented_control(
                "Type of run",
                options=["Disposition", "Asset Sizing", "Scenario Sweep"],
                default="Disposition", label_visibility="collapsed", key="run_mode",
                width="stretch",
                help="Three questions, in the order they are usually asked. "
                     "**Disposition** plans one day against the fleet as it stands. "
                     "**Asset Sizing** asks what fleet and what chargers the work would "
                     "need, sizing one fleet against a whole range of days. "
                     "**Scenario Sweep** runs a range against the fleet as it stands, one "
                     "solve per day, so the levers of step 3 can be compared.",
            ) or "Disposition"
        st.divider()
        if run_mode == "Disposition":
            st.subheader("Disposition of one operating Day", help=(
                "Plan the day as it stands: the fleet of `fleet_dataset.xlsx` is taken as given "
                "and every trip of the chosen day is assigned to it. No auto-sizing — this "
                "answers *how should today be run*, not *what fleet should we own*."
            ))

            if trip_days:
                # The date is the setting, and it picks everything: which trips are planned,
                # which day of the depot's metered year the baseline load comes from, and which
                # day of the year PVGIS is asked for. One control, so the three can never
                # disagree - planning a January order set against July sunshine was possible
                # while they were separate.
                dated_days = {stamp.date(): day for day, stamp in trip_dates.items()}
                if dated_days:
                    available = sorted(dated_days)
                    picked_date = st.date_input(
                        "Disposition date", value=available[0],
                        min_value=available[0], max_value=available[-1],
                        format="DD.MM.YYYY", key="disposition_date",
                        help="Sets the trips, the depot load and the PV yield together. Only "
                             "dates that data/order_trips.csv actually carries can be planned - "
                             f"{len(available)} of them, from {available[0]:%d.%m.%Y} to "
                             f"{available[-1]:%d.%m.%Y}.",
                    )
                    picked_day = dated_days.get(picked_date)
                    if picked_day is None:
                        near = [d for d in available if abs((d - picked_date).days) <= 3]
                        st.warning(
                            f"**{picked_date:%d.%m.%Y} has no routed trips.** The order data "
                            "covers working days only, so weekends and holidays inside the "
                            "range are absent."
                            + (f" Nearest with trips: "
                               f"{', '.join(f'{d:%d.%m.%Y}' for d in near)}." if near else "")
                        )
                    else:
                        day_date = picked_date.strftime("%d.%m.%Y")
                        disposition_overrides = dict(overrides)
                        disposition_overrides.update({
                            "order_data_days": [int(picked_day), int(picked_day)],
                            "chosen_day": 1,
                            "date_disposition": day_date,
                            "auto_sizing": "off",
                        })
                        render_run_controls("disposition", disposition_overrides)
                else:
                    st.warning(
                        "data/order_trips.csv carries no dates, so a day cannot be chosen by one. "
                        "Rebuild the derived inputs on **2 · Inputs**."
                    )
            else:
                st.info("No routed trips yet — build the derived inputs on **2 · Inputs**.")

        # ------------------------------------------------- ASSET SIZING / SWEEP ----
        # Both plan a *range* of days rather than one, and both need the same range
        # controls, so the shape of the range is built once here and the two tabs differ
        # only in what they do with it: Asset Sizing decides a fleet, Scenario Sweep runs
        # the fleet as it stands against every day so the levers can be compared.
        else:
            sizing_mode = run_mode == "Asset Sizing"
            mode_key = "sizing" if sizing_mode else "sweep"

            if sizing_mode:
                st.subheader("Optimal Fleet and Charging Infrastructure Sizing", help=(
                    "What fleet and what chargers would this work need? Auto-sizing is what "
                    "this tab *is*: the vehicle types of the `synthetic_fleet` sheet become "
                    "a pool to buy from, and **one fleet is chosen for the whole range** - "
                    "one ownership decision shared by every day, operating variables per "
                    "day. "
                    "That is the only way a truck bought for Tuesday can be free on "
                    "Wednesday; sizing day by day and taking the largest answer makes every "
                    "day pay for its trucks alone, so every day under-buys.\n\n"
                    "The fleet is held down by what a truck costs to own, priced from "
                    "`vehicle_price`, so the model will not buy one just to sell its battery "
                    "into the V2G spread. The charging infrastructure is not optimized: the "
                    "run charges against a depot built to fit so the chargers cannot limit "
                    "the fleet, and reports what the finished schedules turned out to need."
                ))
            else:
                st.subheader("Parameter Sweep for multiple Scenarios", help=(
                    "The same days, the fleet as it stands, and every lever in the sidebar "
                    "turned against them - prices, efficiencies, crew rules, V2G and V2V, "
                    "the depot's own PV. Each day is a separate optimization, so this is "
                    "where assumptions are compared rather than where assets are chosen.\n\n"
                    "For *what should we buy*, use **Asset Sizing**."
                ))

            if trip_days:
                day_min, day_max = min(trip_days), max(trip_days)
                if day_min == day_max:
                    sizing_days = [day_min, day_max]
                    st.caption(f"data/order_trips.csv holds a single day (day {day_min}).")
                else:
                    span = st.slider(
                        f"Day range in data/order_trips.csv ({day_min}–{day_max})",
                        min_value=day_min, max_value=day_max, value=(day_min, day_min),
                        key=f"day_range_{mode_key}",
                        help=("Every day in this span is planned against one shared "
                              "fleet." if sizing_mode else
                              "Every day in this span is solved, one optimization each - so "
                              "widening it costs one solve per day added."),
                    )
                    sizing_days = [int(span[0]), int(span[1])]

                # The range is the whole input, and each day brings its own date. The model
                # renumbers the range from 1 (order_data_days, section 2.1), so the n-th day
                # here is the model's day n. Each date sets that day's depot load and PV
                # yield, so a day is read against its own season rather than against one
                # date chosen for the whole range - which used to make a January order set
                # possible under July sunshine.
                sizing_fallback = DEFAULT_DISPOSITION_DATE.strftime("%d.%m.%Y")
                sizing_day_dates = []
                undated = []
                for raw_day in range(sizing_days[0], sizing_days[1] + 1):
                    stamp = trip_dates.get(raw_day)
                    if stamp is None:
                        undated.append(raw_day)
                    sizing_day_dates.append(
                        (raw_day, sizing_fallback if stamp is None else stamp.strftime("%d.%m.%Y")))

                if undated:
                    st.warning(
                        f"{len(undated)} day(s) of data/order_trips.csv carry no date, so their "
                        f"depot load and PV yield fall back to {sizing_fallback}: "
                        f"day {', '.join(str(d) for d in undated[:8])}"
                        + (" ..." if len(undated) > 8 else "")
                        + ". Rebuild the derived inputs on **2 · Inputs** to give them one."
                    )
                if sizing_mode and len(sizing_day_dates) > 3:
                    st.warning(
                        f"{len(sizing_day_dates)} days to size against. Expect a long solve."
                    )

                # 4 · Run → Scenario Sweep: the axes the sweep may vary on top of the days.
                # Each defaults to the single value the sidebar already carries, so the tab
                # opens as the plain day sweep it was and only widens when asked. The solve
                # count is the product of all of them, which is why it is stated before the
                # button rather than discovered halfway through.
                sweep_specs = None
                if not sizing_mode:
                    axis_a, axis_b = st.columns(2)
                    with axis_a:
                        sweep_years = st.multiselect(
                            "Scenario years", options=scenario_years,
                            default=[int(st.session_state.get("run_year") or 2025)]
                                    if scenario_years else [],
                            key="sweep_years",
                            help="One solve per year, per day, per other axis. The prices of "
                                 "`costs_dataset.xlsx` are read for each.",
                        ) or ([int(st.session_state.get("run_year") or 2025)]
                              if scenario_years else [2025])
                        sweep_prices = st.multiselect(
                            "Cost scenarios", options=["best case", "worst case"],
                            default=[st.session_state.get("run_scenario", "best case")],
                            key="sweep_prices",
                            help="**best case** is cheap electricity against expensive "
                                 "diesel, **worst case** the reverse. Both is the honest "
                                 "span of an electrification case.",
                        ) or [st.session_state.get("run_scenario", "best case")]
                    with axis_b:
                        sweep_v2g = st.multiselect(
                            "V2G", options=["on", "off"],
                            default=[st.session_state.get("run_v2g", "on")],
                            key="sweep_v2g",
                            help="Running both answers what V2G is worth on this work, "
                                 "which one run alone cannot.",
                        ) or [st.session_state.get("run_v2g", "on")]
                        sweep_external = st.multiselect(
                            "External charging", options=["on", "off"],
                            default=[st.session_state.get("run_external_charging", "on")],
                            key="sweep_external",
                            help="Off makes the day run on depot charging alone.",
                        ) or [st.session_state.get("run_external_charging", "on")]

                    sweep_specs = sweep_combinations(
                        sizing_day_dates, sweep_years, sweep_prices, sweep_v2g,
                        sweep_external)
                    scenario_count = max(1, len(sweep_specs) // max(len(sizing_day_dates), 1))
                    st.caption(
                        f"**{scenario_count} scenario(s) × {len(sizing_day_dates)} day(s) = "
                        f"{len(sweep_specs)} solves.**",
                        help="Each solve is a full MILP. The sweep runs them in order, days "
                             "innermost, so one scenario's days come out together.",
                    )
                    if len(sweep_specs) > 5:
                        st.warning(
                            f"This will run **{len(sweep_specs)} optimizations back to "
                            "back**. Narrow an axis, or raise the MIP gap under "
                            "*3 · Settings → Solver*, if that is longer than you want to wait."
                        )

                range_overrides = dict(overrides)
                range_overrides.update({
                    "order_data_days": sizing_days,
                    "auto_sizing": "on" if sizing_mode else "off",
                })
                # The fleet-search budget. It belongs here and not only in the model
                # source because it is the binding constraint on how good the fleet is:
                # what it buys is not a tighter gap on a given fleet, it is how much of
                # the fleet space gets looked at before the run has to answer.
                if sizing_mode:
                    search_minutes = st.slider(
                        "Time to spend searching for the fleet (minutes)",
                        min_value=5, max_value=480, value=120, step=5,
                        key=f"design_budget_{mode_key}",
                        help=(
                            "The fleet search costs one solve per day per candidate "
                            "fleet, so what this buys is candidates examined. Measured "
                            "on a three-day range: the ownership cutoff left 308 "
                            "candidate fleets to cost at about 22 s each, so proving "
                            "that answer took roughly two hours — and a one-hour budget "
                            "got through half of them and stopped. A longer range needs "
                            "more, because every candidate costs one solve per day. "
                            "If the run stops on this limit it says so, and the fleet "
                            "it reports is the best one found rather than the best one "
                            "there is."
                        ),
                    )
                    range_overrides["design_max_search_seconds"] = int(search_minutes) * 60
                # Neither of these two kinds of run writes figures, and nothing here says
                # so any more: it follows from what they are (model 1.4b1). Every solve of
                # a sweep writes under the same names - plot_suffix (model 5.1) is only
                # ever set by a design run - so two solves are two writers of one file,
                # and no metric depends on them anyway: every number is in the per-run
                # record. run_scenario_sweep() and run_design_range() state the run kind.

                render_run_controls(
                    mode_key, range_overrides,
                    day_dates=sizing_day_dates, sweep_specs=sweep_specs)
            else:
                st.info("No routed trips yet — build the derived inputs on **2 · Inputs**.")

    # -------------------------------------------------------------- 5 · RESULTS ----
    with tab_results:
        st.subheader("Latest Results", help=(
            "The run this session produced, in full: its metrics, its figures and its "
            "schedule. It is held in memory, so it is replaced by the next run and lost "
            "when the app restarts - every run also writes a summary CSV, and those are "
            "under *Previously Results* below."
        ))
        # one run, the last one - whichever kind it was. Splitting it into "latest
        # disposition" and "latest design run" made the reader choose before they could
        # look, when in practice there is only ever one answer they just produced.
        last_mode = st.session_state.get("last_run_mode")
        if last_mode is None:
            st.info("No run yet — start one on **4 · Run**.")
        else:
            render_latest_result(
                last_mode, {"disposition": "Disposition", "sizing": "Asset Sizing"}
                .get(last_mode, "Scenario Sweep"))
        st.divider()
        st.subheader("Previous Results", help=(
            "Every run ever made from this folder, read back from its summary CSV in "
            "`results/`. The section above holds the run of *this* session and loses it "
            "when the app restarts; these are on disk and stay. One row per solve, so a "
            "single disposition is one row and a sweep or a sizing range is one per day."
        ))

        summaries = get_latest_summary_results()
        if not summaries:
            st.info(
                "No run summaries in `results/` yet. Every run writes one — start one on "
                "**4 · Run** and it appears here when it finishes."
            )
        else:
            names = [s["name"] for s in summaries]
            # Nothing is selected and nothing is read until one is picked. The list is a
            # directory scan, so it costs the same however long it gets; opening a file is
            # what costs, and on a page that reruns on every widget interaction the last
            # thing wanted is a CSV parsed each time to fill a dropdown nobody has touched.
            # The label is carried by the placeholder rather than sitting above the field,
            # which is also where the count belongs - it is a property of the list, not a
            # name for it.
            choice = st.selectbox(
                "Previous result file",
                names,
                index=None,
                placeholder=f"Pick a previous Result File here ({len(names)} found)",
                label_visibility="collapsed",
            )
            if choice is not None:
                chosen = next(s for s in summaries if s["name"] == choice)
                result_df = pd.DataFrame()
                try:
                    result_df = chosen["load"]()
                except Exception as exc:
                    note_load_failure(chosen["path"], exc)
                    st.error(f"`{choice}` could not be read — {type(exc).__name__}: {exc}")

                _display_dataframe(result_df, width='stretch', hide_index=True)
                st.download_button(
                    f"Download {choice}",
                    data=result_df.to_csv(index=False).encode("utf-8"),
                    file_name=choice,
                    mime="text/csv",
                )

                # what kind of run wrote this, from the record itself rather than from the
                # filename: the name is descriptive and a rename would silently change what
                # the page decides to show. The filename is the fallback for the older
                # summaries written before run_mode was a column.
                run_mode = ""
                if "run_mode" in result_df.columns and not result_df.empty:
                    run_mode = str(result_df["run_mode"].iloc[0] or "")
                if not run_mode:
                    run_mode = ("sweep" if "_sweep_results_" in choice
                                else "sizing" if "_sizing_results_" in choice
                                else "disposition")

                if run_mode == "sweep":
                    render_sweep_charts(result_df, f"previous_{choice}")

                # ... and then the same depth the run got when it was fresh. A summary row
                # carries the keys an in-memory result does, so the whole metric page can
                # be rebuilt from it; before this, a run was a full page while the app was
                # open and a wide table of raw column names once it had restarted.
                if not result_df.empty:
                    rows = len(result_df)
                    row_index = 0
                    if rows > 1:
                        def previous_solve_label(i):
                            row = result_df.iloc[i]
                            parts = [str(row.get("disposition_date", f"solve {i + 1}"))]
                            for key in ("scenario", "year", "v2g_status"):
                                if key in result_df.columns:
                                    parts.append(str(row.get(key)))
                            return (" · ".join(parts)
                                    + f" ({row.get('optimization_status', '-')})")

                        row_index = st.selectbox(
                            "Solve to look at in detail",
                            options=list(range(rows)), index=rows - 1,
                            format_func=previous_solve_label,
                            key=f"previous_row_{choice}",
                            help="One row of the summary above, read at the same depth as "
                                 "a run made in this session.",
                        )
                    st.markdown("#### This Solve in Detail")
                    render_result_metrics(result_df.iloc[row_index].to_dict())

                # this run's own figures, not the newest on disk. Everything a run writes
                # shares its stamp, so a summary opened weeks later shows the figures that
                # belong to its numbers instead of whatever was drawn last.
                stamp = run_stamp_of(choice)
                previous_plots = find_plots_for_stamp(stamp)
                if previous_plots:
                    render_run_figures(previous_plots, note_inputs=False)
                elif run_mode == "sweep":
                    st.caption(
                        "A sweep writes no figures: every solve would write the same "
                        "filenames, so they are switched off for the whole run. The "
                        "numbers above are the complete record of it."
                    )
                else:
                    st.caption(
                        f"No figures carrying this run's stamp (`{stamp}`) are in "
                        "`results/`. They were either turned off for the run or have been "
                        "cleaned up since."
                    )

        st.divider()


    # -------------------------------------------------------------------- HELP ----

    # ----------------------------------------------------------- 1 · START HERE ----
    with tab_help:
        # Help is prose: a page of it centred is markedly harder to read than the
        # same page left-aligned, so this one step opts out. The container key is
        # what the stylesheet keys on - Streamlit renders it as a .st-key-* class.
        with st.container(key="help_panel"):
            st.subheader("How to use this Interface")

            st.markdown(
                """
    This app plans a truck fleet's operating day: which vehicle runs which trip, when it
    charges, and when it sells energy back. Everything it knows comes from four Excel files
    in `inputs/`; everything else is derived and can be deleted at will.

    The tabs are numbered because their order matters — the inputs have to be there before
    the settings mean anything, the settings before a run, and a run before there is a
    result to read.

    **New here?** Check **2 · Inputs** shows green, leave every setting alone, and run one day
    on **4 · Run → Disposition**. That is the whole model working end to end; everything below
    is what to change once it has.

    | step | what it is for |
    | --- | --- |
    | **1 · Start Here** | this page |
    | **2 · Inputs** | the four Excel files of `inputs/` sheet by sheet, then everything derived from them in `results/` and the button that rebuilds it |
    | **3 · Settings** | every parameter of a run, grouped, plus the exact configuration that will be sent to the model |
    | **4 · Run** | pick the type of run and start it |
    | **5 · Results** | the latest run, whichever type it was, and the aggregate CSVs of earlier batch sweeps |

    Step 4 offers three ways to run the model, in the order the questions are usually asked:

    | tab | question | fleet | days |
    | --- | --- | --- | --- |
    | **Disposition** | how should this day be run? | the roster, used as given | one date you pick |
    | **Asset Sizing** | what should we buy? | chosen from the `synthetic_fleet` types | a range, all of it in **one** model |
    | **Scenario Sweep** | what would this cost under other assumptions? | the roster, used as given | a range, **one solve per day** |

    On **Disposition** the date is the only control you need: it picks the trips of that day,
    the depot's metered baseline load for that day and month, and the PV yield PVGIS reports
    for it. One setting for all three, so they cannot disagree.

    Only dates the order data actually covers can be planned. The gaps are weekends and
    holidays - the interface says so and names the nearest dates that do have trips.

    Neither range tab asks for a date: each day of the range brings its own, so the depot
    load and the PV yield always belong to the trips being planned, and a range spanning
    seasons is read against each of those seasons.

    **Asset Sizing** chooses **one fleet for the whole range**: one ownership decision shared
    by every day, operating variables per day. That sharing is the point — sizing day by day and
    taking the largest answer makes every day pay for its trucks alone, so every day
    under-buys, and none of them can see that a truck bought for Tuesday is free on
    Wednesday. The fleet is held down by what a truck costs to own, priced from
    `vehicle_price`, which is what stops the model buying one just to sell its battery into
    the V2G spread. The chargers are not optimized: the run charges against a depot built to
    fit so they cannot limit the fleet, and reports what the schedules turned out to need.
    Figures are off here, and the switch says so.

    **Scenario Sweep** leaves the fleet alone and sweeps two ways at once: over the days of
    the range, and over four scenario axes — **scenario years**, **cost scenarios**, **V2G**
    and **external charging**. Every combination is one full MILP solve, so the count is the
    product of all of them and is stated before the button rather than discovered halfway
    through. Each axis defaults to the single value the sidebar already carries, so the tab
    opens as a plain day sweep and only widens when you ask it to.

    Past **five solves** the figures and the schedule CSV are turned off for that run
    whatever the switch says: every solve writes over the previous one's files, so on a
    sweep of any size all but the last would be drawn and thrown away. The switch stays live
    and still decides for sweeps of five or fewer.

    **5 · Results** gets one row per solve, with a column for each axis that actually moved —
    an axis held fixed is not shown, since a column of one repeated value is noise in a table
    whose point is the comparison. Pick any solve to see it at the usual depth. The schedule
    table and the figures are files in `results/` that each solve overwrote in turn, so those
    are always the last one — run that day on its own on **Disposition** to see its schedule.

    Everything both share lives under **3 · Settings**. **2 · Inputs** is in two halves:
    *Original Input Files* is the four Excel datasets of `inputs/`, sheet by sheet and read
    straight from disk, and *Derived Input Files* is everything in `results/` that was built
    from them, with the rebuild button at the top of it. The two long sheets — the depot's
    metered year and the order book — are shown as a short preview with the rest folded
    into an expander.

    Closing the browser tab stops the server, so `python main.py` returns instead of leaving a
    Gurobi licence held by a window nobody is looking at.

    Nothing has to be prepared by hand. Everything the model reads from `results/` is derived
    from the four Excel datasets in `inputs/`, so anything missing there is built on sight - on
    the first run, and again whenever a derived file is deleted. Budget time for the first
    one: geocoding and routing every order in `order_dataset.xlsx` is rate-limited by the
    external services and takes tens of minutes from an empty `results/`. It is cached in
    `data/cache_routing.json` afterwards and reused unless `order_dataset.xlsx` changes. Only `inputs/` is irreplaceable - deleting
    anything in `results/` is safe.

    *Rebuild derived inputs* on the Run tab refreshes files that are already there, which is
    the one case auto-building does not cover.

    ### Data flow

    | Primary input (`inputs/`) | Derived file (`results/`) | Produced by |
    | --- | --- | --- |
    | `costs_dataset.xlsx` (`energy_yearly`) | `cost_parameter_yearly.csv` | `src/hdv_cost_parameter_generation.py` |
    | `costs_dataset.xlsx` (`energy_daily`) | `cost_parameter_hourly.csv` | `src/hdv_cost_parameter_generation.py` |
    | `depot_dataset.xlsx` (`consumption`) | `depot_load_profile.csv` | `src/hdv_depot_load_profile_generation.py` |
    | `depot_dataset.xlsx` (`generation`) | `depot_pv_parameters.csv` | `src/hdv_depot_load_profile_generation.py` |
    | `depot_dataset.xlsx` (`charging`) | `depot_charging_stations.csv` | `src/hdv_depot_load_profile_generation.py` |
    | `order_dataset.xlsx` | `order_trips.csv` | `src/hdv_trip_generation.py` |
    | `depot_dataset.xlsx` (`generation`) + `order_trips.csv` | `cache_pv_profile.json` | `src/hdv_pv_profile_generation.py` |
    | `fleet_dataset.xlsx` | read directly by the model | - |

    Nothing is ever written back into `inputs/`.

    ### Which prices a run is made at

    A **Disposition** run is priced off `energy_daily` alone — all four prices, as the
    workbook writes them. An **Asset Sizing** or **Scenario Sweep** run is priced off
    `energy_yearly`, at the scenario year and band, with the intraday shape of
    `energy_daily` scaled onto the two curves so the V2G arbitrage channel still has a
    spread to trade:

    | Price | Disposition | Asset Sizing & Scenario Sweep |
    | --- | --- | --- |
    | electricity spot curve | `energy_daily`, as written | `energy_daily` shape, levelled onto the year & band |
    | flexibility spot curve | `energy_daily`, as written | `energy_daily` shape, levelled onto the year & band |
    | public charging | `energy_daily` | `energy_yearly`, year & band |
    | diesel | `energy_daily` | `energy_yearly`, year & band |

    A disposition plans one concrete operating day, so it is priced at that day's own
    numbers and every figure in the result traces back to a cell you can point at. A sizing
    run and a sweep move the year and the scenario, so both set the level — each curve is
    normalised by its own daily mean and multiplied by that cell's value, which keeps
    whatever it is anchored on intact without the model having to know what.

    **`energy_daily` has neither a year nor a scenario band, so neither affects a
    Disposition run's prices at all** — both stay on the result as labels. To compare years
    or scenarios, run a Scenario Sweep. The diesel price is flat on every kind of run;
    which sheet states it is not a question about its shape.

    Where the daily curve already averages out to the band it is run under the two are
    identical and the factor is 1. `hdv_cost_parameter_generation.py` prints the factor for
    every band on every rebuild — that is exactly how far a sweep of the base year sits
    from a disposition of the same day.

    ### Battery day boundary

    *Day start & target end SoC* is one figure for both ends of the day, 50 % by default:
    every bev starts at 00:00 with that share of its own capacity and has to be back at it by
    24:00. Driving and V2G discharge therefore both have to be charged back before the day
    closes, so a schedule cannot be financed by depleting the batteries overnight. Ending
    above the target is allowed, ending below it is not.

    ### Charging infrastructure

    The `charging` sheet of `depot_dataset.xlsx` is the station list, one row per station
    with its own `charger_power_kW`, used verbatim - nothing about the infrastructure is
    synthesized here. Every station is a charging opportunity in every 30-min step, capped at
    that station's power and at the truck's own, one truck per station and step. Add, remove
    or re-power a row; the derived station list rebuilds on the next run. Results name them by
    their `charger_id`.

    ### Depot PV

    The depot plant is described in `depot_dataset.xlsx`, sheet `generation`: location, peak
    power, tilt and azimuth. PVGIS is asked once for a representative year of that plant;
    every day in `trips.csv` is sliced from that year onto the 30-min grid and stored in
    `data/cache_pv_profile.json`. The cache carries a fingerprint of `order_dataset.xlsx`
    and `depot_dataset.xlsx` and is rebuilt whenever either file no longer matches, so the
    curves always belong to the current files in `inputs/`. The site's own load (sheet
    `consumption`) is served from the plant first; the surplus charges the trucks and is
    billed at **spot − the PV selling overhead** — the revenue given up by not selling that
    kWh. Grid charging is billed at **spot + the grid overhead**. The spot term is in both
    and cancels, so the PV advantage per kWh is simply the two overheads added
    (15 ct/kWh at the defaults) and does not depend on the market price at all: a
    self-consumed kWh is never taxed or tariffed.

    ### Slack notification

    **3 · Settings → Notifications** posts a message when a run finishes. The bot token is
    read from the `SLACK_BOT_TOKEN` environment variable and from nowhere else — not from a
    parameter, not from a file in the repository, and not from anything this interface
    stores. Set it once:

    ```bash
    setx SLACK_BOT_TOKEN "xoxb-..."
    ```

    Then **start the interface from a new terminal**. A process only sees the environment it
    was launched with, so a variable set after `python main.py` started is invisible to it —
    which is the usual reason the switch is greyed out on a machine where the token is in
    fact set. A refused post never fails the run: the result is kept and the reason is shown
    next to the Run button.

    ### Running without the interface

    Everything this interface does can be done from a terminal, and it is the same code —
    the runner applies your settings on top of the model defaults and calls the model's own
    `build_runtime_context()` and `run_optimization()`, so neither way can drift from the
    other. `main.py` starts this interface by default; the flags opt out of it.

    | command | what it does |
    | --- | --- |
    | `python main.py` | start this interface on <http://localhost:8501> |
    | `python main.py --port 8600` | ... on another port |
    | `python main.py --prepare` | regenerate the derived inputs in `results/`, then stop |
    | `python main.py --optimize` | prepare the derived inputs, then run the parameter sweep |
    | `python main.py --no-interface` | the same run, spelled for the case it exists for: a remote server, or any machine with no browser |
    | `python main.py --force-routing` | with `--prepare`/`--optimize`: re-route the order data even if `order_trips.csv` is current |
    | `python main.py --no-plots` | with `--prepare`/`--optimize`: skip the figures of the preparation steps |
    | `python src/hdv_disposition_optimization.py` | run the sweep directly, skipping the preparation step |

    That is the whole flag set — there is no `--date` and no `--mipgap`. What a run plans and
    how hard it solves are parameters of `src/hdv_disposition_optimization.py` (sections
    1.3/1.4), reached either by editing that block or with `model_parameters()` below.

    **The date is not typed in.** `date_disposition` does *not* choose the day: it only
    decides which depot load and PV curves are pre-built, and the day's own date overrides
    them anyway. What a run plans is `order_data_days`, and each day's calendar date is read
    from `trip_date` in `data/order_trips.csv` — the same mapping the date picker on **4 · Run**
    offers. `order_data_days = [N, N]` is the single-day form.

    ### One day, by date, with a MIP gap

    `model_parameters()` sets module parameters for the length of a block and puts them back
    afterwards, refusing any name that is not already a parameter so a typo fails loudly.
    From the project root:

    ```bash
    python - <<'PY'
    import sys, pandas as pd
    sys.path.insert(0, 'src')
    import hdv_disposition_optimization as opt

    DATE   = '2024-01-04'     # the day to plan
    MIPGAP = 0.2              # 0...1

    trips = pd.read_csv('data/order_trips.csv')
    day = int(trips.loc[pd.to_datetime(trips['trip_date']) == DATE, 'day_ID'].iloc[0])

    with opt.model_parameters(order_data_days=[day, day], optimization_MIPGap=MIPGAP):
        opt.build_runtime_context()
        results, _ = opt.run_optimization(('best case', 2025, 'on', 1))
    print('date planned:', results['solve_date'], '| gap:', results['mip_gap'])
    PY
    ```

    The lookup only spares you translating a date into a `day_ID` by hand. The tuple is
    `(scenario, scenario_year, v2g_status, day)`, where `day` is the position *inside* the
    chosen range and is therefore always `1` for a single day. Call `build_runtime_context()`
    **inside** the block: it reads `order_data_days` when it loads the trip set, so a
    parameter set afterwards has nothing left to affect. Anything else from sections 1.3/1.4
    goes in the same call — `work_hours_start='06:00'`, `optimization_time_limit_s=600`.

    Outputs land in `results/` under the run's own timestamp, so a terminal run never
    overwrites an earlier one and appears on **5 · Results** like any other.

    ### Batch sweeps

    This interface runs one scenario at a time. For sweeps over fleet sizes, years or
    scenarios, edit the parameter block at the top of
    `src/hdv_disposition_optimization.py` and run `python main.py --optimize`. Set
    `slack_notification_status = 'on'` in that same block for a Slack message when the
    sweep finishes.

    ### Troubleshooting

    - **Run button disabled** - either a dataset in `inputs/` is missing (those cannot be
      regenerated) or a derived input failed to build; the Run tab names which.
    - **`ModuleNotFoundError: gurobipy`** - install gurobipy and a valid licence in the same
      environment that runs Streamlit.
    - **Solver status `infeasible`** - the fleet cannot serve the day's trips. Pick another
      day, add rows to the `charging` sheet of `depot_dataset.xlsx` or raise their kW, or edit
      `fleet_dataset.xlsx` to add vehicles or lower the bev share. Both are datasets, so
      edit them and run again. Widening the working hours will not help: a trip that
      does not fit inside them is already scheduled by its own window.
    - **Long runtimes** - raise the MIP gap, lower `Monte-Carlo samples per trip`, or use a
      smaller roster in `fleet_dataset.xlsx` or a shorter `charging` sheet.
    - **A "Script execution error" naming a file that does not exist** - the browser is
      connected to a Streamlit server from an earlier session that still holds the port, not
      to the app just started. `python main.py` refuses to start in that case and says how to
      free the port or choose another one.
            """
            )

            st.markdown("**Project root**")
            st.code(str(PROJECT_ROOT))

            st.markdown("**Input Status**")
            st.json({
                "primary_inputs_missing": [p.name for p in missing_primary],
                "derived_inputs_missing": [p.name for p, _ in missing_derived],
                "trip_days_available": trip_days,
                "scenario_years_available": f"{scenario_years[0]}-{scenario_years[-1]}" if scenario_years else None,
            })


# 7 MAIN
if __name__ == '__main__':
    if running_under_streamlit():
        render_app()
    else:
        sys.exit(launch())