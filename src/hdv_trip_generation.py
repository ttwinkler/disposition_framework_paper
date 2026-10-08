"""Generate routed trips for the disposition model from inputs/order_dataset.xlsx.

Primary input : inputs/order_dataset.xlsx with the columns
                  pickup_location, delivery_location  ("PLZ City Country")
                  pickup_starttime, pickup_endtime, dropoff_starttime, dropoff_endtime

Output        : data/order_trips.csv with the schema consumed by the disposition model
                  day_ID, trip_ID, trip_window_start_time_hhmm, trip_window_end_time_hhmm,
                  trip_distance_km, trip_duration_h
                plus trip_date / the origin-destination pair for traceability and a
                source_fingerprint column identifying the order dataset it was built from.

Routing is expensive (one geocode per unique location, one route per unique OD pair),
so it is skipped whenever data/order_trips.csv already carries the fingerprint of the
current order_dataset.xlsx. Pass --force to route unconditionally.

    python src/hdv_trip_generation.py [--force] [--analysis]
"""

# 1 SETUP
# 1.1 load modules
import os
import sys
import json
import math
import hashlib
import argparse
import time as systime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
import numpy as np
import pandas as pd
import matplotlib
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable, GeocoderRateLimited, GeocoderServiceError


# 1.2 project paths
#     inputs/     the Excel datasets. Primary input, never written to.
#     data/  what this script builds from them for the model to read.
#     results/   the figures. See hdv_disposition_optimization 1.2 for why the three.
PROJECT_ROOT     = Path(__file__).resolve().parent.parent
USER_DATA_DIR    = PROJECT_ROOT / 'inputs'
WORKING_DATA_DIR = PROJECT_ROOT / 'data'
RESULT_DATA_DIR  = PROJECT_ROOT / 'results'
ORDER_DATASET  = USER_DATA_DIR / 'order_dataset.xlsx'
TRIPS_CSV      = WORKING_DATA_DIR / 'order_trips.csv'
CSV_ENCODING   = 'utf-8'

# every figure is written as PNG. A raster keeps the browser fast: the Streamlit
# page lays the figures out again on every interaction, and a vector plot of a
# dense schedule costs it thousands of DOM nodes each time. 150 dpi so the raster
# still holds up when zoomed.
FIGURE_DPI = 150


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
if __name__ != '__main__':
    matplotlib.use('Agg')
import matplotlib.pyplot as plt
from hdv_figure_style import use_figure_style

# 1.4 trip filter parameters (part of the fingerprint - changing them forces a rebuild)
# v5: durations are placed on the 30-min grid by rounding up rather than to the nearest
# step, which is what the disposition model does (its 1.11b). The version is part of the
# fingerprint, so an existing order_trips.csv built under v4 is reported as stale and re-routed
# instead of being run against a fit filter that no longer matches the model's.
GENERATOR_VERSION       = 'trips-v5'
MIN_TRIP_DURATION_H     = 1.0   # h, operator-set floor (was 0.5 = one 30-min model step)
MAX_TRIP_DURATION_H     = 9.0   # h, German driving-time limit (Lenkzeitbegrenzung)
ROUTING_MODE            = 'truck'
STEP_MINUTES            = 30

# 1.5 routing service parameters
GEOAPIFY_API_KEY        = os.environ.get('GEOAPIFY_API_KEY', 'e77f14237ad2405a847aae3f0d230cc7')
GEOAPIFY_MIN_DELAY_S    = 0.25  # free tier allows ~5 requests/s
GEOCODING_MIN_DELAY_S   = 1.1   # Nominatim usage policy: max 1 request/s
# How much of the order set may be lost to the geocoder and the router before the build
# is treated as an outage rather than as data quality. A handful of unusable addresses is
# ordinary; losing a large share means the service was down, and finishing quietly would
# hand the model a decimated trip set that looks exactly like a complete one.
MAX_UNRESOLVED_LOCATION_SHARE = 0.05
MAX_UNROUTED_ORDER_SHARE      = 0.05
ROUTING_CACHE_FILE      = WORKING_DATA_DIR / 'cache_routing.json'

# 1.6 the order dataset uses 23:59 as the "no constraint / end of day" sentinel
END_OF_DAY_SENTINEL     = '23:59'

# 1.7 columns of the generated trip set
TRIP_COLUMNS = [
    'day_ID',
    'trip_ID',
    'trip_window_start_time_hhmm',
    'trip_window_end_time_hhmm',
    'trip_distance_km',
    'trip_duration_h',
    'trip_date',
    'trip_start_location',
    'trip_end_location',
    'source_fingerprint',
]

ORDER_COLUMNS = ['pickup_location', 'delivery_location',
                 'pickup_starttime', 'pickup_endtime',
                 'dropoff_starttime', 'dropoff_endtime']

geolocator = Nominatim(user_agent="hdv_trip_generator")



# 2 PRIMARY INPUT
# 2.1 read and validate the order dataset
def load_orders(path=None):
    path = require_input(path or ORDER_DATASET)
    orders = pd.read_excel(path)
    orders.columns = [str(c).strip() for c in orders.columns]

    missing = [c for c in ORDER_COLUMNS if c not in orders.columns]
    if missing:
        raise ValueError(f"{path.name} is missing the required column(s): {missing}")

    # The workbook may contain real Excel datetimes or text in either the ISO format
    # ('2024-01-02 00:00') or the German format ('02.01.2024 00:00:00'). Mixed-format
    # parsing is required here: parsing the whole column with one inferred format can
    # reject the German text after seeing ISO text first, while dayfirst=True alone can
    # interpret ISO text as month-first.
    for column in ORDER_COLUMNS[2:]:
        orders[column] = pd.to_datetime(
            orders[column], format='mixed', errors='coerce', dayfirst=True
        )
    for column in ORDER_COLUMNS[:2]:
        orders[column] = orders[column].astype(str).str.strip()

    orders = orders[ORDER_COLUMNS].reset_index(drop=True)
    return orders


# 2.2 fingerprint of the order dataset plus every parameter that shapes the output
def order_fingerprint(orders):
    """Stable hash over the order content and the trip-generation parameters.

    Recomputed on every run and compared against the fingerprint stored in
    data/order_trips.csv; a mismatch means the trips are stale and must be re-routed.
    """
    canonical = orders.copy()
    for column in ORDER_COLUMNS[2:]:
        canonical[column] = canonical[column].dt.strftime('%Y-%m-%dT%H:%M:%S').fillna('')
    payload = {
        'version': GENERATOR_VERSION,
        'routing_mode': ROUTING_MODE,
        'min_trip_duration_h': MIN_TRIP_DURATION_H,
        'max_trip_duration_h': MAX_TRIP_DURATION_H,
        'step_minutes': STEP_MINUTES,
        'rows': canonical.to_csv(index=False),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


# 2.3 fingerprint stored in an existing trip set
def stored_fingerprint(path=None):
    path = Path(path or TRIPS_CSV)
    if not path.exists():
        return None
    try:
        existing = pd.read_csv(path, encoding=CSV_ENCODING)
    except Exception:
        return None
    if 'source_fingerprint' not in existing.columns or existing.empty:
        return None
    values = existing['source_fingerprint'].dropna().unique()
    return str(values[0]) if len(values) == 1 else None



# 3 ROUTING
# 3.1 persistent geocode / route cache, keyed by content and independent of the order set
def load_routing_cache():
    empty = {'geocode': {}, 'geocode_alternatives': {}, 'routes': {}}
    if not ROUTING_CACHE_FILE.exists():
        return empty
    try:
        cache = json.loads(ROUTING_CACHE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return empty
    for section in empty:
        cache.setdefault(section, {})
    return cache


def save_routing_cache(cache):
    ensure_working_data_dir()
    ROUTING_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding='utf-8')


# 3.2 build the geocoding queries for a "PLZ City Country" location string
def location_queries(location):
    parts = str(location).split()
    if len(parts) < 2:
        return [str(location).strip()]
    postal_code, country, city = parts[0], parts[-1], ' '.join(parts[1:-1])
    queries = [f"{postal_code} {city}, {country}"]
    if city:
        queries.append(f"{city}, {country}")
    queries.append(f"{postal_code}, {country}")
    return queries


# 3.3 geocode a location to [lon, lat]
_last_geocode_call_ts = 0.0


def geocode_query(query, max_attempts=4):
    """Resolve a single free-form query, honouring the Nominatim rate limit."""
    global _last_geocode_call_ts

    backoff_s = 1.0
    for attempt in range(max_attempts):
        try:
            elapsed = systime.time() - _last_geocode_call_ts
            if elapsed < GEOCODING_MIN_DELAY_S:
                systime.sleep(GEOCODING_MIN_DELAY_S - elapsed)

            match = geolocator.geocode(query, timeout=15)
            _last_geocode_call_ts = systime.time()

            return [float(match.longitude), float(match.latitude)] if match else None
        except GeocoderRateLimited as exc:
            wait_s = getattr(exc, 'retry_after', None) or backoff_s
            systime.sleep(max(float(wait_s), 1.0))
            backoff_s = min(backoff_s * 2, 30.0)
        except (GeocoderTimedOut, GeocoderUnavailable, GeocoderServiceError):
            if attempt < max_attempts - 1:
                systime.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 30.0)
                continue
            break
    return None


def geocode_location(location, cache, max_attempts=4):
    """Best coordinates for a location: the most specific query that resolves."""
    key = str(location).strip()
    if key in cache['geocode']:
        return cache['geocode'][key]

    for query in location_queries(key):
        coords = geocode_query(query, max_attempts)
        if coords is not None:
            cache['geocode'][key] = coords
            return coords

    cache['geocode'][key] = None
    return None


def geocode_candidates(location, cache, max_attempts=4):
    """Every distinct coordinate a location resolves to, most specific first.

    A postal-code centroid occasionally lands off the truck-accessible road
    network, which makes the router reject an otherwise valid relation; the
    less specific city-level match then provides a usable alternative.
    """
    key = str(location).strip()
    if key in cache['geocode_alternatives']:
        return [c for c in cache['geocode_alternatives'][key] if c]

    candidates = []
    for query in location_queries(key):
        coords = geocode_query(query, max_attempts)
        if coords is not None and coords not in candidates:
            candidates.append(coords)
    cache['geocode_alternatives'][key] = candidates
    return candidates


# 3.4 route one origin-destination pair; a single request yields distance and duration
_last_routing_call_ts = 0.0


def route_pair(start_coords, end_coords, max_attempts=3):
    """Return (distance_km, duration_h) for a truck route, or (None, None) on failure."""
    global _last_routing_call_ts

    if start_coords is None or end_coords is None:
        return None, None

    waypoints = f"{start_coords[1]},{start_coords[0]}|{end_coords[1]},{end_coords[0]}"
    url = (f"https://api.geoapify.com/v1/routing?waypoints={waypoints}"
           f"&mode={ROUTING_MODE}&apiKey={GEOAPIFY_API_KEY}")

    backoff_s = 1.0
    for attempt in range(max_attempts):
        try:
            elapsed = systime.time() - _last_routing_call_ts
            if elapsed < GEOAPIFY_MIN_DELAY_S:
                systime.sleep(GEOAPIFY_MIN_DELAY_S - elapsed)

            response = requests.get(url, timeout=30)
            _last_routing_call_ts = systime.time()

            if response.status_code == 200:
                properties = response.json()['features'][0]['properties']
                return round(float(properties['distance']) / 1000.0, 1), \
                       round(float(properties['time']) / 3600.0, 3)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                systime.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 30.0)
                continue
            return None, None
        except (requests.RequestException, KeyError, IndexError, ValueError):
            if attempt < max_attempts - 1:
                systime.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 30.0)
                continue
    return None, None


# 3.5 route every unique OD pair of the order set
def route_orders(orders, cache):
    locations = sorted(set(orders['pickup_location']) | set(orders['delivery_location']))
    pending_locations = [l for l in locations if l not in cache['geocode']]
    print(f"geocoding {len(pending_locations)} of {len(locations)} unique locations "
          f"({len(locations) - len(pending_locations)} cached)...")
    for index, location in enumerate(pending_locations, 1):
        geocode_location(location, cache)
        if index % 25 == 0 or index == len(pending_locations):
            print(f"  geocoded {index}/{len(pending_locations)}")
            save_routing_cache(cache)

    unresolved = [l for l in locations if cache['geocode'].get(l) is None]
    if unresolved:
        share = len(unresolved) / max(1, len(locations))
        if share > MAX_UNRESOLVED_LOCATION_SHARE:
            raise RuntimeError(
                f"{len(unresolved)} of {len(locations)} locations ({share:.0%}) could not "
                f"be geocoded, e.g. {unresolved[:3]}. Above "
                f"{MAX_UNRESOLVED_LOCATION_SHARE:.0%} this is treated as a geocoder "
                f"outage rather than as bad addresses: continuing would build the trip "
                f"set from a fraction of the orders and look no different from a full "
                f"one. The answers so far are cached, so a later run resumes where this "
                f"one stopped.")
        print(f"WARNING: {len(unresolved)} of {len(locations)} location(s) ({share:.1%}) "
              f"could not be geocoded, e.g. {unresolved[:3]}")

    pairs = sorted(set(zip(orders['pickup_location'], orders['delivery_location'])))
    pending_pairs = [p for p in pairs if f"{p[0]}|{p[1]}" not in cache['routes']]
    print(f"routing {len(pending_pairs)} of {len(pairs)} unique OD pairs "
          f"({len(pairs) - len(pending_pairs)} cached)...")
    for index, (origin, destination) in enumerate(pending_pairs, 1):
        distance_km, duration_h = route_pair(cache['geocode'].get(origin),
                                             cache['geocode'].get(destination))
        cache['routes'][f"{origin}|{destination}"] = \
            None if distance_km is None else [distance_km, duration_h]
        if index % 25 == 0 or index == len(pending_pairs):
            print(f"  routed {index}/{len(pending_pairs)}")
            save_routing_cache(cache)

    # retry rejected pairs with the less specific coordinates of either endpoint
    rejected = [p for p in pairs if cache['routes'].get(f"{p[0]}|{p[1]}") is None]
    if rejected:
        print(f"retrying {len(rejected)} unroutable OD pair(s) with alternative geocodes...")
        for origin, destination in rejected:
            origin_options = geocode_candidates(origin, cache)
            destination_options = geocode_candidates(destination, cache)
            for origin_coords in origin_options:
                for destination_coords in destination_options:
                    distance_km, duration_h = route_pair(origin_coords, destination_coords)
                    if distance_km is not None:
                        cache['routes'][f"{origin}|{destination}"] = [distance_km, duration_h]
                        break
                if cache['routes'].get(f"{origin}|{destination}") is not None:
                    break
        recovered = sum(1 for o, d in rejected if cache['routes'].get(f"{o}|{d}") is not None)
        print(f"  recovered {recovered} of {len(rejected)} pair(s)")

    save_routing_cache(cache)

    routed = orders.copy()
    lookup = [cache['routes'].get(f"{o}|{d}")
              for o, d in zip(routed['pickup_location'], routed['delivery_location'])]
    routed['trip_distance_km'] = [r[0] if r else None for r in lookup]
    routed['trip_duration_h'] = [r[1] if r else None for r in lookup]

    failed = int(routed['trip_distance_km'].isna().sum())
    if failed:
        share = failed / max(1, len(routed))
        if share > MAX_UNROUTED_ORDER_SHARE:
            raise RuntimeError(
                f"{failed} of {len(routed)} orders ({share:.0%}) could not be routed. "
                f"Above {MAX_UNROUTED_ORDER_SHARE:.0%} this is treated as a routing "
                f"service outage rather than as unroutable pairs: continuing would drop "
                f"most of the orders and produce a trip set that looks complete. The "
                f"routes resolved so far are cached, so a later run resumes from them.")
        print(f"WARNING: {failed} of {len(routed)} orders ({share:.1%}) could not be "
              f"routed and are dropped.")
    return routed



# 4 TRIP CONSTRUCTION
# 4.1 snap a timestamp onto the 30-min grid of the disposition model
def window_start_hhmm(timestamp):
    """Earliest usable step: round the availability start UP to the next grid point."""
    if pd.isna(timestamp):
        return None
    minutes = timestamp.hour * 60 + timestamp.minute + (1 if timestamp.second else 0) / 60.0
    step = math.ceil(minutes / STEP_MINUTES)
    step = min(step, (24 * 60) // STEP_MINUTES)
    return f"{(step * STEP_MINUTES) // 60:02d}:{(step * STEP_MINUTES) % 60:02d}"


def window_end_hhmm(timestamp):
    """Latest usable step: round the availability end DOWN to the grid.

    The dataset marks an unconstrained end of day as 23:59; that sentinel maps to
    "24:00" (= step 48) so the final 23:30-24:00 step stays available to the model.
    """
    if pd.isna(timestamp):
        return None
    if timestamp.strftime('%H:%M') == END_OF_DAY_SENTINEL:
        return '24:00'
    minutes = timestamp.hour * 60 + timestamp.minute
    step = minutes // STEP_MINUTES
    return f"{(step * STEP_MINUTES) // 60:02d}:{(step * STEP_MINUTES) % 60:02d}"


def hhmm_to_step(value):
    hours, minutes = map(int, str(value).split(':'))
    return (hours * 60 + minutes) // STEP_MINUTES


def duration_to_steps(duration_h):
    """Occupied time steps per trip, using the same rounding as the disposition model.

    Rounded *up*, which is the model's rule (its 1.11b): a duration that does not land on
    the grid is booked as the next whole step rather than the nearest one, so the schedule
    never receives time the truck does not have. This has to agree with the model exactly,
    because the fit test below is what promises every trip written to order_trips.csv has at
    least one feasible start once the model converts the same duration itself. Rounding
    down here and up there would write trips the model then rejects as longer than their
    own window.
    """
    steps = np.ceil(pd.to_numeric(duration_h) / (STEP_MINUTES / 60.0)).astype(int)
    return steps.clip(lower=1)


# 4.2 turn routed orders into the trip set of the disposition model
def build_trips(routed, fingerprint):
    trips = routed.copy()
    total_orders = len(trips)

    trips['trip_distance_km'] = pd.to_numeric(trips['trip_distance_km'], errors='coerce')
    trips['trip_duration_h'] = pd.to_numeric(trips['trip_duration_h'], errors='coerce')

    # 4.2.1 availability window: from the earliest pickup to the latest drop-off
    trips['trip_window_start_time_hhmm'] = trips['pickup_starttime'].apply(window_start_hhmm)
    trips['trip_window_end_time_hhmm'] = trips['dropoff_endtime'].apply(window_end_hhmm)

    # 4.2.2 discard orders the model cannot represent, with a reason breakdown
    counts = {'orders': total_orders}

    def drop(mask, reason):
        nonlocal trips
        removed = int((~mask).sum())
        if removed:
            counts[reason] = removed
        trips = trips[mask]

    drop(trips['trip_distance_km'].notna() & trips['trip_duration_h'].notna(), 'unrouted')
    drop(trips['trip_window_start_time_hhmm'].notna() & trips['trip_window_end_time_hhmm'].notna(),
         'missing_time_window')
    drop(trips['pickup_starttime'].dt.date == trips['dropoff_endtime'].dt.date, 'multi_day')
    drop(trips['trip_duration_h'] >= MIN_TRIP_DURATION_H, 'shorter_than_min_duration')
    drop(trips['trip_duration_h'] <= MAX_TRIP_DURATION_H, 'longer_than_max_duration')

    # the disposition model derives the occupied steps as ceil(duration_h * 2) and needs
    # at least one feasible start, i.e. duration_steps <= window_end_step - window_start_step
    window_steps = (trips['trip_window_end_time_hhmm'].map(hhmm_to_step)
                    - trips['trip_window_start_time_hhmm'].map(hhmm_to_step))
    duration_steps = duration_to_steps(trips['trip_duration_h'])
    drop(duration_steps <= window_steps, 'does_not_fit_time_window')

    if trips.empty:
        raise ValueError(
            "No order of the dataset produced a usable trip - refusing to write an empty "
            f"trip set. Drop reasons: {counts}"
        )

    # 4.2.3 day and trip identifiers
    trips = trips.assign(trip_date=trips['pickup_starttime'].dt.normalize())
    trips = trips.sort_values(['trip_date', 'pickup_starttime', 'pickup_location',
                               'delivery_location']).reset_index(drop=True)
    day_index = {day: i for i, day in enumerate(sorted(trips['trip_date'].unique()), start=1)}
    trips['day_ID'] = trips['trip_date'].map(day_index)
    trips['trip_ID'] = trips.groupby('day_ID').cumcount() + 1

    trips['trip_date'] = trips['trip_date'].dt.strftime('%Y-%m-%d')
    trips['trip_start_location'] = trips['pickup_location']
    trips['trip_end_location'] = trips['delivery_location']
    trips['source_fingerprint'] = fingerprint

    print(f"trips: {len(trips)} of {total_orders} orders usable over {len(day_index)} days"
          + (f"; dropped {counts}" if len(counts) > 1 else ""))
    return trips[TRIP_COLUMNS].reset_index(drop=True)



# 5 PROCESSING
# 5.1 pipeline entry point
def generate_trips(force=False, order_path=None, trips_path=None):
    """Return the trip set, re-routing only when data/order_trips.csv is missing or stale."""
    trips_path = Path(trips_path or TRIPS_CSV)
    orders = load_orders(order_path)
    fingerprint = order_fingerprint(orders)

    existing = stored_fingerprint(trips_path)
    if not force and existing == fingerprint:
        trips = pd.read_csv(trips_path, encoding=CSV_ENCODING)
        print(f"{trips_path.name} matches the current order dataset "
              f"(fingerprint {fingerprint[:12]}) - routing skipped, {len(trips)} trips loaded.")
        return trips

    if existing is None:
        reason = f"{trips_path.name} is missing or carries no fingerprint"
    elif force:
        reason = "--force requested"
    else:
        reason = (f"{trips_path.name} was built from a different order dataset "
                  f"({existing[:12]} != {fingerprint[:12]})")
    print(f"routing required: {reason}.")

    cache = load_routing_cache()
    routed = route_orders(orders, cache)
    trips = build_trips(routed, fingerprint)

    ensure_working_data_dir()
    trips.to_csv(trips_path, index=False, encoding=CSV_ENCODING)
    print(f"  -> {trips_path.relative_to(PROJECT_ROOT)} "
          f"(fingerprint {fingerprint[:12]})")
    return trips



# 6 POSTPROCESSING
# 6.1 trip-set statistics and overview plot
def analyse_trips(trips):
    ensure_working_data_dir()

    per_day = pd.DataFrame({'day': sorted(trips['day_ID'].unique())})
    grouped = trips.groupby('day_ID')
    per_day['trip_amount'] = per_day['day'].map(grouped['trip_ID'].count())
    per_day['total_distance_km'] = per_day['day'].map(grouped['trip_distance_km'].sum())
    per_day['min_distance_km'] = per_day['day'].map(grouped['trip_distance_km'].min())
    per_day['max_distance_km'] = per_day['day'].map(grouped['trip_distance_km'].max())
    per_day['median_distance_km'] = per_day['day'].map(grouped['trip_distance_km'].median())
    per_day['total_duration_h'] = per_day['day'].map(grouped['trip_duration_h'].sum())
    per_day['min_duration_h'] = per_day['day'].map(grouped['trip_duration_h'].min())
    per_day['max_duration_h'] = per_day['day'].map(grouped['trip_duration_h'].max())
    per_day['median_duration_h'] = per_day['day'].map(grouped['trip_duration_h'].median()).round(2)

    use_figure_style(plt)
    plt.figure(figsize=(10, 6))
    plt.plot(per_day['day'], per_day['min_distance_km'], label='min. trip distance km', color='#0015FFFF')
    plt.plot(per_day['day'], per_day['median_distance_km'], label='med. trip distance km', color="#7E89FFFF")
    plt.plot(per_day['day'], per_day['max_distance_km'], label='max. trip distance km', color="#081495FF")
    plt.plot(per_day['day'], per_day['trip_amount'], label='trip amount [n]', color="#000000FF")
    plt.xlabel('dataset day\n', fontweight='bold')
    plt.ylabel('trip distance (km)\n', fontweight='bold')
    plt.title("Overview of the real world disposition data set", y=1.05, fontweight="bold")
    plt.suptitle(f"({len(per_day)} days, {len(trips)} trips, "
                 f"{MIN_TRIP_DURATION_H}-{MAX_TRIP_DURATION_H} h duration filter)",
                 y=0.92)
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.xlim(per_day['day'].min(), per_day['day'].max())
    plt.ylim(0, max(per_day['max_distance_km'].max(), per_day['trip_amount'].max()))
    plt.legend(framealpha=1.0)

    plt.savefig(ensure_result_data_dir() / 'plot_result_analysis_trip_dataset_overview.png', dpi=FIGURE_DPI)
    per_day.to_csv(ensure_result_data_dir() / 'results_analysis_trips_dataset_overview.csv',
                   index=False, encoding=CSV_ENCODING)
    return per_day



# 7 MAIN
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--force', action='store_true',
                        help='re-route even if data/order_trips.csv is up to date')
    parser.add_argument('--analysis', action='store_true',
                        help='write the trip-set statistics and overview plot')
    arguments = parser.parse_args()

    trip_set = generate_trips(force=arguments.force)
    if arguments.analysis:
        print(analyse_trips(trip_set).to_string(index=False))
        plt.show()
    print('\nDONE :)\n')
