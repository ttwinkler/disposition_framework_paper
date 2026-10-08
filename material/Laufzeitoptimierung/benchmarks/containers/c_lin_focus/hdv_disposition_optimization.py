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
import multiprocessing
import numpy as np
import pandas as pd
import gurobipy as gp
import requests
from pathlib import Path
from datetime import datetime
from slack_sdk import WebClient
# matplotlib is imported lazily by _pyplot() - see section 1.1b

sys.path.insert(0, 'C:\\Users\\go25pux\\Desktop\\Framework\\02_Modell\\src')

from hdv_global_parameter import global_parameter

# 1.2 project paths
#     data/    primary inputs only - the Excel datasets, never written to
#     results/ every generated artefact
PROJECT_ROOT              = Path('C:\\Users\\go25pux\\Desktop\\Framework\\02_Modell')
DATA_DIR                  = PROJECT_ROOT / 'data'
RESULTS_DIR               = PROJECT_ROOT / 'results'
FLEET_DATASET             = DATA_DIR / 'fleet_dataset.xlsx'
# the other primary datasets, named here so a derived file can be checked against the one
# it was built from rather than only for existence
ENERGY_DATASET            = DATA_DIR / 'energy_dataset.xlsx'
DEPOT_DATASET             = DATA_DIR / 'depot_dataset.xlsx'
TRIPS_CSV                 = RESULTS_DIR / 'trips.csv'
COST_PARAMETER_ENERGY_CSV = RESULTS_DIR / 'cost_parameter_energy.csv'
COST_PARAMETER_V2G_CSV    = RESULTS_DIR / 'cost_parameter_v2g.csv'
DEPOT_LOAD_PROFILE_CSV    = RESULTS_DIR / 'depot_load_profile.csv'
DEPOT_PV_PARAMETER_CSV    = RESULTS_DIR / 'depot_pv_parameters.csv'
DEPOT_CHARGING_CSV        = RESULTS_DIR / 'depot_charging_stations.csv'
PV_PROFILE_CACHE_JSON     = RESULTS_DIR / 'pv_profile_cache.json'  # PVGIS answers, shared by all processes
CSV_ENCODING              = 'utf-8'
# below this the solver's answer is numerical noise, not a charge or a discharge. Used to
# decide whether a step really moved energy when the schedule is written.
ENERGY_TOLERANCE_KWH      = 1e-6


def project_path(*parts):
    return PROJECT_ROOT.joinpath(*parts)


def ensure_results_dir():
    """Create results/ on demand and return it."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def require_input(path, produced_by=None):
    """Fail fast with an actionable message instead of a bare FileNotFoundError."""
    path = Path(path)
    if path.exists():
        return path
    hint = f" Run {produced_by} first." if produced_by else ""
    raise FileNotFoundError(f"Missing input file: {path}.{hint}")


# 1.2b derived inputs: which generator produces which file in results/
#      Everything the model reads from results/ is derived from a dataset in data/, so a
#      missing file is not an error - it just has not been built yet. ensure_derived_inputs()
#      builds whatever is absent, which is why none of the entry points has to be primed
#      by hand. Only data/ is irreplaceable.
#      Each entry also names the dataset it is derived FROM, because "present" is not the
#      same as "current": a derived file built from a previous version of its Excel is
#      exactly as wrong as a missing one and far harder to notice. If the source is newer
#      than what was derived from it, the file is rebuilt.
#      Trip generation is exempt - its source is the order data, but rebuilding it
#      re-geocodes and re-routes every order, so a stale trips.csv is announced and left
#      for the operator to refresh deliberately.
DERIVED_INPUT_BUILDERS = (
    ((COST_PARAMETER_ENERGY_CSV, COST_PARAMETER_V2G_CSV),
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
    """Build every derived input in results/ that is not there yet.

    The generators are imported only when something is actually missing: on the normal
    path - all files present - this costs a handful of stat() calls and no imports.
    Trip generation geocodes and routes every order, so building that one takes minutes;
    it is announced rather than done silently.
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
        if stale and not absent and TRIPS_CSV in [Path(p) for p in paths]:
            # never rebuilt implicitly: it costs minutes of geocoding and routing
            if announce:
                print(f"note: {label} is older than {Path(source).name}; "
                      f"run with --force-routing to refresh it", flush=True)
            continue
        if announce:
            reason = (f"{', '.join(p.name for p in absent)} missing" if absent
                      else f"{', '.join(p.name for p in stale)} older than {Path(source).name}")
            print(f"deriving {label} from data/ ({reason}) ...", flush=True)
        ensure_results_dir()
        module = importlib.import_module(module_name)
        generator = getattr(module, function_name)
        # generate_trips() takes no make_plots; the other two default it to True, and a
        # figure nobody asked for is not worth the time here
        if function_name == 'generate_trips':
            generator()
        else:
            generator(make_plots=False)
        built.append(label)
    return built


os.chdir(PROJECT_ROOT)
ensure_results_dir()


# every figure is written as PNG. A raster keeps the browser fast: the Streamlit
# page lays the figures out again on every interaction, and a vector plot of a
# dense schedule costs it thousands of DOM nodes each time. 150 dpi so the raster
# still holds up when zoomed.
FIGURE_DPI = 150


# 1.1b lazy matplotlib
_PYPLOT = None


def _pyplot():
    """Import pyplot and the legend patch artist on first use.

    Only the show_outputs == 'on' plotting paths in postprocess() need matplotlib.
    Keeping it out of the module header means a sweep worker (which spawns a fresh
    interpreter and imports this module) and any headless single run never pay for it.
    """
    global _PYPLOT
    if _PYPLOT is None:
        import matplotlib
        matplotlib.use('Agg')  # figures are always written to results/, never shown
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
        _PYPLOT = (plt, Patch)
    return _PYPLOT


# 1.2b primary fleet input
#      The fleet is never synthesized: data/fleet_dataset.xlsx is the roster, one row
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


def load_fleet_dataset(filepath=None):
    """Read data/fleet_dataset.xlsx and return the roster in the canonical schema."""
    path = require_input(filepath or FLEET_DATASET)
    if path.suffix.lower() in ('.xlsx', '.xls'):
        fleet_df = pd.read_excel(path)
    else:
        fleet_df = pd.read_csv(path, encoding=CSV_ENCODING)
    if fleet_df.empty:
        raise ValueError(f"Fleet input {path} contains no rows.")
    return normalize_fleet(fleet_df)


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
# best incumbent found, which is a *different kind of answer* - the reported gap then says
# how far off it might be, and a run that found nothing at all returns the 999999
# sentinels. Prefer raising optimization_MIPGap over capping the clock: a converged 20%
# answer is at least a bounded one.
optimization_time_limit_s           = None
show_outputs                        = 'on'  # generate plots, csvs, and prints; use only with little optimization scenarios
auto_sizing                         = 'off'  # let the optimizer pick the minimum subset of the roster and the charging infrastructure it needs

# 1.4 feature parameters
fleet_input_file                    = FLEET_DATASET  # roster used verbatim; size and bev share follow from this file
use_real_vehicle_parameters         = 'off'  # 'on' overrides the bev specs from fleet_dataset.xlsx with the constants below; keep 'off' so the Excel input stays authoritative
real_bev_parameters                 = {'vehicle_consumption': 110, 'vehicle_energy_storage': 332, 'vehicle_charging_power': 375}  # kWh/100km, kWh, kW
external_charging_status            = 'on'  # allow charging at public stations, priced at public_charging_price_€/kWh and penalised per minute (see below)
# Lenkzeitpause, the driver's mandatory rest break (EU Regulation 561/2006 as it applies
# in Germany). Charging at a public station costs the driver time, which is what
# penalty_charging_external_time prices - unless the truck has to stand still anyway
# because a break is due, in which case the charging is free of that penalty: the time was
# already lost to the break.
driving_time_before_break_minutes   = 270  # Fahrdauer after which a Lenkzeitpause is due [min]. 4.5 h is the statutory figure. Also sets how much break a long trip carries inside it (2.6b)
driving_break_duration_minutes      = 45   # Dauer der Lenkzeitpause [min], and therefore the penalty-free window it opens. 45 min is the statutory figure
# depot/local and external charging prices are NOT set here: they come from
# energy_dataset.xlsx (energy_spot_price_€/kWh and public_charging_price_€/kWh,
# sheet 'energy_yearly') via results/cost_parameter_energy.csv, so the Excel stays
# the single source. energy_spot_price is the public spot price of electricity, not a
# retail tariff of this depot: it carries no grid fees, levies or taxes, and it is the
# same curve the arbitrage channel sells into.
degradation_cost_status             = 'on'  # battery aging via equivalent full cycles
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
# overheads set here. energy_spot_price_€/kWh is the bare public market price; nothing
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

# 1.4c drivers. Rostered after the optimization, not inside it (see
# src/hdv_driver_scheduling.py): which truck runs which trip is an energy and cost
# decision the MILP is built to make, while covering the movements that result is a
# rostering problem that follows from it.
#
# A driver is tied to a vehicle exactly while that vehicle is away from the home depot, so
# the day splits into indivisible duty blocks - one per absence - and a driver may change
# vehicles between them, which by construction happens at the depot. Gaps between one
# driver's blocks are their breaks, and they are spent at the depot for the same reason.
#
# The crew rules are now IN the objective and the constraints (3.3.17), not only checked
# afterwards. The optimizer therefore trades driver hours against energy, and the reason
# waiting in a customer yard is expensive is that a driver is standing in it: every step a
# vehicle spends away from home is a step somebody is paid for, whether the wheels turn or
# not. src/hdv_driver_scheduling.py still builds the roster afterwards, but now from a
# schedule that was shaped to be crewable.
# Who is behind the wheel - and whether there is a wheel to be behind. This is a scenario
# switch, not a feature flag: it is what makes "what would this fleet cost if it drove
# itself" a question the model can answer.
#
#   'crewed'      every vehicle carries a driver. The Lenkzeit and Arbeitszeit limits of
#                 3.3.17 apply, driver hours are in the objective, long trips and legs
#                 carry their Lenkzeitpause (2.6b), and trips no driver could run legally
#                 are removed before the solve (2.6c).
#   'autonomous'  none of the above. No crew limits, no driver cost, no mandatory breaks,
#                 and nothing is removed - a truck that needs 13 h away and 11 h of driving
#                 is simply a truck that drives for 13 hours.
#
# The difference between the two runs is the value of autonomy for this fleet, and it is
# not only the wage bill: on day 2 the crewed run has to drop five Ruhr loads (1899 km)
# that an autonomous one serves without comment.
fleet_operation_mode                = 'crewed'  # 'crewed' or 'autonomous'
driver_hourly_rate_eur              = 20.0  # € per hour a driver is away from the depot with a vehicle
driver_max_driving_hours            = 9.0   # Lenkzeit: hours actually driving in one driver's day, breaks and waiting excluded
driver_max_shift_hours              = 12.0  # Arbeitszeit: longest continuous absence from the depot [h] - driving plus the breaks and waiting in between
penalty_driver_use                  = 20.0  # € per driver on the day's peak roster; a tie-break towards fewer, longer shifts rather than many short ones, in the same spirit as penalty_vehicle_use
# € per half-hour by which a shift or a day's driving runs over the limits above. The
# limits are priced rather than hard because some trips cannot be crewed legally from one
# depot at all - see 3.3.17 - and a hard rule would answer that with a bare "infeasible"
# that names nothing. At this price the solver breaks them only where no legal schedule
# exists, and the run reports every half-hour it had to.
penalty_crew_rule_breach            = 500.0
# The crew constraints tie every step of a vehicle to every earlier one, which leaves the
# LP relaxation a poor guide and makes *finding a first feasible schedule* the hard part
# rather than closing the last few percent. So the run solves twice: once without 3.3.17,
# which is quick, and then again with it, handed the first answer as a starting point. The
# relaxed schedule already places the trips and the routes sensibly; the second solve only
# has to repair it where the crew rules bite, which is a far smaller search than building
# one from nothing.
driver_warm_start                   = 'on'   # 'off' goes straight to the constrained solve
driver_warm_start_MIPGap            = 0.30   # the relaxed solve only has to be good enough to start from, not optimal

# Truck toll rates (fixed € per km driven, type-specific; added to objective)
diesel_truck_toll_eur_per_km        = 0.183   # diesel/ice HDV toll rate
bev_truck_toll_eur_per_km           = 0.0     # bev HDV toll rate (often reduced/exempt)
peak_power_price_eur_per_kW         = 17  # DSO demand charge [€/kW/year] billed on the highest power the depot draws from the public grid; charged to a single day via /365 on that day's maximum
v2v_status                          = 'on'   # vehicle-to-vehicle: energy passed straight from a discharging truck to a charging one at the depot, never crossing the meter and so never paying either overhead. 'off' bills both sides against the grid as before
v2g_price_mode                      = 'both'  # which channel a discharged kWh is sold into: 'arbitrage' (electricity spot price, the same curve the truck buys at, so the earning is the intraday spread), 'flexibility' (the flexibility spot price, paid for the service rather than for the energy, and the one price the selling overhead is not deducted from), or 'both' = whichever of the two pays more in that step. 'both' is a choice, not a sum: a kWh leaves the battery once and can only be sold once.
v2g_arbitrage_price_file            = None  # optional CSV with columns time_step, price_€/MWh; without one the curve comes from energy_spot_price in energy_dataset.xlsx
v2g_flexibility_price_file          = None  # optional CSV, same format; without one the curve comes from flexibility_spot_price in energy_dataset.xlsx
block_arbitrage_extreme_slots       = 'on'  # block trip starts in highest/lowest arbitrage windows
charging_curve_status               = 'on'  # derate charging power above 80% SoC
# smallest discharge a V2G slot has to deliver [kWh per 30-min step]. A slot that is
# claimed for V2G but delivers nothing is not V2G, so the model forbids it; this is the
# threshold below which taking the slot is not worth calling a discharge. Small against
# the ~175 kWh a 350 kW truck can deliver in a step, large against the solver's
# feasibility tolerance. Raise it to state a minimum bid size the plant has to meet.
v2g_min_discharge_kWh               = 1.0
# the same floor on the *external* charging side: a truck that occupies a public charger
# has to draw at least this much in the step [kWh per 30-min step]. Capped by what the
# truck can take, so it is never asked for more than that.
# Depot charging needs no such floor - 3.3.11b pins it to full power, which is a far
# stronger statement than any minimum.
charging_min_energy_kWh             = 1.0

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
use_virtual_trip_model_for_v2g_and_charging = 'on'  # V2G as explicit discharge "trips", charging as explicit "charge trips" (negative discharge) with same z/SoC/assignment logic as real trips
advanced_degradation_status           = 'on'  # per-vehicle EFC vars + max distribution penalty + drive consumption in EFC + simple SoC weighting on throughput
degradation_distribution_penalty      = 50.0  # extra € penalty per (kWh equiv) on the worst-case vehicle EFC to force aging distribution across fleet instead of concentrating on few
soc_weight_factor                     = 0.5   # advanced aging: the discharged energy counts 1.0x at half charge and (1 + this)x at either extreme, w = 1 + 4*factor*(SoC/cap - 0.5)^2; 0 switches the weighting off entirely
soc_weight_breakpoints                = 9     # tangents used to approximate that parabola piecewise-linearly (9 -> within 1.6% of it); more is closer and slower

# 1.5 other parameters
slack_notification_token            = global_parameter['framework_slack_notification_token']
penalty_charging_use                = 1  # € per occupied 30-min slot at a charger or on V2G; keeps the model from occupying a vehicle for a flow it does not use; tune as needed
penalty_vehicle_use                 = 10  # € per used vehicle; tune as needed
penalty_charging_external_time      = 10  # Strafe pro Minute externes Laden [€/min]: what a minute spent charging at a public station costs beyond the energy - driver time, the detour, the tour not driven. Waived inside a Lenkzeitpause, where the truck stands still regardless. At 30 min per step this is 300 €/step, so it dominates the energy price; tune as needed
penalty_vehicle_id_order            = 1
# day-boundary SoC of every bev, as a fraction of its own capacity: the level each one
# starts the day with at 00:00 and, at the same time, the level it has to have reached
# again at 24:00 (see 3.3.6b). One figure for both ends, so the day is repeatable.
initial_soc_fraction                = 0.5

# 1.7 every value derived from the parameters above is built by
#     build_runtime_context() once the helper functions are defined (section 2.1)

# 1.10 function to convert time to time-step
def time_to_step(time_str):
    h, m = map(int, time_str.split(':'))
    minutes = h * 60 + m
    return (minutes // 30)

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

    step = round((hours * 60 + minutes) / 30)
    if not 0 <= step <= 48:
        raise ValueError(f"{label} must be within 00:00...24:00, got {value!r}.")
    return step


# 1.11 function to convert time-steps in time
def step_to_time(step):
    total_minutes = step * 30
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


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
    df['step'] = df['date'].dt.hour * 2 + (df['date'].dt.minute // 30)

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
    """PV plant of the depot [dict], from results/depot_pv_parameters.csv.

    The five numbers originate in sheet 'generation' of data/depot_dataset.xlsx: they
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
    """Charging stations of the depot, from results/depot_charging_stations.csv.

    Returns (ids, powers_kW) in sheet order, both as plain lists. The rows originate in
    sheet 'charging' of data/depot_dataset.xlsx and are used verbatim: the depot has as
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


def _simplified_pv_profile(steps, peak_kW):
    """Fallback bell-shaped intraday PV generation curve [kW]."""
    profile = []
    for t in steps:
        hour = t * STEP_HOURS
        # bell-shaped generation between 06:00 and 20:00
        if 6 <= hour <= 20:
            profile.append(peak_kW * math.sin((hour - 6) / 14 * math.pi))
        else:
            profile.append(0.0)
    return profile


_pv_profile_cache = {}

# where the PV curve of the last build came from: 'pvgis', 'cache' or 'synthetic'.
# Reported with the results, so a run made without a real curve says so.
pv_profile_source = None


def pvgis_aspect(azimuth_deg):
    """Compass azimuth of the modules -> the 'aspect' PVGIS expects.

    depot_dataset.xlsx states the orientation the way a site plan does (0 = north,
    90 = east, 180 = south, 270 = west); PVGIS counts from south (0 = south,
    -90 = east, +90 = west). Without the conversion a south-facing plant would be
    queried as if it faced west.
    """
    return ((float(azimuth_deg) - 180.0 + 180.0) % 360.0) - 180.0


def _pv_cache_key(steps, peak_kW, date, lat, lon, tilt_deg, azimuth_deg):
    """Stable string key for one PVGIS answer, usable as a JSON object key."""
    return (f"{len(steps)}|{float(peak_kW):g}|{date}|{float(lat):.5f}|{float(lon):.5f}"
            f"|{float(tilt_deg):g}|{float(azimuth_deg):g}")


def _pv_disk_cache_read(key):
    """PV profile for `key` from results/, or None.

    The in-memory cache does not survive across processes, and the parameter sweep
    spawns a fresh interpreter per worker - without this every worker would repeat the
    same PVGIS round trip (and wait out its 45 s timeout whenever the API is down).
    """
    if not PV_PROFILE_CACHE_JSON.exists():
        return None
    try:
        with open(PV_PROFILE_CACHE_JSON, encoding=CSV_ENCODING) as handle:
            entry = json.load(handle).get(key)
        return [float(v) for v in entry] if entry else None
    except Exception:
        return None  # unreadable or half-written cache: just query PVGIS again


def _pv_disk_cache_write(key, profile):
    """Add one PV profile to the results/ cache, atomically.

    Workers write concurrently, so the file is replaced in one step rather than being
    updated in place; a reader either sees the old file or the new one, never a partial.
    """
    try:
        ensure_results_dir()
        entries = {}
        if PV_PROFILE_CACHE_JSON.exists():
            try:
                with open(PV_PROFILE_CACHE_JSON, encoding=CSV_ENCODING) as handle:
                    entries = json.load(handle)
            except Exception:
                entries = {}
        entries[key] = [round(float(v), 4) for v in profile]
        temporary = PV_PROFILE_CACHE_JSON.with_suffix(f'.{os.getpid()}.tmp')
        with open(temporary, 'w', encoding=CSV_ENCODING) as handle:
            json.dump(entries, handle)
        os.replace(temporary, PV_PROFILE_CACHE_JSON)
    except Exception:
        pass  # a cache miss next time is acceptable; a failed run is not


# the timestamp spellings PVGIS uses in the seriescalc answer. Parsed strictly against
# this list: a format outside it means the API changed, which is worth an error rather
# than a guess at what the record meant.
PVGIS_TIMESTAMP_FORMATS = ('%Y%m%d:%H%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M')


def _parse_pvgis_timestamp(text):
    """One PVGIS timestamp, or None if it matches none of the documented formats."""
    for fmt in PVGIS_TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _pvgis_hourly(url, retries, backoff_s):
    """The hourly series of one seriescalc call, retrying a failing round trip.

    Network hiccups and 5xx answers are transient and worth retrying; an exhausted retry
    budget is not something to paper over, so it raises. The caller decides what a run
    without a real PV curve should do.
    """
    last_error = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.get(url, timeout=45)
            response.raise_for_status()
            hourly = response.json().get('outputs', {}).get('hourly', [])
            if hourly:
                return hourly
            last_error = 'the answer carried no hourly series'
        except Exception as exc:                      # network, HTTP, JSON
            last_error = f'{type(exc).__name__}: {exc}'
        if attempt < max(1, retries):
            time.sleep(backoff_s * attempt)
    raise RuntimeError(f"PVGIS did not answer after {max(1, retries)} attempts "
                       f"({last_error}).")


def generate_pv_intraday_profile(steps, peak_kW, date, lat, lon,
                                 tilt_deg=35.0, azimuth_deg=180.0):
    """Generate real intraday PV power profile [kW] for the given date (uses DOY) and the depot plant via PVGIS seriescalc API.

    peak_kW, lat, lon, tilt_deg and azimuth_deg are the user inputs of
    depot_dataset.xlsx (sheet 'generation'); the date is the disposition date, which
    selects the day of year the curve is taken from. Queries PVGIS for a representative
    year, takes that calendar day, and interpolates its 24 hourly values onto the 30-min
    grid.

    A PVGIS round trip that fails is retried; one that keeps failing raises. The curve
    decides how much of the depot's own generation reaches the trucks, so a synthetic
    stand-in would change every energy figure of the run while looking exactly like a real
    one. Set pv_allow_synthetic_profile = 'on' to accept the simplified bell curve anyway;
    the run then says so, and pv_profile_source records it in the result table.
    """
    global pv_profile_source

    # the PVGIS query is a network round trip; the answer only depends on the plant and
    # the date, so it is cached in memory for this process and on disk for every other
    # one (sweep workers are separate interpreters and would each repeat the query)
    cache_key = _pv_cache_key(steps, peak_kW, date, lat, lon, tilt_deg, azimuth_deg)
    if cache_key in _pv_profile_cache:
        pv_profile_source = 'cache'
        return list(_pv_profile_cache[cache_key])
    from_disk = _pv_disk_cache_read(cache_key)
    if from_disk is not None and len(from_disk) == len(steps):
        _pv_profile_cache[cache_key] = from_disk
        pv_profile_source = 'cache'
        return list(from_disk)

    if isinstance(date, str):
        target = datetime.strptime(date, '%d.%m.%Y')
    else:
        target = date
    target_key = (target.month, target.day)

    # Representative year with full coverage in PVGIS SARAH etc. (change if needed)
    rep_year = 2020
    url = (
        "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc"
        f"?lat={lat}&lon={lon}"
        f"&startyear={rep_year}&endyear={rep_year}"
        "&pvcalculation=1"
        f"&peakpower={peak_kW}"
        "&loss=14"
        f"&angle={float(tilt_deg):g}&aspect={pvgis_aspect(azimuth_deg):g}"
        "&pvtechchoice=crystSi&mountingplace=free"
        "&outputformat=json"
    )
    try:
        hourly = _pvgis_hourly(url, pv_profile_retries, pv_profile_retry_backoff_s)

        # hour of the target day -> kW. PVGIS reports the system power P in W.
        hours_of_target_day = {}
        unparsed = []
        for record in hourly:
            stamp = _parse_pvgis_timestamp(str(record.get('time', '')))
            if stamp is None:
                unparsed.append(str(record.get('time', '')))
                continue
            if (stamp.month, stamp.day) == target_key:
                hours_of_target_day[stamp.hour] = float(record.get('P', 0.0) or 0.0) / 1000.0

        # a timestamp the documented formats do not cover means the API changed shape;
        # silently skipping those records would quietly punch holes in the day
        if unparsed:
            raise RuntimeError(
                f"PVGIS returned {len(unparsed)} timestamp(s) in an unknown format, "
                f"e.g. {unparsed[:3]}. Expected one of {list(PVGIS_TIMESTAMP_FORMATS)}.")
        missing_hours = [h for h in range(24) if h not in hours_of_target_day]
        if missing_hours:
            raise RuntimeError(
                f"PVGIS returned no value for hour(s) {missing_hours} of "
                f"{target.strftime('%d.%m.')} in the representative year {rep_year}. "
                f"The day has to be complete before it can be used.")
        prof24 = [hours_of_target_day[h] for h in range(24)]

        # Interpolate 24h -> the 30-min grid (linear between integer hours)
        profile = []
        for t in steps:
            hour = t * STEP_HOURS
            lower = int(math.floor(hour)) % 24
            upper = (lower + 1) % 24
            frac = hour - math.floor(hour)
            value = prof24[lower] * (1.0 - frac) + prof24[upper] * frac
            profile.append(max(0.0, float(value)))

        _pv_profile_cache[cache_key] = list(profile)
        _pv_disk_cache_write(cache_key, profile)
        pv_profile_source = 'pvgis'
        return profile

    except Exception as exc:
        if pv_allow_synthetic_profile != 'on':
            raise RuntimeError(
                f"Could not obtain the depot PV curve for {target.strftime('%d.%m.%Y')} "
                f"at {lat}/{lon}: {exc}\n"
                f"The curve drives every energy figure of the run, so it is not "
                f"substituted silently. Retry when PVGIS is reachable, or set "
                f"pv_allow_synthetic_profile = 'on' to accept a simplified bell curve - "
                f"the run then reports pv_profile_source = 'synthetic'."
            ) from exc
        # opted in: announced, and deliberately not cached, so a transient outage cannot
        # freeze the synthetic curve into results/ for good
        print(f"WARNING: PVGIS unavailable ({exc}). Using the simplified PV curve; "
              f"every PV figure of this run is synthetic.", flush=True)
        pv_profile_source = 'synthetic'
        return _simplified_pv_profile(steps, peak_kW)


def load_v2g_price_curve(filepath, steps, fallback_per_step):
    """Override a channel's price curve [€/MWh] from a CSV; else keep the given curve.

    fallback_per_step is already one price per time step - the curve build_runtime_context
    derived from energy_dataset.xlsx. It used to be six 4h-period values indexed here as
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
    if 'time_step' in df.columns:
        price_map = dict(zip(df['time_step'].astype(int), df.iloc[:, -1]))
        return [float(price_map.get(t, fallback[i])) for i, t in enumerate(steps)]
    return fallback


def get_arbitrage_blocked_steps(arbitrage_prices, steps):
    """Identify highest and lowest 4h arbitrage windows; block trip starts there."""
    period_prices = []
    for p in range(6):
        period_steps = [t for t in steps if t // 8 == p]
        period_prices.append((p, sum(arbitrage_prices[t] for t in period_steps) / len(period_steps)))
    high_period = max(period_prices, key=lambda x: x[1])[0]
    low_period = min(period_prices, key=lambda x: x[1])[0]
    blocked = {t for p in (high_period, low_period) for t in steps if t // 8 == p}
    return blocked


def apply_real_vehicle_parameters(fleet_df):
    """Optionally override the bev specs of fleet_dataset.xlsx with fixed reference values."""
    if use_real_vehicle_parameters != 'on':
        return fleet_df
    fleet_df = fleet_df.copy()
    bev_mask = fleet_df['vehicle_type'] == 'bev'
    for column, value in real_bev_parameters.items():
        fleet_df.loc[bev_mask, column] = value
    return fleet_df


LINDEGRAD_BINS = 8


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
    global time_steps, STEP_HOURS, driving_break_duration_steps, driving_time_before_break_steps, work_start_step, work_end_step, fleet_dataset, all_trips, trips_dataset_amount, date_disposition, cost_parameters_energy, cost_parameters_v2g, depot_baseline_load_kW, pv_generation_kW, pv_charging_available_kWh, pv_site_parameters, pv_peak_power_kW, depot_latitude, depot_longitude, pv_tilt_deg, pv_azimuth_deg, charging_infrastructure, charging_station_ids, total_iterations, max_parallel_workers, multiprocessing_status, gurobi_threads, v2g_by_hour, v2g_channel_curves

    # 2.1.0 whatever this model reads from results/ is derived from data/, so build any
    #       part of it that is not on disk instead of demanding it from the caller
    ensure_derived_inputs()

    # 2.1.1 time grid, fleet roster and trip set
    time_steps                          = list(range(0, 48))
    STEP_HOURS                          = 0.5
    # Lenkzeitpause on the 30-min grid. The break window is rounded up, so a 45-min break
    # covers two steps rather than being clipped to one; the driving time that triggers it
    # is rounded down, so a trip is never denied a break it has legally earned.
    driving_break_duration_steps        = max(1, math.ceil(driving_break_duration_minutes / 30))
    driving_time_before_break_steps     = max(1, int(driving_time_before_break_minutes // 30))
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
    # the roster is read once and used verbatim - there is no fleet size / mix sweep
    fleet_dataset                       = load_fleet_dataset(fleet_input_file)
    # ... and so is the charging infrastructure: one station per row of the 'charging'
    # sheet, each with its own kW. Nothing here decides how many stations there are.
    charging_station_ids, charging_infrastructure = load_depot_charging_stations(depot_charging_station_file)
    # derived inputs come from results/; they are regenerated from the Excel datasets in data/
    require_input(TRIPS_CSV, 'src/hdv_trip_generation.py')
    all_trips                           = pd.read_csv(TRIPS_CSV, encoding=CSV_ENCODING)
    all_trips                           = all_trips[(all_trips['day_ID'] >= order_data_days[0]) & (all_trips['day_ID'] <= order_data_days[1])]
    if all_trips.empty:
        raise ValueError(f"No trips in day range {order_data_days} of {TRIPS_CSV.name}; adjust order_data_days.")
    all_trips['day_ID']                 = all_trips['day_ID'] - order_data_days[0] + 1
    trips_dataset_amount                = order_data_days[1] - order_data_days[0] + 1
    if isinstance(date_disposition, str):
        date_disposition                = datetime.strptime(date_disposition, '%d.%m.%Y')

    # 2.1.2 import cost parameters
    require_input(COST_PARAMETER_ENERGY_CSV, 'src/hdv_cost_parameter_generation.py')
    require_input(COST_PARAMETER_V2G_CSV, 'src/hdv_cost_parameter_generation.py')
    cost_parameters_energy              = pd.read_csv(COST_PARAMETER_ENERGY_CSV, encoding=CSV_ENCODING)
    cost_parameters_v2g                 = pd.read_csv(COST_PARAMETER_V2G_CSV, encoding=CSV_ENCODING)
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
    # Only the distribution over the day is taken from here; curve_for_year() in 2.5
    # rescales it so its mean is the scenario year's price from energy_yearly.
    if sorted(cost_parameters_v2g['Hour'].astype(int)) != list(range(1, 25)):
        raise ValueError(f"{COST_PARAMETER_V2G_CSV.name} must hold the hours 1...24, once each.")
    v2g_by_hour = cost_parameters_v2g.sort_values('Hour').reset_index(drop=True)
    v2g_channel_curves = {}
    for channel in ('arbitrage', 'flexibility'):
        for level in ('min', 'max'):
            column = f'{level}_{channel}_price_€/MWh'
            if column not in v2g_by_hour.columns:
                raise ValueError(f"{COST_PARAMETER_V2G_CSV.name} lacks the column '{column}'.")
            v2g_channel_curves[channel, level] = [float(v) for v in v2g_by_hour[column]]

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


build_runtime_context()


# 2 PREPROCESSING
# 2.1 subfunction for setup multicompute framework and parameters for iterations
def run_optimization(params):
    scenario_iterations, scenario_year_iterations, v2g_status_iteration, trips_dataset_iteration = params

    # 2.2 load trip set
    trips = all_trips[all_trips['day_ID'] == trips_dataset_iteration]

    # 2.3 load fleet - the roster of data/fleet_dataset.xlsx, used verbatim
    fleet = fleet_dataset.copy()
    fleet = apply_real_vehicle_parameters(fleet)

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
    # best case = bev energy cheap + diesel expensive, worst case = the other way round
    yearly = cost_parameters_energy.set_index('Year')
    if scenario_iterations == 'best case':
        price_band, flexibility_band = 'min', 'max'
        costs_diesel = yearly['max_public_diesel_cost_€/l']
        costs_energy_spot = yearly['min_energy_spot_price_€/kWh']
        costs_public = yearly['min_public_charging_cost_€/kWh']
    else:
        price_band, flexibility_band = 'max', 'min'
        costs_diesel = yearly['min_public_diesel_cost_€/l']
        costs_energy_spot = yearly['max_energy_spot_price_€/kWh']
        costs_public = yearly['max_public_charging_cost_€/kWh']
    # €/kWh and €/l straight from energy_dataset.xlsx - the distance conversion happens
    # per vehicle below, with that vehicle's own consumption
    energy_spot_price_eur_per_kWh = float(costs_energy_spot.loc[scenario_year_iterations])
    public_charging_cost_eur_per_kWh = float(costs_public.loc[scenario_year_iterations])
    diesel_cost_eur_per_l = float(costs_diesel.loc[scenario_year_iterations])

    # energy_daily gives the distribution over the day, energy_yearly the level: a curve
    # is that distribution rescaled so its mean over the day equals the scenario year's
    # value. The daily curve is therefore the curve of whichever year its own mean matches
    # - 2025 in the shipped data - and every other year is the same distribution at a
    # different level. No base year has to be named for this: normalising by the curve's
    # own mean and multiplying by the year's value states the rule directly.
    # For the electricity curve this also makes the switch level-neutral - its mean is
    # exactly the flat tariff the model charged before - so only the intraday shape is new.
    def curve_for_year(channel, band, yearly_eur_per_MWh):
        base = v2g_channel_curves[channel, band]
        base_mean = sum(base) / len(base)
        if base_mean <= 0:
            raise ValueError(
                f"the {band} {channel} curve in {COST_PARAMETER_V2G_CSV.name} averages "
                f"{base_mean:g} €/MWh over the day, so there is no distribution to "
                f"rescale. Give that band non-zero prices in sheet 'energy_daily'.")
        factor = yearly_eur_per_MWh / base_mean
        return [v * factor for v in base]

    # Arbitrage buys and sells the same commodity at the same meter, so it takes the same
    # band as the charging price - buying cheap in one band and selling dear in another
    # would be two different electricity markets. Its profit is the spread within the
    # curve, which is where the intraday shape has to survive.
    arbitrage_eur_per_MWh_hourly = curve_for_year(
        'arbitrage', price_band, energy_spot_price_eur_per_kWh * 1000.0)
    flexibility_eur_per_MWh_hourly = curve_for_year(
        'flexibility', flexibility_band,
        float(yearly.loc[scenario_year_iterations, f'{flexibility_band}_flexibility_price_€/MWh']))
    # 30-min steps, two per hour. These are still the *bare market* curves; the overheads
    # of 1.4 turn them into the prices the depot actually transacts at, below.
    spot_arbitrage_eur_per_MWh = [arbitrage_eur_per_MWh_hourly[t // 2] for t in time_steps]
    spot_flexibility_eur_per_MWh = [flexibility_eur_per_MWh_hourly[t // 2] for t in time_steps]
    # the electricity spot curve in €/kWh, which is what both overheads are measured from.
    # Taken before any of them is applied - it is a market price, not a price of this depot.
    energy_spot_eur_per_kWh_t = [p / 1000.0 for p in spot_arbitrage_eur_per_MWh]

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
    # flexibility curve of energy_dataset.xlsx.
    costs_v2g_flexibility = list(spot_flexibility_eur_per_MWh)
    # Nothing is floored at zero. A spot price below the selling overhead means a kWh sold
    # into arbitrage earns less than it costs to place, which is real on a negative-price
    # hour - and the model simply declines those slots, since claiming one is optional.
    # The same curve makes own PV a credit rather than a cost, so it is worth seeing.
    if show_outputs == 'on' and min(pv_charging_eur_per_kWh_t) < 0.0:
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
    if v2g_price_mode == 'arbitrage':
        costs_v2g = costs_v2g_arbitrage
    elif v2g_price_mode == 'flexibility':
        costs_v2g = costs_v2g_flexibility
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

        def leg_steps(hours):
            """Steps a leg occupies, including any Lenkzeitpause it is long enough to need.

            An empty run is driving like any other, so a 5 h repositioning leg obliges the
            same 45-minute stop a 5 h loaded trip does (2.6b).
            """
            if hours <= 0:
                return 0
            driving = max(1, math.ceil(float(hours) / STEP_HOURS))
            if fleet_operation_mode != 'crewed':
                return driving
            breaks = max(0, (driving - 1) // driving_time_before_break_steps)
            return driving + breaks * driving_break_duration_steps

        def leg_driving_steps(hours):
            return max(1, math.ceil(float(hours) / STEP_HOURS)) if hours > 0 else 0

        approach_steps = {f: leg_steps(hours) for f, (_km, hours) in day_routing.approach.items()}
        return_steps = {f: leg_steps(hours) for f, (_km, hours) in day_routing.ret.items()}
        link_steps = {pair: leg_steps(hours) for pair, (_km, hours) in day_routing.links.items()}
        # the driving inside those legs, which is what the Lenkzeit cap counts
        approach_driving_steps = {f: leg_driving_steps(hours)
                                  for f, (_km, hours) in day_routing.approach.items()}
        return_driving_steps = {f: leg_driving_steps(hours)
                                for f, (_km, hours) in day_routing.ret.items()}
        link_driving_steps = {pair: leg_driving_steps(hours)
                              for pair, (_km, hours) in day_routing.links.items()}

        if show_outputs == 'on':
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
    dropped_trips = []
    if day_routing is not None and fleet_operation_mode == 'crewed':
        shift_limit_steps = driver_max_shift_hours / STEP_HOURS
        drive_limit_steps = driver_max_driving_hours / STEP_HOURS
        for _, row in trips.iterrows():
            f = row['trip_ID']
            drive_steps = int(round(row['trip_duration_h'] * 2))
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
            raise ValueError(
                f"all {len(removed)} trip(s) of this day need more driving or more time "
                f"away than the crew limits allow ({driver_max_driving_hours:g} h Lenkzeit, "
                f"{driver_max_shift_hours:g} h Arbeitszeit) once the run out from "
                f"{home_depot_location!r} and back is counted. Either the depot is in the "
                f"wrong place for this order set, or the limits are.")
        day_trips_list = [f for f in day_trips_list if f not in removed]
        trip_distances = {f: km for f, km in trip_distances.items() if f not in removed}
        # the geography has to forget them too, or the chain graph still offers routes
        # through trips that are no longer in the day
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
        if show_outputs == 'on':
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
        drive_steps = int(round(trip_duration_h * 2))
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
    if show_outputs == 'on' and fleet_operation_mode == 'crewed':
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
    if trips_outside_work_hours and show_outputs == 'on':
        named = ', '.join(str(f) for f, _duration, _window in trips_outside_work_hours[:8])
        more = (f" (and {len(trips_outside_work_hours) - 8} more)"
                if len(trips_outside_work_hours) > 8 else '')
        print(f"working hours {work_hours_start}-{work_hours_end}: "
              f"{len(trips_outside_work_hours)} of {len(trips)} trip(s) do not fit and keep "
              f"their own window from the order data - trip {named}{more}")
    active_start_times = {
        (f, t): [s for s in possible_start_times[f] if s <= t < s + trips_duration_steps[f]]
        for f in day_trips_list for t in time_steps
    }

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

    # v43: generate explicit virtual 1-step "trips" (events) for V2G (discharge, same as real trip battery impact) and charging (negative discharge = charging into battery)
    # These use identical z_m_f_s assignment, occupation logic, and feed into unified SoC/delta/degradation as real driving trips.
    v2g_virtual_trips = []
    chg_virtual_trips = []
    virtual_trip_durations = {}
    virtual_trip_possible_starts = {}
    if use_virtual_trip_model_for_v2g_and_charging == 'on':
        # One charging event per step, not one per station. Which station a truck takes is
        # no longer a decision of the model: it plugs into whichever free station has the
        # most power (see 3.3.12), so the only thing to decide is whether it charges at
        # all. That turns 48 x stations x vehicles binaries into 48 x vehicles.
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
    #      is a reference figure at the year's average tariff; what a bev is actually
    #      billed follows the intraday curve and depends on when it charges.
    cost_vehicle_100km = {}
    for m in vehicles:
        if m in bev_vehicles:
            cost_vehicle_100km[m] = ((energy_spot_price_eur_per_kWh
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
    build_arguments = (
        vehicles, bev_vehicles, ice_vehicles, day_trips_list, locations, trips_duration_steps,
        trips_distance_per_step, possible_start_times, active_start_times, is_bev, vehicle_consumption,
        vehicle_energy_storage, vehicle_charging_power, vehicle_v2g_power, costs_v2g, cost_vehicle_100km,
        v2g_status_iteration, show_outputs, auto_sizing, penalty_vehicle_use, penalty_vehicle_id_order,
        initial_soc_fraction, charging_infrastructure, trip_distances,
        degradation_cost_efc, depot_baseline_load_kW, pv_generation_kW, depot_buy_eur_per_kWh_t,
        public_charging_cost_eur_per_kWh, driving_break_duration_steps, driving_time_before_break_steps,
        # v43 additions for MC runtime + virtual trips + advanced degrad
        event_durations, event_possible_starts, virtual_trips, v2g_virtual_trips, chg_virtual_trips,
        monte_carlo_samples_per_trip, advanced_degradation_status, degradation_distribution_penalty, soc_weight_factor,
        # tolls
        toll_rate_per_km,
        # own PV generation: how much of the depot charging it can cover, and at what price
        pv_charging_available_kWh, pv_charging_eur_per_kWh_t,
        day_routing, approach_steps, return_steps, link_steps,
        trips_driving_steps, approach_driving_steps, return_driving_steps, link_driving_steps)

    # 2.8a warm start: solve the same day once without the crew rules, then hand that
    # schedule to the constrained solve as a starting point. The relaxed answer already
    # places the trips and the routes; the second solve only has to repair it where the
    # crew rules bite, which is a much smaller search than building one from nothing.
    warm_values = None
    warm_start_used = False
    if fleet_operation_mode == 'crewed' and driver_warm_start == 'on':
        relaxed_model, _relaxed_E, _relaxed_y = model_build(*build_arguments, crew_rules=False)
        solve_model(relaxed_model, max(optimization_MIPGap, driver_warm_start_MIPGap),
                    gurobi_threads)
        if relaxed_model.SolCount > 0:
            warm_values = {v.VarName: v.X for v in relaxed_model.getVars()}
            if show_outputs == 'on':
                print(f"warm start: relaxed solve found a schedule at "
                      f"{relaxed_model.MIPGap:.1%} gap in {relaxed_model.Runtime:.0f} s; "
                      f"handing it to the crew-constrained solve")
        elif show_outputs == 'on':
            print("warm start: the relaxed solve found nothing to start from; "
                  "the constrained solve begins cold")
        relaxed_model.dispose()

    model, E_neg, y_m = model_build(*build_arguments)
    if warm_values:
        # a variable the relaxed model did not have (the crew slacks, the driving counters)
        # simply has no start value; Gurobi completes the rest itself
        for variable in model.getVars():
            value = warm_values.get(variable.VarName)
            if value is not None:
                variable.Start = value
        warm_start_used = True
    solve_model(model, optimization_MIPGap, gurobi_threads)
    results = postprocess(
        model, vehicles, vehicle_types, bev_vehicles, ice_vehicles, day_trips_list, fleet, trips,
        charging_infrastructure, show_outputs, auto_sizing, v2g_status_iteration, costs_v2g,
        penalty_charging_use, penalty_vehicle_use, penalty_vehicle_id_order, penalty_charging_external_time,
        time_steps, locations, trip_distances, possible_start_times, trips_duration_steps,
        scenario_iterations, scenario_year_iterations, E_neg, y_m, cost_vehicle_100km,
        scenario_year_iterations, depot_buy_eur_per_kWh_t, public_charging_cost_eur_per_kWh,
        # v43
        event_durations, event_possible_starts, virtual_trips, v2g_virtual_trips, chg_virtual_trips,
        # tolls (rates available as module globals too)
        toll_rate_per_km,
        # own PV generation
        pv_charging_available_kWh, pv_charging_eur_per_kWh_t,
        day_routing, approach_steps, return_steps, link_steps,
        dropped_trips, warm_start_used,
        # station ids of depot_dataset.xlsx, so the schedule names the actual charger
        charging_station_ids,
        # trips that had to be let out of the working hours
        [f for f, _duration, _window in trips_outside_work_hours],
        # which channel pays best in each step, so the earnings can be reported per channel
        v2g_channel_at_step, costs_v2g_arbitrage, costs_v2g_flexibility)

    return (results, vehicle_types)



# 3 MODELSETUP
# 3.1 subfunction for building the MILP model
def model_build(vehicles, bev_vehicles, ice_vehicles, day_trips_list, locations, trips_duration_steps, trips_distance_per_step, possible_start_times, active_start_times, is_bev, vehicle_consumption, vehicle_energy_storage, vehicle_charging_power, vehicle_v2g_power, costs_v2g, cost_vehicle_100km, v2g_status_iteration, show_outputs, auto_sizing, penalty_vehicle_use, penalty_vehicle_id_order, initial_soc_fraction, charging_infrastructure, trip_distances, degradation_cost_efc, depot_baseline_load_kW, pv_generation_kW, depot_buy_eur_per_kWh_t, public_charging_cost_eur_per_kWh, driving_break_duration_steps, driving_time_before_break_steps, event_durations, event_possible_starts, virtual_trips, v2g_virtual_trips, chg_virtual_trips, monte_carlo_samples_per_trip, advanced_degradation_status, degradation_distribution_penalty, soc_weight_factor, toll_rate_per_km, pv_charging_available_kWh, pv_charging_eur_per_kWh_t, day_routing=None, approach_steps=None, return_steps=None, link_steps=None, trips_driving_steps=None, approach_driving_steps=None, return_driving_steps=None, link_driving_steps=None, crew_rules=None):
    # 3.1.1 define mixed integer linear program and suppress license information output
    sys.stdout      = open(os.devnull, 'w')
    sys.stderr      = open(os.devnull, 'w')
    model           = gp.Model("fleet_disposition")
    sys.stdout      = sys.__stdout__
    sys.stderr      = sys.__stderr__

    # Grid charging prices per kWh, from energy_dataset.xlsx. They price grid electricity
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
        name="z_m_f_s"
    )
    x_m_t_l         = model.addVars(vehicles, time_steps, locations, vtype=gp.GRB.BINARY, name="x_m_t_l")  # only external now (if enabled)
    # v43: removed x_m_t_l_bev (was proxy for "at depot for energy"); now use dedicated v2g_z / chg_z event indicators for gating E flows (unified with trip assignment)
    y_m             = model.addVars(vehicles, vtype=gp.GRB.BINARY, name="y_m")  # 1 if vehicle m is used on any trip
    x_m_t_E         = model.addVars(bev_vehicles, time_steps, lb=-gp.GRB.INFINITY, name="x_m_t_E") #bev-only: energy flow E (kWh per step, can be positive (charging) or negative (V2G)),
    x_m_SoC         = model.addVars(bev_vehicles, time_steps, lb=0, name="x_m_SoC") #bev-only: SoC (kWh)
    E_neg           = model.addVars(bev_vehicles, time_steps, lb=0, name="E_neg") #bev-only: E_neg (kWh) = max(-E, 0)
    E_pos           = model.addVars(bev_vehicles, time_steps, lb=0, name="E_pos") #bev-only: E_posg (kWh) = max(0, E)
    # split for public (external) vs private (depot/internal) charging energy costs from cost_parameters_energy
    E_private       = model.addVars(bev_vehicles, time_steps, lb=0, name="E_private")
    E_public        = model.addVars(bev_vehicles, time_steps, lb=0, name="E_public")

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
    if chaining_on:
        approach_steps = approach_steps or {}
        return_steps = return_steps or {}
        link_steps = link_steps or {}
        last_step = time_steps[-1]

        # 3.3.16a a route boundary is an assignment that also drives a leg. Only offered
        # where the leg fits inside the day: a route cannot start before 00:00 or finish
        # after 24:00, so the approach leg has to fit before the trip and the return leg
        # after it. Where neither is possible at any start time the trip has to be chained
        # instead, which the flow balance below then forces.
        for m in vehicles:
            for f in day_trips_list:
                app = approach_steps.get(f, 0)
                ret = return_steps.get(f, 0)
                for st in possible_start_times.get(f, []):
                    if st - app >= 0:
                        route_start[m, f, st] = model.addVar(
                            vtype=gp.GRB.BINARY, name=f"route_start[{m},{f},{st}]")
                        model.addLConstr(route_start[m, f, st] <= z_m_f_s[m, f, st])
                    if st + trips_duration_steps[f] + ret <= last_step + 1:
                        route_end[m, f, st] = model.addVar(
                            vtype=gp.GRB.BINARY, name=f"route_end[{m},{f},{st}]")
                        model.addLConstr(route_end[m, f, st] <= z_m_f_s[m, f, st])
                for (pf, pg) in link_steps:
                    if pf == f:
                        chain[m, f, pg] = model.addVar(
                            vtype=gp.GRB.BINARY, name=f"chain[{m},{f},{pg}]")

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
                at_depot[m, t] = model.addVar(lb=0.0, ub=1.0, name=f"at_depot[{m},{t}]")
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
                var = model.addVar(vtype=gp.GRB.BINARY, name=f"chain_from[{m},{f},{g},{st}]")
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
    #       without needing to know where the absences are.
    #   (b) Lenkzeit - the driving inside one absence may not exceed
    #       driver_max_driving_hours. A running counter does this exactly: it accumulates
    #       driving and is forced to zero whenever the vehicle is home.
    #   (c) Lenkzeitpause - handled in 2.6b rather than here, by giving every trip and leg
    #       long enough to need one the 45 minutes it cannot avoid. What 3.3.17 then caps
    #       is the driving alone, which is why trips_driving_steps is separate from
    #       trips_duration_steps.
    #
    # And the reason waiting in a customer yard is now expensive: it is inside `away`, so
    # the objective pays a driver for it. Nothing else had ever made idleness away from
    # home cost anything.
    away = {}
    drivers_needed = None
    driver_away_steps = 0.0
    # crew_rules exists for one caller: the warm start (2.8a), which builds this same
    # model without 3.3.17 so it can hand the result over as a starting point. Every
    # other caller gets the rules, because a schedule no driver may legally run is not a
    # schedule.
    crew_rules_on = ((fleet_operation_mode == 'crewed') if crew_rules is None
                     else bool(crew_rules))
    if chaining_on and crew_rules_on:
        trips_driving_steps = trips_driving_steps or {}
        approach_driving_steps = approach_driving_steps or {}
        return_driving_steps = return_driving_steps or {}
        link_driving_steps = link_driving_steps or {}

        max_shift_steps = max(1, int(round(driver_max_shift_hours / STEP_HOURS)))
        max_drive_steps = max(1, int(round(driver_max_driving_hours / STEP_HOURS)))
        horizon = len(time_steps)

        for m in vehicles:
            for t in time_steps:
                away[m, t] = 1.0 - at_depot[m, t]

        # Both limits are enforced with a slack that costs penalty_crew_rule_breach per
        # half-hour of excess. That is not softness for its own sake: on real order data
        # some trips *cannot* be crewed legally from one depot at all - day 2 has five
        # whose approach alone is 6.5 h, so depot -> trip -> depot is 13 h of absence and
        # 11 h of driving before anything else is scheduled. A hard constraint turns those
        # into a bare "infeasible" that names nothing; a priced one keeps the rest of the
        # day legal, forces the breach to be as small as possible, and reports exactly
        # where the law had to give. Set the penalty high enough and it behaves as a hard
        # rule wherever a hard rule is satisfiable.
        shift_excess = {}
        drive_excess = {}

        # (a) no absence longer than a working day
        if max_shift_steps < horizon:
            window = max_shift_steps + 1
            for m in vehicles:
                for start in range(0, horizon - window + 1):
                    slack = model.addVar(lb=0.0, name=f"shift_excess[{m},{start}]")
                    shift_excess[m, start] = slack
                    model.addLConstr(
                        gp.quicksum(at_depot[m, t] for t in range(start, start + window))
                        + slack >= 1)

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
                                       name=f"drive_since_depot[{m},{t}]")
                over = model.addVar(lb=0.0, name=f"drive_excess[{m},{t}]")
                drive_excess[m, t] = over
                model.addLConstr(counter <= previous + driven_now)
                model.addLConstr(counter >= previous + driven_now
                                 - big_m_steps * at_depot[m, t])
                model.addLConstr(counter <= big_m_steps * (1.0 - at_depot[m, t]))
                model.addLConstr(counter <= max_drive_steps + over)
                previous = counter

        # how many drivers the day needs at its busiest: every vehicle away at the same
        # moment is a driver of its own, and no roster can do better than that peak
        drivers_needed = model.addVar(lb=0.0, name="drivers_needed")
        for t in time_steps:
            model.addLConstr(drivers_needed >= gp.quicksum(away[m, t] for m in vehicles))

        driver_away_steps = gp.quicksum(away[m, t] for m in vehicles for t in time_steps)
        # the excess is measured per half-hour step, so the penalty is per half-hour of
        # illegal driving or of a shift run long
        crew_breach_steps = (gp.quicksum(shift_excess.values())
                             + gp.quicksum(drive_excess.values()))

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
            model.addLConstr(x_m_SoC[m, t] >= 0)

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

    # 3.3.10 vehicle usage activation: if any event is assigned to vehicle m, y_m must be 1
    # Over all_events_for_z, not just the real trips. y_m is only ever pushed up here, and
    # both objective terms carrying it push down, so a truck the narrow loop missed settled
    # on y_m = 0: a truck parked on a charger all day, selling energy back, counted as
    # unused. It reported one used bev beside three occupied chargers and 17 MWh charged,
    # it escaped penalty_vehicle_use that a driving truck pays, and under auto_sizing - where
    # y_m *is* the fleet-size objective - it was invisible to the sizing decision while
    # v2g_earnings_used_total dropped its earnings from the report. A truck on a charger
    # occupies a bay, ages, and had to be bought, so it is in service.
    for m in vehicles:
        for f in all_events_for_z:
            for s in event_possible_starts.get(f, possible_start_times.get(f, [])):
                model.addLConstr(y_m[m] >= z_m_f_s[m, f, s])

    # 3.3.11 power and energy limits per step for bev  (v43: fully updated for virtual trip model)
    # x_m_t_E positive only when charging "trip" (CHG or ext), negative only when V2G "trip" assigned (same as real trip activation).
    # Gating uses z of V2G_t{t} / CHG_t{t} instead of old x locs / x_l_bev.
    # the best a single plugged-in truck can be offered, since it takes the strongest free
    # station; what several of them can draw together is bounded in 3.3.12
    strongest_station_kW = max(charging_infrastructure)

    # Depot charging is not throttled: a plugged-in truck draws the lowest of its own
    # charging power, the station's, and what the battery can still take - and never less
    # (3.3.11b). That is stated per truck against the strongest station, so it only agrees
    # with the fleet-level greedy bound of 3.3.12 while every station a truck can occupy
    # is at least as strong as the truck. Where a truck would land on a weaker station its
    # full power is that station's, which the greedy rule does not say who gets - so the
    # two would contradict and the model would come back infeasible with no hint why.
    stations_desc_check = sorted(charging_infrastructure, reverse=True)
    occupiable = min(len(stations_desc_check), len(bev_vehicles))
    strongest_truck_kW = max((vehicle_charging_power[m] for m in bev_vehicles), default=0.0)
    weak_stations = [s for s in stations_desc_check[:occupiable] if s < strongest_truck_kW]
    if weak_stations:
        raise ValueError(
            f"full-power depot charging needs the {occupiable} strongest stations to each "
            f"be at least as strong as the strongest bev ({strongest_truck_kW:g} kW), "
            f"because a truck on a weaker station charges at that station's power and the "
            f"greedy rule does not fix which truck that is. "
            f"{len(weak_stations)} of them fall short: "
            f"{[float(s) for s in weak_stations]} kW in the 'charging' sheet of "
            f"depot_dataset.xlsx. Raise those stations, remove them, or reduce the bev "
            f"count to {sum(1 for s in stations_desc_check if s >= strongest_truck_kW)}.")
    for m in bev_vehicles:
        P_ch = vehicle_charging_power[m]  # kW
        E_step_ch_max = P_ch * 0.5  # kWh per 30-min step
        P_v2g = vehicle_v2g_power[m]  # kW for V2G limit
        E_step_v2g_max = P_v2g * 0.5  # kWh per 30-min step
        
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
            # the discharge a step can deliver is the inverter power over half an hour.
            # How deep the battery may be drawn is not a per-step matter: the SoC balance
            # and the 24:00 target already bound that.
            E_v2g_effective = E_step_v2g_max
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
            if t == 0:
                # the day-start level is a constant, so the minimum is one too
                headroom_0 = (1 - initial_soc_fraction) * vehicle_energy_storage[m]
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
                full_binds = model.addVar(vtype=gp.GRB.BINARY, name=f"chg_full_{m}_{t}")
                model.addLConstr(
                    E_private[m, t] >= best_station_e * z_chg - m_station * full_binds)
                model.addLConstr(
                    E_private[m, t] >= battery_factor * headroom
                                       - m_battery * (1 - full_binds)
                                       - m_battery * (1 - z_chg))
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

    # 3.3.11b external charging only for bev (kept for external loc x)
    if 'external_charging' in locations:
        for m in vehicles:
            for t in time_steps:
                model.addLConstr(x_m_t_l[m, t, 'external_charging'] <= is_bev[m])

    # 3.3.12 the depot's stations, handed out strongest-first
    #
    # Which station a truck gets is not optimised. A truck that plugs in takes whichever
    # free station has the most power, so with k trucks plugged in at once the fleet
    # occupies the k strongest stations - a fact of the rule, not a decision. Two
    # constraints per step express it:
    #
    #   (a) no more trucks plugged in than there are stations
    #   (b) the energy they draw together stays within the k strongest stations
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
    stations_desc = sorted(charging_infrastructure, reverse=True)
    truck_powers_desc = sorted((vehicle_charging_power[m] for m in bev_vehicles), reverse=True)
    pairable = min(len(stations_desc), len(truck_powers_desc))
    pair_kW = [min(truck_powers_desc[i], stations_desc[i]) for i in range(pairable)]
    cumulative_kW = [0.0]
    for power in pair_kW:
        cumulative_kW.append(cumulative_kW[-1] + power)

    for t in time_steps:
        plugged_in = gp.quicksum(z_m_f_s[m, f'CHG_t{t}', t] for m in bev_vehicles)
        # (a) a station can hold one truck, so the number of stations is the ceiling
        model.addLConstr(plugged_in <= pairable)
        # (b) k trucks share the k strongest stations
        depot_draw = gp.quicksum(E_private[m, t] for m in bev_vehicles)
        for j in range(pairable):
            model.addLConstr(
                depot_draw <= (cumulative_kW[j] + pair_kW[j] * (plugged_in - j)) * STEP_HOURS)

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
    site_import = {}
    site_peak_kW = model.addVar(lb=0, ub=site_peak_limit_kW, name="site_peak_kW")
    for t in time_steps:
        site_import[t] = model.addVar(lb=0, name=f"site_import_{t}")
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
    # is bought from the grid at energy_spot_price_€/kWh.
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
        E_pv_charging[t] = model.addVar(lb=0, ub=surplus_kWh, name=f"E_pv_charging_{t}")
        if surplus_kWh <= 0:
            continue
        depot_charging_kWh = gp.quicksum(E_private[m, t] for m in bev_vehicles)
        model.addLConstr(E_pv_charging[t] <= depot_charging_kWh)
        # ... and equal to the smaller of the two: surplus_binding = 1 makes the surplus
        # the binding side (the trucks take all of it), 0 the charging demand
        surplus_binding = model.addVar(vtype=gp.GRB.BINARY, name=f"pv_surplus_binding_{t}")
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
    # This is a correction as much as a feature. 3.3.15 already nets the discharge against
    # the charging when it computes the site's grid draw - so the *peak* was right - while
    # 3.4 went on billing the gross charging at the buy price and crediting the gross
    # discharge at the sell price. The depot was paying import fees on kWh its meter never
    # saw.
    #
    # No binary is needed for the min(): the objective pays for E_v2v, so it pushes the
    # variable up against whichever of the two bounds is tighter.
    E_v2v = {}
    v2v_saving_eur_per_kWh = (grid_energy_overhead_eur_per_kWh
                              + energy_selling_overhead_eur_per_kWh)
    for t in time_steps:
        E_v2v[t] = model.addVar(lb=0.0, name=f"E_v2v_{t}")
        if v2v_status != 'on':
            model.addLConstr(E_v2v[t] == 0.0)
            continue
        depot_charging_kWh = gp.quicksum(E_private[m, t] for m in bev_vehicles)
        depot_discharge_kWh = gp.quicksum(E_neg[m, t] for m in bev_vehicles)
        model.addLConstr(E_v2v[t] <= depot_discharge_kWh)
        # ... and it competes with the sun for the same charging demand. Own PV and a
        # neighbouring truck are both local supply and both save the same fees, so which
        # of them feeds a given kWh does not change the bill - but counting a kWh as fed
        # by both would credit the saving twice for one delivery.
        model.addLConstr(E_v2v[t] + E_pv_charging[t] <= depot_charging_kWh)

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
    v2g_earnings = gp.quicksum(-(costs_v2g[t] / 1000.0) * E_neg[m, t]
        for m in bev_vehicles for t in time_steps)
    # vehicle-to-vehicle (3.3.15c). Those kWh are billed above as if they had been bought
    # at the buy price and sold at the sell price; neither happened, so both overheads come
    # back. The spot price itself is in both and cancels, which is why the saving is the
    # two overheads and does not depend on the market - the same arithmetic that makes own
    # PV worth what it is worth.
    #
    # It creates no incentive to shuffle energy for its own sake: a kWh moved from one
    # truck to another still loses both conversions, and restoring the sending truck costs
    # that loss at the full buy price. The saving is only ever worth having when the
    # discharge and the charge were both worth doing anyway.
    v2v_saving = -v2v_saving_eur_per_kWh * gp.quicksum(E_v2v[t] for t in time_steps)
    degradation_cost = 0
    max_efc = None
    if degradation_cost_status == 'on':
        # Degradation is charged on V2G discharge only (E_neg). Driving and ordinary
        # charging age the battery too, but that wear is an unavoidable consequence of
        # operating the truck and is not attributable to the V2G decision, so pricing it
        # here would only add a constant-ish offset that biases the V2G business case.
        # EFC keeps the usual definition throughput / (2 * capacity), with the
        # throughput narrowed to the discharged energy.
        # + distribution via max_efc penalty (to spread aging, not concentrate on few vehicles)
        degrad_expr = 0
        efc_m_vars = {}
        # the tangents are the same for every vehicle and step - the weight is a function
        # of the SoC *fraction*, so only the division by capacity below is per vehicle
        soc_weight_tangents = soc_aging_weight_tangents()
        if advanced_degradation_status == 'on':
            max_efc = model.addVar(lb=0, name="max_efc_degrad")
        for m in bev_vehicles:
            cap = vehicle_energy_storage[m]
            efc_base = degradation_cost_efc[m]
            # per-vehicle efc for the distribution penalty. This one stays on the full
            # throughput: it spreads *physical* wear evenly over the fleet, which happens
            # whether or not the wear is charged for.
            if advanced_degradation_status == 'on':
                efc_m = model.addVar(lb=0, name=f"efc_{m}")
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
                # soc_weight_factor == 0 means every kWh ages the battery alike, and then
                # the weight is the constant 1.0. Taken as a scalar rather than as a
                # variable pinned to 1.0, because E_neg * variable is a product of two
                # variables however tightly the second is bounded: this is what keeps the
                # objective linear, and the whole model a MILP, when the weighting is off.
                # --- benchmark container m_lindegrad: the same weighting, linearized ---
                if t == 0 or soc_weight_factor == 0 or v2g_status_iteration != 'on':
                    soc_w = soc_aging_weight(initial_soc_fraction if t == 0 else 0.5)
                    weighted_thp = throughput * soc_w
                else:
                    edges = [k / LINDEGRAD_BINS for k in range(LINDEGRAD_BINS + 1)]
                    pick = model.addVars(LINDEGRAD_BINS, vtype=gp.GRB.BINARY,
                                         name=f"soc_bin_{m}_{t}")
                    share = model.addVars(LINDEGRAD_BINS, lb=0.0,
                                          name=f"e_neg_bin_{m}_{t}")
                    model.addLConstr(pick.sum() == 1)
                    # the step starts inside exactly one bin
                    start_fraction = x_m_SoC[m, t-1] / cap
                    model.addLConstr(start_fraction >= gp.quicksum(
                        edges[k] * pick[k] for k in range(LINDEGRAD_BINS)))
                    model.addLConstr(start_fraction <= gp.quicksum(
                        edges[k+1] * pick[k] for k in range(LINDEGRAD_BINS)))
                    # ... and all of the discharge is booked against that bin
                    discharge_cap = vehicle_v2g_power[m] * STEP_HOURS
                    model.addLConstr(share.sum() == E_neg[m, t])
                    for k in range(LINDEGRAD_BINS):
                        model.addLConstr(share[k] <= discharge_cap * pick[k])
                    weighted_thp = gp.quicksum(
                        soc_aging_weight(0.5 * (edges[k] + edges[k+1])) * share[k]
                        for k in range(LINDEGRAD_BINS)) / discharging_efficiency
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
    external_time_penalty = 0
    if 'external_charging' in locations and external_charging_status == 'on':
        break_active = model.addVars(bev_vehicles, time_steps, vtype=gp.GRB.BINARY, name="break_active")
        penalized_external = model.addVars(bev_vehicles, time_steps, vtype=gp.GRB.BINARY, name="penalized_external")
        # A Lenkzeitpause is only due once the driver has actually driven long enough for
        # one. The window used to open after *every* trip however short, which handed the
        # fleet free public charging after a 30-minute run - the break was the reason the
        # penalty is waived, and there was no break. A trip earns the window only if its
        # own driving time reaches driving_time_before_break_minutes.
        # Simplification worth knowing: the test is per trip, not on driving accumulated
        # across trips. Three two-hour trips are six hours of driving and would oblige a
        # break in reality, but none of them reaches the threshold on its own, so none
        # earns the window here. Tracking accumulated driving needs a per-vehicle counter
        # that breaks reset, which is a scheduling problem of its own.
        # sparse precompute: only add break_active links for valid (m,t,f,s) tuples
        break_links = {}
        for f in day_trips_list:
            if trips_duration_steps[f] < driving_time_before_break_steps:
                continue
            trip_end_offset = trips_duration_steps[f] - 1
            for s in possible_start_times[f]:
                trip_end = s + trip_end_offset
                for t in range(trip_end + 1,
                               min(trip_end + 1 + driving_break_duration_steps, time_steps[-1] + 1)):
                    if t in time_steps:
                        for m in bev_vehicles:
                            break_links.setdefault((m, t), []).append(z_m_f_s[m, f, s])
        # break_active has to be pinned from both sides. It only ever *relieves* a penalty
        # and carries no cost of its own, so with the lower bounds alone the solver simply
        # set it to 1 everywhere and the whole external-time penalty fell away - which is
        # why runs charged freely at public stations while nominally paying 10 €/min for
        # it. The upper bound makes it a real disjunction: the window is open at t exactly
        # when some qualifying trip assignment opens it, and closed otherwise.
        for (m, t), z_vars in break_links.items():
            for z_var in z_vars:
                model.addLConstr(break_active[m, t] >= z_var)
            model.addLConstr(break_active[m, t] <= gp.quicksum(z_vars))
        for m in bev_vehicles:
            for t in time_steps:
                if (m, t) not in break_links:
                    model.addLConstr(break_active[m, t] == 0)
        for m in bev_vehicles:
            for t in time_steps:
                model.addLConstr(penalized_external[m, t] <= x_m_t_l[m, t, 'external_charging'])
                model.addLConstr(penalized_external[m, t] <= 1 - break_active[m, t])
                model.addLConstr(penalized_external[m, t] >= x_m_t_l[m, t, 'external_charging'] - break_active[m, t])
        external_time_penalty = gp.quicksum(
            penalty_charging_external_time * 30 * penalized_external[m, t]
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
    vehicle_use_penalty = penalty_vehicle_use * gp.quicksum(y_m[m] for m in vehicles)
    vehicle_id_penalty = gp.quicksum(penalty_vehicle_id_order * (m - 1) * y_m[m] for m in vehicles)
    # actual charging energy costs (private depot vs public external) using E split + rates derived from cost_parameters_energy
    charging_energy_cost = gp.quicksum(
        depot_buy_eur_per_kWh_t[t] * E_private[m, t] + public_charging_eur_per_kwh.get(m, 0.0) * E_public[m, t]
        for m in bev_vehicles for t in time_steps)
    # ... and the correction for the part of the depot charging the site generates itself:
    # those kWh never pass the grid meter, so they are re-priced from the spot price of
    # energy_dataset.xlsx to what an own PV kWh is worth. The spot price is the same for
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
    # drivers (1.4c, 3.3.17). Two terms, because the operator pays twice over:
    #
    #   hours - every step a vehicle is away from home is a step a driver is paid for,
    #           whether the wheels turn or not. This is what prices waiting in a customer
    #           yard, which nothing had ever charged for before, and it is why the model
    #           now prefers to bring a truck home between jobs rather than park it out.
    #   heads - a tie-break towards fewer, longer shifts instead of many short ones, on the
    #           day's peak concurrency. Priced like penalty_vehicle_use rather than as a
    #           wage: the hours above already pay the wage, and charging a full day per
    #           driver on top would double-count.
    driver_cost = 0
    if drivers_needed is not None:
        driver_cost = (driver_hourly_rate_eur * STEP_HOURS * driver_away_steps
                       + penalty_driver_use * drivers_needed
                       + penalty_crew_rule_breach * crew_breach_steps)
    total_cost = driving_cost + v2g_earnings + v2v_saving + degradation_cost + peak_shaving_cost + external_time_penalty + charging_penalty + vehicle_use_penalty + charging_energy_cost + toll_cost + driver_cost
    if advanced_degradation_status == 'on' and max_efc is not None and degradation_cost_status == 'on':
        total_cost += degradation_distribution_penalty * max_efc   # v43: encourages distribution of degradation across vehicles
    # The vehicle-id tie-break used to be conditioned on show_outputs, which meant asking for
    # plots changed the objective and could return a different schedule than the same run
    # without them. It is a readability device, so it is applied whenever the fleet is fixed.
    # auto_sizing stays exempt for a real reason: the roster is the result there, and a bias
    # towards low ids would distort which vehicles the run selects.
    if auto_sizing == 'on':
        model.setObjective(total_cost, gp.GRB.MINIMIZE)
    else:
        model.setObjective(total_cost + vehicle_id_penalty, gp.GRB.MINIMIZE)

    return (model, E_neg, y_m)



# 4 OPTIMIZATION
# 4.1 optimization subfunction
def solve_model(model, optimization_MIPGap, gurobi_threads):
    # 4.2 set model parameters
    model.setParam('OutputFlag', 0)
    model.setParam('LogToConsole', 0)
    model.setParam('MIPGap', optimization_MIPGap)
    model.setParam('Threads', gurobi_threads)
    # No wall-clock cap by default: the solver runs until it meets optimization_MIPGap.
    # Set explicitly rather than left to Gurobi's default so the intent is on the page -
    # a stopped solve and a converged one are not the same result, and a time limit that
    # is not written down anywhere is the kind of thing that silently decides a study.
    model.setParam('TimeLimit', gp.GRB.INFINITY if optimization_time_limit_s is None
                   else float(optimization_time_limit_s))
    model.setParam('Presolve', -1)  # -1 = auto, 0 = off, 1 = conservative, 2 = aggressive
    model.setParam('Aggregate', 1)  # 0 = off, 1 = moderate, 2 = aggressive
    model.setParam('Cuts', -1)  # -1 = auto, 0 = off, 1 = conservative, 2 = aggressive, 3 = very aggressive
    model.setParam('Method', -1)  # -1 = auto, 0 = primal simplex, 1 = dual simplex, 2 = barrier with crossover, 3 = concurrent, 4 = deterministic concurrent
    # Under the crew rules (3.3.17) the hard part stops being the last few percent of the
    # gap and becomes finding any feasible schedule at all: the driving counters couple
    # every step of a vehicle to every earlier one, which leaves the LP relaxation a poor
    # guide. MIPFocus 1 tells Gurobi to spend its effort on incumbents rather than on the
    # bound, which is what a run in that state needs. It suits the warm start's relaxed
    # solve too - that one only has to produce a schedule worth starting from, quickly.
    if fleet_operation_mode == 'crewed':
        model.setParam('MIPFocus', 1)
    
    # 4.3 solve the MILP model
    model.optimize()

    # 4.4 model tuning
    #model.write(str(project_path('results', 'fleet_disposition_tuning_model.lp')))
    #model.tune()
    #model.getTuneResult(0)
    #model.write(str(project_path('results', 'fleet_disposition_tuning_model_tuneresults.prm')))



# 5 POSTPROCESSING
# 5.1 subfunction for postprocessing
def postprocess(model, vehicles, vehicle_types, bev_vehicles, ice_vehicles, day_trips_list, fleet, trips, charging_infrastructure, show_outputs, auto_sizing, v2g_status_iteration, costs_v2g, penalty_charging_use, penalty_vehicle_use, penalty_vehicle_id_order, penalty_charging_external_time, time_steps, locations, trip_distances, possible_start_times, trips_duration_steps, scenario_iterations, scenario_year_iterations, E_neg, y_m, cost_vehicle_100km, year_iteration, depot_buy_eur_per_kWh_t, public_charging_cost_eur_per_kWh, event_durations, event_possible_starts, virtual_trips, v2g_virtual_trips, chg_virtual_trips, toll_rate_per_km, pv_charging_available_kWh=None, pv_charging_eur_per_kWh_t=None, day_routing=None, approach_steps=None, return_steps=None, link_steps=None, dropped_trips=None, warm_start_used=False, charging_station_ids=None, trips_outside_work_hours=None, v2g_channel_at_step=None, costs_v2g_arbitrage=None, costs_v2g_flexibility=None):
    # the station list is positional in the model (station index si); its charger_id from
    # depot_dataset.xlsx is what a reader of the schedule recognises
    def station_label(index):
        if charging_station_ids and index < len(charging_station_ids):
            return charging_station_ids[index]
        return str(index)

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
        """
        drawing = []
        for veh in bev_vehicles:
            energy = model.getVarByName(f"E_private[{veh},{t}]").X
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

    if model.status == gp.GRB.OPTIMAL:
        # 5.2 disposition schedule plot
        # 5.2.1 create disposition schedule dataframe
        schedule_rows = []
        # v43: real trips only for "trip" rows
        assigned_trip_starts = [
            (m, f, s)
            for m in vehicles
            for f in day_trips_list
            for s in event_possible_starts.get(f, [])
            if model.getVarByName(f"z_m_f_s[{m},{f},{s}]").X >= 0.5
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
                        started = model.getVarByName(f"route_start[{m},{f},{s}]")
                        steps = approach_steps.get(f, 0)
                        if started is not None and started.X > 0.5 and steps > 0:
                            deadhead_rows.append((m, s - steps, steps, 'approach', f, None))
                        ended = model.getVarByName(f"route_end[{m},{f},{s}]")
                        steps = return_steps.get(f, 0)
                        if ended is not None and ended.X > 0.5 and steps > 0:
                            deadhead_rows.append(
                                (m, s + trips_duration_steps[f], steps, 'return', f, None))
                        for (pf, pg), leg_steps_count in link_steps.items():
                            if pf != f or leg_steps_count <= 0:
                                continue
                            linked = model.getVarByName(f"chain_from[{m},{f},{pg},{s}]")
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
                    private = model.getVarByName(f"E_private[{m},{t}]").X
                    public = model.getVarByName(f"E_public[{m},{t}]").X
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
                    model.getVarByName(f"z_m_f_s[{m},{f},{s}]").X >= 0.5
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
                    discharged = model.getVarByName(f"E_neg[{m},{t}]").X
                    charged = model.getVarByName(f"E_pos[{m},{t}]").X

                # check V2G virtual trip at this exact slot
                activity = 'parking'
                loc = 'parking_lot'
                vtg = f'V2G_t{t}'
                if vtg in v2g_virtual_trips and discharged > ENERGY_TOLERANCE_KWH:
                    zv = model.getVarByName(f"z_m_f_s[{m},{vtg},{t}]")
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
                    zc = model.getVarByName(f"z_m_f_s[{m},CHG_t{t},{t}]")
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
                    extv = model.getVarByName(f"x_m_t_l[{m},{t},external_charging]")
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
                home = model.getVarByName(f"at_depot[{m},{t}]")
                idle = 'parking' if home is None or home.X > 0.5 else 'standby_away'
                schedule_rows.append({'vehicle': m, 'time_step': t, 'activity': idle, 'location': loc, 'trip_ID': None})
        schedule_df = pd.DataFrame(schedule_rows)

        # 5.2.2 group consecutive time slots with same activity for plot
        if show_outputs == 'on':
            grouped_schedule = []
            for vehicle in vehicles:
                vehicle_data = schedule_df[schedule_df['vehicle'] == vehicle].sort_values('time_step')
                if vehicle_data.empty:
                    continue
                current_start = vehicle_data.iloc[0]['time_step']
                current_activity = vehicle_data.iloc[0]['activity']
                current_trip_ID = vehicle_data.iloc[0].get('trip_ID', None)
                current_location = vehicle_data.iloc[0].get('location', None)
                for i in range(1, len(vehicle_data)):
                    row = vehicle_data.iloc[i]
                    step = row['time_step']
                    activity = row['activity']
                    trip_ID = row.get('trip_ID', None)
                    location = row.get('location', None)
                    prev_step = vehicle_data.iloc[i-1]['time_step']
                    if step != prev_step + 1 or activity != current_activity or \
                       (current_activity in ('trip', 'deadhead') and trip_ID != current_trip_ID) or \
                       (current_activity in ['parking', 'standby_away', 'charging', 'v2g', 'charging_in', 'v2g_charge', 'v2g_discharge', 'external_charging'] and location != current_location):
                        grouped_schedule.append({
                            'vehicle': vehicle,
                            'start_step': current_start,
                            'end_step': prev_step,
                            'activity': current_activity,
                            'trip_ID': current_trip_ID,
                            'location': current_location
                        })
                        current_start = step
                        current_activity = activity
                        current_trip_ID = trip_ID
                        current_location = location
                grouped_schedule.append({
                    'vehicle': vehicle,
                    'start_step': current_start,
                    'end_step': vehicle_data.iloc[-1]['time_step'],
                    'activity': current_activity,
                    'trip_ID': current_trip_ID,
                    'location': current_location
                })

            # 5.2.3 create disposition schedule plot
            grouped_df = pd.DataFrame(grouped_schedule)
            plt, Patch = _pyplot()
            fig1, ax1 = plt.subplots(figsize=(15, 8))
            colors = {
                'trip': 'black', 'charging_in': 'tab:green', 'v2g_discharge': 'tab:red',
                'external_charging': 'tab:orange', 'parking': 'lightgrey',
                'charging': 'tab:green', 'v2g': 'tab:red',
                # empty running: the same blue as a loaded trip because it is the same
                # driving, lightened and hatched because it carries nothing
                # both halves of a V2G round trip read as V2G, but they are not the same
                # event: the lighter red buys, the strong red sells. Hatch alone was not
                # enough to tell them apart at a glance in a full day's figure.
                'v2g_charge': 'lightcoral',
                'deadhead': 'lightsteelblue',
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

            for idx, vehicle in enumerate(reversed(vehicles)):
                vehicle_data = grouped_df[grouped_df['vehicle'] == vehicle]
                for _, row in vehicle_data.iterrows():
                    start_step = row['start_step']
                    end_step = row['end_step']
                    activity = row['activity']
                    trip_ID = row['trip_ID']
                    width = end_step - start_step + 1
                    ax1.barh(idx, width=width, left=start_step, color=colors.get(activity, 'lightgrey'), hatch=hatches.get(activity, ''))
                    if activity == 'trip' and trip_ID is not None:
                        text_x = start_step + width / 2
                        ax1.text(text_x, idx, f"T{str(int(trip_ID))}", ha='center', va='center', color='white', fontsize=12)
                    elif activity == 'deadhead' and trip_ID is not None and width >= 2:
                        # '>7' approaching trip 7, '7>' returning from it, '7>9' the run
                        # between two chained trips. Only where the bar is wide enough to
                        # hold the text without overprinting its neighbours.
                        ax1.text(start_step + width / 2, idx, str(trip_ID),
                                 ha='center', va='center', color='dimgrey', fontsize=9)

            ax1.set_yticks(range(len(vehicles)))
            y_labels = [f"{v} ({t})" for v, t in zip(reversed(vehicles), reversed(vehicle_types))]
            ax1.set_yticklabels(y_labels)
            ax1.set_xlabel('Time [hh:mm]')
            ax1.set_ylabel('Vehicle ID (Type)')

            # The figure always spans the whole day, 00:00 to 24:00, whatever the working
            # hours are. Charging and V2G are not bound by them, and a trip that does not
            # fit inside them may run outside them too (see 2.6), so any narrower frame
            # would hide activity and suggest an idle fleet where there is none.
            tick_positions = list(range(0, len(time_steps), 4)) + [len(time_steps)]
            ax1.set_xticks(tick_positions)
            ax1.set_xticklabels([step_to_time(step) for step in tick_positions])
            ax1.set_xlim(0, len(time_steps))
            ax1.set_title('Fleet disposition with single vehicle activity')
            legend_elements = [
                Patch(facecolor=colors['trip'], label='driving (loaded)'),
                Patch(facecolor=colors['deadhead'], hatch=chr(92) * 2,
                      label='driving (empty)'),
                Patch(facecolor=colors['charging_in'], hatch='//', label='charging (in)'),
                Patch(facecolor=colors['v2g_charge'], hatch='//', label='V2G arbitrage (in)'),
                Patch(facecolor=colors['v2g_discharge'], hatch='xx', label='V2G (out)'),
                Patch(facecolor=colors['external_charging'], hatch='..', label='external charging'),
                Patch(facecolor=colors['parking'], label='parking (depot)'),
                Patch(facecolor=colors['standby_away'], hatch='..',
                      label='waiting (away)'),
            ]
            ax1.legend(handles=legend_elements, loc='upper right')
            plt.tight_layout()
            plt.savefig(project_path('results', 'disposition_optimized_plot.png'), dpi=FIGURE_DPI)
            plt.close(fig1)

            # ... and the same day as a grid (src/hdv_grid_plots.py). The bars above are
            # read along a row, one truck at a time; the grid is read down a column, which
            # is the only way to see what the whole fleet was doing at 13:00 at once.
            #
            # The colour is the coarse state and carries the same meaning it carries in the
            # bars above, so the two figures cannot disagree. The two characters in the cell
            # carry what the colour deliberately leaves out: which trip, and for a V2G step
            # which channel it settled in and which way the energy went.
            import hdv_grid_plots as grid_plots

            disposition_palette = {
                'trip': (colors['trip'], 'driving (loaded) - cell: trip no.'),
                'deadhead': (colors['deadhead'], 'driving (empty) - >n to trip n, n> home'),
                'charging_in': (colors['charging_in'], 'charging (C)'),
                'v2g_charge': (colors['v2g_charge'], 'V2G arbitrage buy (AC)'),
                'v2g_discharge': (colors['v2g_discharge'],
                                  'V2G sell - AD arbitrage, FD flexibility'),
                'external_charging': (colors['external_charging'],
                                      'external charging (EC)'),
                'parking': (colors['parking'], 'parking at the depot (P)'),
                'standby_away': (colors['standby_away'], 'waiting away from home (W)'),
            }
            plain_codes = {'charging_in': 'C', 'v2g_charge': 'AC',
                           'external_charging': 'EC', 'parking': 'P', 'standby_away': 'W'}

            def cell_code(activity, location, trip_id):
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
                                                 entry.trip_ID)

            grid_plots.grid_categories(
                states, cell_text,
                [f"{m} ({vehicle_types[i]})" for i, m in enumerate(vehicles)],
                project_path('results', 'disposition_optimized_plot_grid.png'),
                palette=disposition_palette, step_hours=STEP_HOURS,
                title='Fleet disposition - vehicle activity per 30-min step',
                row_axis_label='Vehicle (type)', plt=plt)

        # 5.3 calculation of total-km, bev-km, ice-km driven, amount of needed vehicles
        total_bev_km = sum(
            trip_distances[f]
            for m, f, _ in assigned_trip_starts if m in bev_vehicles
        )
        total_km = sum(trip_distances.values())
        electrified_km_percentage = (total_bev_km / total_km) * 100 if total_km > 0 else 0
        used_ice = sum(1 for m in ice_vehicles if model.getVarByName(f"y_m[{m}]").X >= 0.5)
        used_bev = sum(1 for m in bev_vehicles if model.getVarByName(f"y_m[{m}]").X >= 0.5)
        used_bev_ids = [m for m in bev_vehicles if model.getVarByName(f"y_m[{m}]").X >= 0.5]
        used_bev_battery = fleet.loc[fleet['vehicle_id'].isin(used_bev_ids), 'vehicle_energy_storage'].astype(float).tolist()

        # 5.4 idle time, V2G-time, and V2G-cost calculation, V2G duration = steps with bev, E_neg>0, and "parking"
        idle_times = []
        for m in vehicles:
            if m in bev_vehicles:
                v2g_steps = [t for t in time_steps if E_neg[m, t].X > 1e-6]
                hours = (len(v2g_steps) * 30) / 60.0
            else:
                hours = 0.0
            idle_times.append(hours)

        v2g_earnings_all = [] # for full fleet V2G utilisation
        for m in vehicles:
            if m in bev_vehicles:
                v2g_earnings_all_single = sum((costs_v2g[t] / 1000.0) * E_neg[m, t].X for t in time_steps)
            else:
                v2g_earnings_all_single = 0.0
            v2g_earnings_all.append(v2g_earnings_all_single)
        v2g_earnings_all_total = float(sum(v2g_earnings_all))
        
        v2g_earnings_used = [] # for auto fleet sizing
        for m in vehicles:
            if m in used_bev_ids:
                v2g_earnings_used_single = sum((costs_v2g[t] / 1000.0) * E_neg[m, t].X for t in time_steps)
            else:
                v2g_earnings_used_single = 0.0
            v2g_earnings_used.append(v2g_earnings_used_single)
        v2g_earnings_used_total = float(sum(v2g_earnings_used))

        # which channel each kWh was actually sold into. costs_v2g already carries the
        # better of the two per step, so the split is a matter of attributing the earnings
        # that were booked, not of re-pricing them.
        v2g_earnings_by_channel = {'arbitrage': 0.0, 'flexibility': 0.0}
        v2g_energy_by_channel = {'arbitrage': 0.0, 'flexibility': 0.0}
        if v2g_channel_at_step is not None:
            for t in time_steps:
                discharged = sum(E_neg[m, t].X for m in bev_vehicles)
                if discharged <= ENERGY_TOLERANCE_KWH:
                    continue
                channel = v2g_channel_at_step[t]
                v2g_energy_by_channel[channel] += discharged
                v2g_earnings_by_channel[channel] += (costs_v2g[t] / 1000.0) * discharged


        # 5.5 total energy costs calculation (now actual: ice fuel + bev private/public charging energy from cost_parameters_energy)
        optimization_status     = 'optimal'
        # vehicle-steps in which a bev actually took energy, depot or external. Counted on
        # E_pos (= E_private + E_public) rather than on the CHG/external assignments: an
        # assignment only permits a flow, so counting those overstates the charging exactly
        # as it used to overstate it in the schedule (see 5.2.2).
        total_charging_steps    = sum(
            1 for m in bev_vehicles for t in time_steps
            if model.getVarByName(f"E_pos[{m},{t}]").X > ENERGY_TOLERANCE_KWH)
        # DSO demand charge on the daily maximum grid draw at the depot.
        # Recomputed from the primitives rather than read from the site_peak_kW variable:
        # when the demand charge is 0 nothing pushes that variable down to the true peak.
        site_peak_kW_value = 0.0
        for t in time_steps:
            depot_charging_kW = sum(
                model.getVarByName(f"E_private[{m},{t}]").X for m in bev_vehicles) / STEP_HOURS
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
        # different consumptions are priced differently instead of at a fleet average
        ice_energy_cost = sum(
            cost_vehicle_100km[m] * 0.01 * trip_distances.get(f, 0.0)
            for m, f, _s in assigned_trip_starts if m in ice_vehicles
        )
        # bev charging is billed on the energy split the model decided on, exactly as the
        # objective prices it: depot from the grid, external, and the share the site's own
        # PV plant covered
        pv_charging_per_step = {t: model.getVarByName(f"E_pv_charging_{t}").X for t in time_steps}
        depot_charging_per_step = {
            t: sum(model.getVarByName(f"E_private[{m},{t}]").X for m in bev_vehicles)
            for t in time_steps}
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
        external_charging_kWh = sum(model.getVarByName(f"E_public[{m},{t}]").X
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
        # 5.5b vehicle-to-vehicle (3.3.15c). Recomputed from the flows rather than read off
        # E_v2v, so the figure is exactly min(charging, discharging) net of what the sun
        # already covered, whatever pressure the objective did or did not put on the
        # variable - with both overheads at zero the term is worth nothing and the solver
        # has no reason to push it anywhere.
        v2v_per_step = {}
        for t in time_steps:
            charged_t = sum(model.getVarByName(f"E_private[{m},{t}]").X for m in bev_vehicles)
            discharged_t = sum(E_neg[m, t].X for m in bev_vehicles)
            local_room = max(0.0, charged_t - pv_charging_per_step.get(t, 0.0))
            v2v_per_step[t] = (min(local_room, discharged_t)
                               if v2v_status == 'on' else 0.0)
        v2v_kWh = sum(v2v_per_step.values())
        # what those kWh would have cost had they gone out to the grid and come back
        v2v_saved_grid_fees = grid_energy_overhead_eur_per_kWh * v2v_kWh
        v2v_saved_selling_fees = energy_selling_overhead_eur_per_kWh * v2v_kWh
        v2v_saved_total = v2v_saved_grid_fees + v2v_saved_selling_fees
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
        if day_routing is not None:
            for m in vehicles:
                for f in day_trips_list:
                    for s in possible_start_times.get(f, []):
                        started = model.getVarByName(f"route_start[{m},{f},{s}]")
                        if started is not None and started.X > 0.5:
                            routes_driven += 1
                            deadhead_approach_km += day_routing.approach.get(f, (0.0, 0.0))[0]
                        ended = model.getVarByName(f"route_end[{m},{f},{s}]")
                        if ended is not None and ended.X > 0.5:
                            deadhead_return_km += day_routing.ret.get(f, (0.0, 0.0))[0]
                    for (pf, pg), (km, _hours) in day_routing.links.items():
                        if pf != f:
                            continue
                        linked = model.getVarByName(f"chain[{m},{f},{pg}]")
                        if linked is not None and linked.X > 0.5:
                            chains_used += 1
                            deadhead_chain_km += km
                            if km <= 0.0:
                                direct_chains_used += 1
            # how much of the fleet's day is spent standing at home, which is the window
            # depot charging and V2G actually had available to them
            depot_steps = sum(model.getVarByName(f"at_depot[{m},{t}]").X
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

        away_by_vehicle = {}
        for m in vehicles:
            if day_routing is not None:
                away_by_vehicle[m] = [
                    model.getVarByName(f"at_depot[{m},{t}]").X < 0.5 for t in time_steps]
            else:
                driving = set()
                for f in day_trips_list:
                    for s in possible_start_times.get(f, []):
                        if model.getVarByName(f"z_m_f_s[{m},{f},{s}]").X >= 0.5:
                            driving.update(range(s, s + trips_duration_steps[f]))
                away_by_vehicle[m] = [t in driving for t in time_steps]
        if fleet_operation_mode == 'crewed':
            roster = driver_scheduling.schedule_drivers(
                away_by_vehicle, STEP_HOURS, driver_max_shift_hours, driver_hourly_rate_eur)
        else:
            # an autonomous fleet is rostered with nobody. The duty blocks are still built,
            # because "how long is a vehicle away in one stretch" stays a useful figure and
            # is the direct comparison against what a crewed run could have managed - but
            # no driver is assigned to them and nothing is paid.
            blocks = driver_scheduling.build_duty_blocks(away_by_vehicle, STEP_HOURS)
            roster = {'drivers': [], 'blocks': blocks, 'over_shift': [],
                      'longest_block_h': max((b.hours for b in blocks), default=0.0),
                      'driver_count': 0, 'driver_lower_bound': 0, 'paid_hours': 0.0,
                      'driving_hours': sum(b.hours for b in blocks), 'break_hours': 0.0,
                      'cost_eur': 0.0, 'vehicle_changes': 0}

        # what the optimizer itself had to give up on (3.3.17). The crew limits are priced
        # rather than hard, so a run is only legal if these come back at zero - and where
        # they do not, the trips involved cannot be crewed from this depot in a day at all.
        crew_breach_h = 0.0
        peak_drivers_model = None
        if day_routing is not None and fleet_operation_mode == 'crewed':
            for var in model.getVars():
                if var.VarName.startswith(('shift_excess[', 'drive_excess[')):
                    crew_breach_h += max(0.0, var.X) * STEP_HOURS
            needed = model.getVarByName('drivers_needed')
            peak_drivers_model = None if needed is None else needed.X

        # (only when show_outputs asks for figures)
        if show_outputs == 'on':
            # 5.2.4 the driver roster, as a figure
            #
            # Two panels, because there are two questions and they are not the same one. The
            # top is who drives what and when - one row per driver, one bar per duty block,
            # labelled with the vehicle, so a driver changing trucks at the depot is a visible
            # event rather than a number in a table. The bottom is how many are on duty at each
            # moment, which is the shape of the day: where the peak sits, how long it lasts,
            # and how much of the roster is idle around it.
            #
            # Only drawn for a crewed fleet. An autonomous one has duty blocks but nobody to
            # put on the rows, and a chart of zero drivers says nothing the figures do not.
            if fleet_operation_mode == 'crewed' and roster['drivers']:
                plt, Patch = _pyplot()
                drivers_sorted = sorted(roster['drivers'], key=lambda d: (d.sign_on, d.driver_id))
                height = max(4.0, 0.45 * len(drivers_sorted) + 3.0)
                fig3, (axr, axc) = plt.subplots(
                    2, 1, figsize=(16, height), sharex=True,
                    gridspec_kw={'height_ratios': [max(2, len(drivers_sorted)), 2]})

                over_limit = {id(b) for b in roster['over_shift']}
                for row, driver in enumerate(reversed(drivers_sorted)):
                    # the span the driver is committed for, breaks included - what they are paid
                    axr.barh(row, width=driver.sign_off - driver.sign_on + 1,
                             left=driver.sign_on, color='whitesmoke',
                             edgecolor='lightgrey', height=0.75)
                    for block in driver.blocks:
                        illegal = id(block) in over_limit
                        axr.barh(row, width=block.steps, left=block.first_step,
                                 color='indianred' if illegal else 'tab:blue',
                                 edgecolor='black', linewidth=0.4, height=0.75)
                        if block.steps >= 2:
                            axr.text(block.first_step + block.steps / 2, row,
                                     f"V{block.vehicle}", ha='center', va='center',
                                     color='white', fontsize=8)
                axr.set_yticks(range(len(drivers_sorted)))
                axr.set_yticklabels([f"D{d.driver_id} ({d.shift_hours:g} h)"
                                     for d in reversed(drivers_sorted)])
                axr.set_ylabel('Driver (shift length)')
                axr.set_title(
                    f"Driver roster: {len(drivers_sorted)} drivers, "
                    f"{roster['paid_hours']:.1f} paid h, {roster['cost_eur']:.0f} € at "
                    f"{driver_hourly_rate_eur:.2f} €/h  —  max {driver_max_driving_hours:g} h "
                    f"driving / {driver_max_shift_hours:g} h shift")
                axr.legend(handles=[
                    Patch(facecolor='tab:blue', label='with a vehicle (away from the depot)'),
                    Patch(facecolor='whitesmoke', edgecolor='lightgrey',
                          label='break at the depot (paid, inside the shift)'),
                ] + ([Patch(facecolor='indianred', label='block longer than one shift')]
                     if over_limit else []), loc='upper right', fontsize=8)

                # how many are on duty at each step, and how many are actually with a vehicle
                on_shift = [sum(1 for d in drivers_sorted if d.sign_on <= t <= d.sign_off)
                            for t in time_steps]
                with_vehicle = [sum(1 for d in drivers_sorted for b in d.blocks
                                    if b.first_step <= t <= b.last_step) for t in time_steps]
                axc.fill_between(time_steps, on_shift, step='post', color='lightsteelblue',
                                 label='on shift')
                axc.fill_between(time_steps, with_vehicle, step='post', color='tab:blue',
                                 label='with a vehicle')
                axc.axhline(len(drivers_sorted), color='grey', linestyle=':', linewidth=1,
                            label=f'roster size ({len(drivers_sorted)})')
                axc.set_ylabel('Drivers')
                axc.set_xlabel('Time [hh:mm]')
                axc.legend(loc='upper right', fontsize=8)
                axc.set_ylim(0, len(drivers_sorted) + 1)

                ticks = list(range(0, len(time_steps), 4)) + [len(time_steps)]
                axc.set_xticks(ticks)
                axc.set_xticklabels([step_to_time(step) for step in ticks])
                axc.set_xlim(0, len(time_steps))
                plt.tight_layout()
                plt.savefig(project_path('results', 'driver_schedule_plot.png'),
                            bbox_inches='tight', dpi=FIGURE_DPI)
                plt.close(fig3)

                # ... and the roster as a grid: what each driver is doing, half hour by
                # half hour. The Gantt above shows the shape of a shift; this shows the
                # handovers, because the vehicle number stands in every cell and a change
                # of number down a row is a change of truck.
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
                    project_path('results', 'driver_schedule_plot_grid.png'),
                    palette=driver_palette, step_hours=STEP_HOURS,
                    title=(f"Driver roster - {len(drivers_sorted)} drivers, "
                           f"{roster['paid_hours']:.1f} paid h, "
                           f"{roster['cost_eur']:.0f} EUR"),
                    row_axis_label='Driver (shift length)', plt=plt)
        # the time cost of standing at a public station, counted on the steps the model
        # actually paid for: those outside a Lenkzeitpause. The steps inside one are free
        # of it, and reporting them separately is the only way to see whether external
        # charging was cheap because it was necessary or because the penalty was waived.
        external_steps_penalised = 0
        external_steps_in_break = 0
        if 'external_charging' in locations and external_charging_status == 'on':
            for m in bev_vehicles:
                for t in time_steps:
                    if model.getVarByName(f"x_m_t_l[{m},{t},external_charging]").X < 0.5:
                        continue
                    if model.getVarByName(f"penalized_external[{m},{t}]").X >= 0.5:
                        external_steps_penalised += 1
                    else:
                        external_steps_in_break += 1
        external_time_penalty_eur = (penalty_charging_external_time * 30
                                     * external_steps_penalised)
        energy_costs_total = ice_energy_cost + bev_actual_energy_cost
        pv_surplus_kWh = float(sum(pv_charging_available_kWh)) if pv_charging_available_kWh else 0.0
        # tolls, at the per-vehicle rates run_optimization built for the objective, so the
        # figure reported is the one that was optimised against
        toll_costs_total = sum(
            toll_rate_per_km.get(m, 0.0) * trip_distances.get(f, 0.0)
            for m, f, _s in assigned_trip_starts
        )
        v2g_earnings             = v2g_earnings_all_total      
        
        # energy_costs_total is the energy bill itself - prices from energy_dataset.xlsx
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
                drawn_kW = model.getVarByName(f"E_private[{m},{t}]").X / STEP_HOURS
                peak_demands_per_station[station] = max(peak_demands_per_station[station], drawn_kW)
        used_LIS_peak_powers = [v for v in peak_demands_per_station.values() if v > 0]
        # which of the depot's stations the schedule actually occupies, by charger_id -
        # with a heterogeneous roster the powers alone no longer identify them
        used_station_ids = [station_label(s) for s, v in peak_demands_per_station.items() if v > 0]

        # 5.7 per-bev SoC / energy figure and the depot power overview
        if show_outputs == 'on' and len(bev_vehicles) > 0:
            x_steps = time_steps[:]
            plt, Patch = _pyplot()

            # the whole day on every figure, labelled hh:mm like the disposition plot
            hour_ticks = list(range(0, len(time_steps), 4)) + [len(time_steps)]
            hour_labels = [step_to_time(step) for step in hour_ticks]

            def as_hours(axis):
                axis.set_xlim(0, len(time_steps))
                axis.set_xticks(hour_ticks)
                axis.set_xticklabels(hour_labels)

            # energy flow per vehicle and step [kWh]: + charging, - V2G discharge.
            # Battery-side, so it is the flow the SoC curve beside it actually follows;
            # the metered energy is larger in both directions by the conversion loss.
            energy_flow = {m: [model.getVarByName(f"x_m_t_E[{m},{t}]").X for t in x_steps]
                           for m in bev_vehicles}

            # 5.7.1 one symmetric energy-flow scale for the whole figure. Per-vehicle limits
            #       would silently rescale each row, so a small flow on one truck would look
            #       like a large one on the next; symmetric about zero keeps charging and
            #       discharging of equal size equally tall.
            flow_extreme = max((abs(e) for values in energy_flow.values() for e in values),
                               default=1.0)
            flow_limit = max(1.0, flow_extreme) * 1.05

            # 5.7.2 individual bev plots with SoC and energy flow
            fig_h = max(3.0 * len(bev_vehicles), 6.0)
            fig2, ax2 = plt.subplots(len(bev_vehicles), 1, figsize=(16, fig_h), sharex=True)
            ax2 = ax2.flatten().tolist() if hasattr(ax2, 'flatten') else [ax2]  # Robust list handling
            for i, m in enumerate(bev_vehicles):
                ax_soc = ax2[i]
                soc_values = [model.getVarByName(f"x_m_SoC[{m},{t}]").X for t in x_steps]
                ax_soc.plot(x_steps, soc_values, 'b-', linewidth=2, label='SoC [kWh]')
                battery_capacity = fleet.loc[fleet['vehicle_id'] == m, 'vehicle_energy_storage'].values[0]  # From fleet
                ax_soc.axhline(y=battery_capacity, color='r', linestyle='--', alpha=0.7, label='Battery Capacity')
                ax_soc.set_ylabel('SoC [kWh]')
                ax_soc.set_title(f'BEV {m}: State of Charge')
                ax_soc.legend(loc='upper right', fontsize='small')
                ax_soc.grid(True, alpha=0.3)
                as_hours(ax_soc)

                ax_e = ax_soc.twinx()
                e_values = energy_flow[m]
                colors_e = ['green' if e >= 0 else 'red' for e in e_values]
                ax_e.bar(x_steps, e_values, width=0.8, color=colors_e, alpha=0.5, label='Energy Flow E [kWh/step]')
                ax_e.set_ylabel('Energy Flow [kWh/step]')
                ax_e.set_ylim(-flow_limit, flow_limit)  # same on every row, see 5.7.1
                ax_e.axhline(y=0.0, color='grey', linewidth=0.8, alpha=0.6)

            ax2[-1].set_xlabel('Time [hh:mm]')
            plt.tight_layout()
            plt.savefig(project_path('results', 'SoC_charging_plot.png'),
                        bbox_inches='tight', dpi=FIGURE_DPI)

            # ... and the same state of charge as a grid. The curves above are exact for
            # one truck at a time; this puts the whole bev fleet in one image, where a
            # row running pale is a battery the day left with nothing in reserve.
            import hdv_grid_plots as grid_plots

            soc_grid = np.full((len(bev_vehicles), len(time_steps)), np.nan)
            for row, m in enumerate(bev_vehicles):
                capacity = float(
                    fleet.loc[fleet['vehicle_id'] == m, 'vehicle_energy_storage'].values[0])
                if capacity <= 0:
                    continue
                for t in time_steps:
                    soc_grid[row, t] = 100.0 * (
                        model.getVarByName(f"x_m_SoC[{m},{t}]").X / capacity)
            grid_plots.grid_heatmap(
                soc_grid, [f"BEV {m}" for m in bev_vehicles],
                project_path('results', 'SoC_charging_plot_grid.png'),
                value_label='State of charge (%)',
                colour_map='GnBu', step_hours=STEP_HOURS, value_format='{:.0f}',
                vmin=0.0, vmax=100.0,
                title='State of charge of every bev, per 30-min step',
                row_axis_label='Vehicle', plt=plt)

            # 5.7.2b the same grid again, but showing the power rather than the level it
            #        builds up to. The SoC grid answers "how full"; this answers "how hard
            #        it is being pushed", and the two are not the same question - a battery
            #        can sit at 80 % all afternoon whether it got there at 50 kW or at 600.
            #
            #        Metered power, not battery-side, because that is what the scale is set
            #        against: a truck pulling its rated kW reaches the end of the scale
            #        exactly, where the battery-side figure would stop short of it by the
            #        conversion loss and never quite arrive.
            power_grid = np.full((len(bev_vehicles), len(time_steps)), np.nan)
            for row, m in enumerate(bev_vehicles):
                for t in time_steps:
                    charged = model.getVarByName(f"E_pos[{m},{t}]").X
                    discharged = model.getVarByName(f"E_neg[{m},{t}]").X
                    if max(charged, discharged) <= ENERGY_TOLERANCE_KWH:
                        continue                  # not plugged in, or plugged in and idle
                    power_grid[row, t] = (charged - discharged) / STEP_HOURS

            # one scale for the whole figure and for both directions: the strongest bev's
            # rated charging power, positive for charging and the same number negative for
            # discharging. Symmetric so that a 300 kW charge and a 300 kW discharge are the
            # same distance from the middle, and shared so that a colour means the same kW
            # on every row - per-row limits would make a small truck at full power look
            # like a large one, which is the comparison the figure exists to make.
            power_limit = max(
                (float(fleet.loc[fleet['vehicle_id'] == m, 'vehicle_charging_power'].values[0])
                 for m in bev_vehicles), default=0.0)
            # v2g power is taken equal to charging power, so nothing should exceed it; the
            # guard is here because a clipped cell is a wrong number drawn confidently
            observed = np.nanmax(np.abs(power_grid)) if np.isfinite(power_grid).any() else 0.0
            power_limit = max(power_limit, float(observed), 1.0)
            grid_plots.grid_heatmap(
                power_grid, [f"BEV {m}" for m in bev_vehicles],
                project_path('results', 'charging_power_plot_grid.png'),
                value_label='Power at the meter (kW): + charging, - discharging',
                colour_map='RdBu', step_hours=STEP_HOURS, value_format='{:.0f}',
                vmin=-power_limit, vmax=power_limit,
                # white is the middle of a diverging map, so an empty cell has to be some
                # other colour or it would read as zero power rather than as no connection
                blank_colour='#EDEDED',
                title='Charging and V2G discharge power of every bev, per 30-min step',
                row_axis_label='Vehicle', plt=plt)
            # release the figures; leaving them open leaks memory across repeated runs
            # (parameter sweeps and the web interface both call postprocess many times)
            plt.close(fig2)

            # 5.7.3 depot power overview: everything that meets at the grid connection
            #       The fleet used to be a row inside the SoC figure, where it could only be
            #       compared with itself. On its own axis it can be put next to the site's
            #       own load and its PV, which is what decides the grid draw.
            depot_charging_kW_t = []   # what the charging infrastructure pulls
            v2g_feed_kW_t = []         # what the trucks push back
            for t in x_steps:
                depot_charging_kW_t.append(
                    sum(model.getVarByName(f"E_private[{m},{t}]").X for m in bev_vehicles) / STEP_HOURS)
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
            ax3.set_title('Depot power overview')
            ax3.set_xlabel('Time [hh:mm]')
            ax3.set_ylabel('Power [kW]   (negative = fed into the grid)')
            as_hours(ax3)
            ax3.grid(True, alpha=0.3)
            ax3.legend(loc='upper right', fontsize='small', ncol=2)
            plt.tight_layout()
            plt.savefig(project_path('results', 'depot_power_overview.png'),
                        bbox_inches='tight', dpi=FIGURE_DPI)
            plt.close(fig3)
            plt.close('all')
                    
        # 5.8 degradation
        #     only the V2G discharge is cycled at a cost, exactly as priced in the
        #     objective; driving and ordinary charging are not charged for.
        #     The cost is recomputed with the same SoC weighting the objective applies, so
        #     what is reported is what the optimiser actually paid. That is why it is no
        #     longer v2g_equivalent_full_cycles x €/EFC: the cycles stay a plain physical
        #     count, the cost carries the weight, and the two differ by the weight's mean.
        v2g_efc_total = 0.0
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
                              else model.getVarByName(f"x_m_SoC[{m},{t-1}]").X)
                # the piecewise-linear weight the objective was built from, not the
                # parabola it approximates, so the figure reported is the one charged
                soc_w = soc_aging_weight(soc_before / cap)
                degradation_cost_total += (efc_base * E_neg[m, t].X / discharging_efficiency
                                           * soc_w / (2.0 * cap))

        # 5.9 output results and write results dictionary
        if show_outputs == 'on':
            # Gurobi calls a run OPTIMAL as soon as it meets MIPGap, so with the default 10%
            # "optimal" alone reads as exact while the cost may still be that far above the
            # best bound. The achieved gap says how far, so print it with the status.
            print('optimization status:     ', optimization_status,
                  f'(gap {model.MIPGap * 100:.2f}%, target {optimization_MIPGap * 100:.2f}%)')
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
                print('driver cost:             ', round(roster['cost_eur'], 2),
                      f"€ at {driver_hourly_rate_eur:.2f} €/h on the shift span,",
                      f"{roster['vehicle_changes']} vehicle change(s) at the depot")
            if fleet_operation_mode == 'crewed' and crew_breach_h > 1e-6:
                print('  CREW LIMITS BROKEN:    ', f'{crew_breach_h:.1f} h beyond the',
                      f'{driver_max_driving_hours:g} h driving /',
                      f'{driver_max_shift_hours:g} h shift limits. The limits are priced,',
                      'not hard, because some trips cannot be crewed legally from this',
                      'depot at all - a trip whose approach alone is 6 h leaves no legal',
                      'day. The solver broke them only where it had to; every other',
                      'vehicle-hour in this schedule is inside the rules.')
            if roster['over_shift']:
                print('  OVER SHIFT:            ', len(roster['over_shift']), 'of',
                      len(roster['blocks']), f"block(s) exceed the {driver_max_shift_hours:g} h "
                      f"limit on their own (longest {roster['longest_block_h']:g} h) -",
                      ', '.join(f"vehicle {b.vehicle} {b.hours:g} h"
                                for b in roster['over_shift'][:3]),
                      '- each is given its own driver and counted, but the schedule cannot',
                      'be crewed legally as it stands: the MILP has no driver constraint,',
                      'so nothing pushed those vehicles to come home in time')
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
                print('  saved selling fees:    ', round(v2v_saved_selling_fees, 2),
                      f'€ ({energy_selling_overhead_eur_per_kWh:.3f} €/kWh of marketing '
                      f'not paid on the way out)')
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
                  external_steps_penalised, f'x 30 min at {penalty_charging_external_time} €/min;',
                  external_steps_in_break, 'step(s) free inside a Lenkzeitpause',
                  f'({driving_break_duration_minutes} min after '
                  f'{driving_time_before_break_minutes / 60:g} h driving)\n')
            schedule_df.to_csv(project_path('results', f'disposition_schedule.csv'), index=False)
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
            pd.DataFrame(driver_rows).to_csv(
                project_path('results', 'driver_schedule.csv'), index=False)

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
            'v2g_equivalent_full_cycles': round(v2g_efc_total, 3),
            'optimization_status': optimization_status,
            'energy_costs_€': round(energy_costs_total, 2),
            'toll_costs_€': round(toll_costs_total, 2),
            'v2g_status': v2g_status_iteration,
            'chargers_installed': len(charging_infrastructure),
            'chargers_installed_kW': [round(p) for p in charging_infrastructure],
            'chargers_used_ids': used_station_ids,
            'chargers_kW': [round(v) for v in used_LIS_peak_powers],
            'bev_kWh': [round(v) for v in used_bev_battery],
            'ice_amount': used_ice,
            'bev_amount': used_bev,
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
            'fleet_at_depot_share': (None if depot_steps_share is None
                                     else round(depot_steps_share, 3)),
            # drivers (1.4c), rostered from the finished schedule - not minimised by it
            'drivers_required': roster['driver_count'],
            'drivers_lower_bound': roster['driver_lower_bound'],
            'driver_cost_€': round(roster['cost_eur'], 2),
            'driver_paid_h': round(roster['paid_hours'], 2),
            'driver_driving_h': round(roster['driving_hours'], 2),
            'driver_break_h': round(roster['break_hours'], 2),
            'driver_vehicle_changes': roster['vehicle_changes'],
            'driver_hourly_rate_€': driver_hourly_rate_eur,
            'driver_max_shift_h': driver_max_shift_hours,
            'driver_blocks_over_shift': len(roster['over_shift']),
            'driver_longest_block_h': round(roster['longest_block_h'], 2),
            'driver_duty_blocks': len(roster['blocks']),
            'crew_breach_h': round(crew_breach_h, 2),
            'trips_removed': len(dropped_trips or []),
            'trips_removed_ids': [d['trip_ID'] for d in (dropped_trips or [])],
            'trips_removed_km': round(sum(d['trip_distance_km']
                                          for d in (dropped_trips or [])), 1),
            'warm_start_used': warm_start_used,
            'fleet_operation_mode': fleet_operation_mode,
            'fleet_operation_mode': fleet_operation_mode,
            'driver_max_driving_h': driver_max_driving_hours,
            'drivers_peak_in_model': (None if peak_drivers_model is None
                                      else round(peak_drivers_model, 2)),
            'v2v_status': v2v_status,
            'v2v_kWh': round(v2v_kWh, 1),
            'v2v_steps': v2v_steps,
            'v2v_saved_grid_fees_€': round(v2v_saved_grid_fees, 2),
            'v2v_saved_selling_fees_€': round(v2v_saved_selling_fees, 2),
            'v2v_saved_total_€': round(v2v_saved_total, 2),
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

        if show_outputs == 'on':
            print('\n\nRESULTS')
            print('optimization status:     ', optimization_status,'\n')
        
        return {
            'iteration_number': 1,
            'fleet_size': len(vehicles),
            'fleet_electrification_%': round((len(fleet[fleet['vehicle_type'] == 'bev']) / len(fleet)) * 100, 1) if len(fleet) > 0 else 0,
            'scenario': scenario_iterations,
            'year': scenario_year_iterations,
            'km_electrification_%': 999999,
            'total_fleet_distance_km': 999999,
            'median_trip_distance_km': int(statistics.median(trips['trip_distance_km'].tolist()) if len(trips) > 0 else 0),
            'v2g_earnings_total_€': 999999,
            'degradation_cost_€': 999999,
            'v2g_equivalent_full_cycles': 999999,
            'optimization_status': optimization_status,
            'energy_costs_€': 999999,
            'toll_costs_€': 999999,
            'v2g_status': v2g_status_iteration,
            'chargers_installed': len(charging_infrastructure),
            'chargers_installed_kW': [round(p) for p in charging_infrastructure],
            'chargers_used_ids': [],
            'chargers_kW': [999999],
            'bev_kWh': [999999],
            'ice_amount': 999999,
            'bev_amount': 999999,
            'depot_grid_peak_kW': 999999,
            'demand_charge_€': 999999,
            'disposition_date': date_disposition.strftime('%d.%m.%Y'),
            # 'pvgis' / 'cache', or 'synthetic' when the run had to do without
            # a real curve - see pv_allow_synthetic_profile
            'pv_profile_source': pv_profile_source,
            'trips_outside_work_hours': list(trips_outside_work_hours or []),
            'pv_generation_kWh': round(float(sum(pv_generation_kW)) * STEP_HOURS, 1),
            'pv_surplus_kWh': round(float(sum(pv_charging_available_kWh)), 1) if pv_charging_available_kWh else 0.0,
            'pv_charging_kWh': 999999,
            'pv_charging_cost_€': 999999,
            'pv_opportunity_price_€/kWh': 999999,
            'pv_energy_saving_€': 999999,
            'home_depot_location': None if day_routing is None else day_routing.depot_location,
            'routes_driven': 999999,
            'chains_used': 999999,
            'direct_chains_used': 999999,
            'deadhead_km': 999999,
            'deadhead_approach_km': 999999,
            'deadhead_return_km': 999999,
            'deadhead_chain_km': 999999,
            'fleet_at_depot_share': 999999,
            'drivers_required': 999999,
            'drivers_lower_bound': 999999,
            'driver_cost_€': 999999,
            'driver_paid_h': 999999,
            'driver_driving_h': 999999,
            'driver_break_h': 999999,
            'driver_vehicle_changes': 999999,
            'driver_hourly_rate_€': driver_hourly_rate_eur,
            'driver_max_shift_h': driver_max_shift_hours,
            'driver_blocks_over_shift': 999999,
            'driver_longest_block_h': 999999,
            'driver_duty_blocks': 999999,
            'crew_breach_h': 999999,
            'trips_removed': len(dropped_trips or []),
            'trips_removed_ids': [d['trip_ID'] for d in (dropped_trips or [])],
            'trips_removed_km': round(sum(d['trip_distance_km']
                                          for d in (dropped_trips or [])), 1),
            'warm_start_used': warm_start_used,
            'driver_max_driving_h': driver_max_driving_hours,
            'drivers_peak_in_model': 999999,
            # parameters rather than results, so they are known even when the run failed -
            # a sweep CSV keeps the same columns whichever way a row went
            'v2v_status': v2v_status,
            'v2v_kWh': 999999,
            'v2v_steps': 999999,
            'v2v_saved_grid_fees_€': 999999,
            'v2v_saved_selling_fees_€': 999999,
            'v2v_saved_total_€': 999999,
            'charging_efficiency': charging_efficiency,
            'discharging_efficiency': discharging_efficiency,
            'charging_loss_kWh': 999999,
            'discharging_loss_kWh': 999999,
            'conversion_loss_cost_€': 999999,
            'battery_charged_kWh': 999999,
            'battery_discharged_kWh': 999999,
            'grid_charging_kWh': 999999,
        }



# 6 MAIN
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
    
    param_combinations = list(itertools.product(
        scenario,
        scenario_year,
        v2g_status,
        range(1, trips_dataset_amount + 1)
    ))


    results = []
    if multiprocessing_status == 'on':
        with multiprocessing.Pool(processes=max_parallel_workers) as pool:
            computed_results = list(tqdm.tqdm(pool.imap_unordered(run_optimization, param_combinations), total=len(param_combinations)))
            for i, (result_dict, _) in enumerate(computed_results, 1):
                result_dict['iteration_number'] = i
                results.append(result_dict)
    else:
        for i, params in enumerate(tqdm.tqdm(param_combinations), 1):
            result_tuple = run_optimization(params)
            result_dict = result_tuple[0]
            result_dict['iteration_number'] = i
            results.append(result_dict)

    # 6.3 save results
    project_path('results').mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    # fleet size and bev share describe data/fleet_dataset.xlsx and the charger count
    # data/depot_dataset.xlsx, so none of the three is swept
    fleet_bev_share = round((fleet_dataset['vehicle_type'] == 'bev').mean() * 100)
    filename = (
        f"potential_analysis_results_"
        f"v={len(fleet_dataset)}_"
        f"e={fleet_bev_share}_"
        f"c={len(charging_infrastructure)}_"
        f"t={trips_dataset_amount}_"
        f"sc={scenario}_"
        f"y={scenario_year}_"
        f"v2g={v2g_status}_"
        f"MIPgap={optimization_MIPGap}_"
        f"{timestamp}.csv"
    )
    results_df = pd.DataFrame(results)
    results_df.to_csv(project_path('results', filename), index=False)
    
    # 6.4 send slack bot message after completion
    if slack_notification_token:
        client = WebClient(token=slack_notification_token)
        client.chat_postMessage(
            channel="python-updates",
            text=f'HDV Optimization completed! Results: {len(results)} iterations, saved as {filename}',
            username="Python Notification"
        )
