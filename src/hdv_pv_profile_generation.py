"""PVGIS year cache for every day in data/order_trips.csv.

Primary inputs : inputs/depot_dataset.xlsx sheet 'generation' (plant: location, peak,
                 tilt, azimuth) and data/order_trips.csv (the calendar days to cover).
                 inputs/order_dataset.xlsx is hashed as well so a changed order book
                 invalidates the cache even before order_trips.csv is rebuilt.

Output         : data/cache_pv_profile.json
                 one 30-min PV curve per calendar day of a representative PVGIS year,
                 plus a source_fingerprint of the two Excel files it was built from.

PVGIS seriescalc is asked for one representative year for the depot plant and answers
with the whole year. Every day in trips.csv is a (month, day) slice of that year, so
one round trip fills the cache for the whole trip set - and for any other disposition
date, which is looked up by month and day with the year ignored, the same way the
depot load is.

The cache is skipped only while its fingerprint still matches the current
order_dataset.xlsx and depot_dataset.xlsx (and the plant / trip days those files
imply). A mismatch rebuilds it, including from ensure_derived_inputs(): unlike
trips.csv this is cheap, and a stale curve would silently move every energy figure.

    python src/hdv_pv_profile_generation.py [--force]
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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
import pandas as pd

from hdv_depot_load_profile_generation import read_depot_pv_parameters, DEPOT_DATASET


# 1.2 project paths
#     inputs/     the Excel datasets. Primary input, never written to.
#     data/  what this script builds from them for the model to read.
#     results/   the figures. See hdv_disposition_optimization 1.2 for why the three.
PROJECT_ROOT          = Path(__file__).resolve().parent.parent
USER_DATA_DIR         = PROJECT_ROOT / 'inputs'
WORKING_DATA_DIR      = PROJECT_ROOT / 'data'
RESULT_DATA_DIR       = PROJECT_ROOT / 'results'
ORDER_DATASET         = USER_DATA_DIR / 'order_dataset.xlsx'
TRIPS_CSV = WORKING_DATA_DIR / 'order_trips.csv'
PV_PROFILE_CACHE_JSON = WORKING_DATA_DIR / 'cache_pv_profile.json'
CSV_ENCODING          = 'utf-8'


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


# 1.3 cache identity - changing any of these forces a rebuild
GENERATOR_VERSION = 'pv-cache-v1'
N_STEPS           = 48
STEP_HOURS        = 0.5
PVGIS_REP_YEAR    = 2020   # representative year with full coverage in PVGIS SARAH
PVGIS_LOSS        = 14     # % system loss, the seriescalc default the model has always used
PVGIS_TIMEOUT_S   = 45
DEFAULT_RETRIES   = 3
DEFAULT_BACKOFF_S = 5.0

# the timestamp spellings PVGIS uses in the seriescalc answer. Parsed strictly against
# this list: a format outside it means the API changed, which is worth an error rather
# than a guess at what the record meant.
PVGIS_TIMESTAMP_FORMATS = ('%Y%m%d:%H%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M')

PLANT_FIELDS = (
    'pv_latitude_deg',
    'pv_longitude_deg',
    'pv_peak_power_kW',
    'pv_tilt_deg',
    'pv_azimuth_deg',
)


# 1.4 in-memory copy of the disk cache, so a process that already loaded it does not
#     reopen the JSON for every day of a design run
_pv_profile_cache = {}
_pv_cache_document = None


# 2 FINGERPRINT
# 2.1 hash of a primary dataset as it sits on disk
def file_sha256(path):
    """SHA-256 of the file bytes. Any rewrite of the Excel changes this."""
    path = require_input(path)
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b''):
            digest.update(chunk)
    return digest.hexdigest()


# 2.2 calendar days the trip set actually uses
def trip_dates_and_fingerprint(path=None):
    """Sorted ISO dates in data/order_trips.csv, and the order-dataset fingerprint it stores.

    Days, not rows: PVGIS is asked per calendar day, and many trips share one. The
    fingerprint column is the same hash generate_trips() writes, so a trip rebuild
    that dropped or added a day is visible here even when the Excel bytes did not
    change (filter parameters live in that hash).
    """
    path = require_input(path or TRIPS_CSV, 'src/hdv_trip_generation.py')
    columns = pd.read_csv(path, encoding=CSV_ENCODING, nrows=0).columns.tolist()
    wanted = [c for c in ('trip_date', 'source_fingerprint') if c in columns]
    if 'trip_date' not in wanted:
        raise ValueError(
            f"{path.name} has no trip_date column; regenerate it with "
            "src/hdv_trip_generation.py."
        )
    frame = pd.read_csv(path, encoding=CSV_ENCODING, usecols=wanted)
    dates = sorted({
        _as_datetime(value).strftime('%Y-%m-%d')
        for value in frame['trip_date'].dropna()
    })
    if not dates:
        raise ValueError(f"{path.name} contains no trip_date values.")
    stored = None
    if 'source_fingerprint' in frame.columns:
        values = frame['source_fingerprint'].dropna().unique()
        stored = str(values[0]) if len(values) == 1 else None
    return dates, stored


# 2.3 the payload whose hash is stored on the cache
def pv_cache_fingerprint(order_sha, depot_sha, trips_fingerprint, dates, plant,
                         n_steps=N_STEPS):
    """Stable hash over the two Excel files, the trip days and the plant the query uses.

    Recomputed on every run and compared against source_fingerprint in
    data/cache_pv_profile.json. A mismatch means the cache is not the current
    inputs/ files and must be rebuilt.
    """
    payload = {
        'version': GENERATOR_VERSION,
        'n_steps': int(n_steps),
        'step_hours': STEP_HOURS,
        'rep_year': PVGIS_REP_YEAR,
        'loss': PVGIS_LOSS,
        'order_dataset_sha256': order_sha,
        'depot_dataset_sha256': depot_sha,
        'trips_source_fingerprint': trips_fingerprint or '',
        'dates': list(dates),
        'plant': _canonical_plant(plant),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


# 2.4 everything the cache has to agree with right now
def current_cache_sources(n_steps=N_STEPS):
    """Plant, trip days and fingerprints of the Excel files currently in inputs/."""
    require_input(ORDER_DATASET)
    require_input(DEPOT_DATASET)
    plant = read_depot_pv_parameters()
    dates, trips_fingerprint = trip_dates_and_fingerprint()
    order_sha = file_sha256(ORDER_DATASET)
    depot_sha = file_sha256(DEPOT_DATASET)
    fingerprint = pv_cache_fingerprint(
        order_sha, depot_sha, trips_fingerprint, dates, plant, n_steps=n_steps)
    return {
        'plant': plant,
        'dates': dates,
        'trips_fingerprint': trips_fingerprint,
        'order_sha': order_sha,
        'depot_sha': depot_sha,
        'fingerprint': fingerprint,
        'n_steps': int(n_steps),
    }


# 3 DATES AND PLANT
def _as_datetime(value):
    """One date as datetime, from the spellings this project actually writes."""
    if isinstance(value, datetime):
        return value
    if hasattr(value, 'to_pydatetime'):
        converted = value.to_pydatetime()
        if isinstance(converted, datetime):
            return converted
    if isinstance(value, str):
        text = value.strip()
        for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%Y-%m-%d %H:%M:%S',
                    '%Y-%m-%d %H:%M:%S.%f'):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d')
        except ValueError:
            pass
    raise ValueError(f"unsupported date value {value!r}")


def _md_key(month, day):
    """Calendar-day key: PVGIS weather is looked up by month and day, year ignored."""
    return f"{int(month):02d}-{int(day):02d}"


def _date_md(value):
    stamp = _as_datetime(value)
    return _md_key(stamp.month, stamp.day)


def _canonical_plant(plant):
    return {name: float(plant[name]) for name in PLANT_FIELDS}


def _plants_match(left, right):
    if not left or not right:
        return False
    try:
        a, b = _canonical_plant(left), _canonical_plant(right)
    except (KeyError, TypeError, ValueError):
        return False
    return all(abs(a[name] - b[name]) < 1e-9 for name in PLANT_FIELDS)


def _requested_plant(peak_kW, lat, lon, tilt_deg, azimuth_deg):
    return {
        'pv_latitude_deg': float(lat),
        'pv_longitude_deg': float(lon),
        'pv_peak_power_kW': float(peak_kW),
        'pv_tilt_deg': float(tilt_deg),
        'pv_azimuth_deg': float(azimuth_deg) % 360.0,
    }


def pvgis_aspect(azimuth_deg):
    """Compass azimuth of the modules -> the 'aspect' PVGIS expects.

    depot_dataset.xlsx states the orientation the way a site plan does (0 = north,
    90 = east, 180 = south, 270 = west); PVGIS counts from south (0 = south,
    -90 = east, +90 = west). Without the conversion a south-facing plant would be
    queried as if it faced west.
    """
    return ((float(azimuth_deg) - 180.0 + 180.0) % 360.0) - 180.0


def _memory_key(n_steps, plant, md):
    plant = _canonical_plant(plant)
    return (int(n_steps),
            round(plant['pv_latitude_deg'], 5),
            round(plant['pv_longitude_deg'], 5),
            float(f"{plant['pv_peak_power_kW']:g}"),
            float(f"{plant['pv_tilt_deg']:g}"),
            float(f"{plant['pv_azimuth_deg']:g}"),
            md)


# 4 DISK CACHE
def _is_cache_document(payload):
    """True for the fingerprinted document this generator writes, not the legacy flat map."""
    return isinstance(payload, dict) and isinstance(payload.get('profiles'), dict)


def _read_cache_document():
    """The cache file as a document, or None if it is missing, legacy or unreadable."""
    if not PV_PROFILE_CACHE_JSON.exists():
        return None
    try:
        with open(PV_PROFILE_CACHE_JSON, encoding=CSV_ENCODING) as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not _is_cache_document(payload):
        return None
    return payload


def _write_cache_document(document):
    """Replace the cache file in one step so a reader never sees a partial write."""
    ensure_working_data_dir()
    temporary = PV_PROFILE_CACHE_JSON.with_suffix(f'.{os.getpid()}.tmp')
    with open(temporary, 'w', encoding=CSV_ENCODING) as handle:
        json.dump(document, handle, ensure_ascii=False)
    os.replace(temporary, PV_PROFILE_CACHE_JSON)


def _clear_memory_cache():
    global _pv_cache_document
    _pv_profile_cache.clear()
    _pv_cache_document = None


def _warm_memory(document):
    """Load every stored curve into the process-local dict."""
    global _pv_cache_document
    _pv_cache_document = document
    plant = document.get('plant') or {}
    n_steps = int(document.get('n_steps') or N_STEPS)
    for md, profile in (document.get('profiles') or {}).items():
        if not isinstance(profile, list):
            continue
        _pv_profile_cache[_memory_key(n_steps, plant, md)] = [float(v) for v in profile]


def _profiles_cover(document, dates, n_steps):
    """True if every trip date has a complete 30-min curve in the document."""
    if not _is_cache_document(document):
        return False
    if int(document.get('n_steps') or 0) != int(n_steps):
        return False
    profiles = document['profiles']
    for date in dates:
        entry = profiles.get(_date_md(date))
        if not isinstance(entry, list) or len(entry) != int(n_steps):
            return False
    return True


def _document_matches_sources(document, sources):
    """True when the cache was built from the current inputs/ files and covers the trip days."""
    if not _is_cache_document(document):
        return False
    if document.get('version') != GENERATOR_VERSION:
        return False
    if document.get('source_fingerprint') != sources['fingerprint']:
        return False
    if not _plants_match(document.get('plant'), sources['plant']):
        return False
    return _profiles_cover(document, sources['dates'], sources['n_steps'])


def pv_cache_matches_sources():
    """Whether data/cache_pv_profile.json already matches the current inputs/ files.

    Used by the web interface to treat a present-but-stale cache like a missing one,
    so the first page load rebuilds it instead of waiting for a model run.
    """
    try:
        sources = current_cache_sources()
    except Exception:
        return False
    return _document_matches_sources(_read_cache_document(), sources)


def _cache_document_from_parts(sources, profiles):
    rounded = {
        md: [round(float(v), 4) for v in curve]
        for md, curve in profiles.items()
    }
    return {
        'version': GENERATOR_VERSION,
        'source_fingerprint': sources['fingerprint'],
        'order_dataset_sha256': sources['order_sha'],
        'depot_dataset_sha256': sources['depot_sha'],
        'trips_source_fingerprint': sources['trips_fingerprint'],
        'trip_dates': list(sources['dates']),
        'plant': _canonical_plant(sources['plant']),
        'rep_year': PVGIS_REP_YEAR,
        'n_steps': int(sources['n_steps']),
        'step_hours': STEP_HOURS,
        'profiles': rounded,
    }


# 5 PVGIS
def _parse_pvgis_timestamp(text):
    """One PVGIS timestamp, or None if it matches none of the documented formats."""
    for fmt in PVGIS_TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _pvgis_url(plant):
    plant = _canonical_plant(plant)
    return (
        "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc"
        f"?lat={plant['pv_latitude_deg']}&lon={plant['pv_longitude_deg']}"
        f"&startyear={PVGIS_REP_YEAR}&endyear={PVGIS_REP_YEAR}"
        "&pvcalculation=1"
        f"&peakpower={plant['pv_peak_power_kW']}"
        f"&loss={PVGIS_LOSS}"
        f"&angle={plant['pv_tilt_deg']:g}&aspect={pvgis_aspect(plant['pv_azimuth_deg']):g}"
        "&pvtechchoice=crystSi&mountingplace=free"
        "&outputformat=json"
    )


def _pvgis_hourly(url, retries, backoff_s):
    """The hourly series of one seriescalc call, retrying a failing round trip.

    Network hiccups and 5xx answers are transient and worth retrying; an exhausted retry
    budget is not something to paper over, so it raises. The caller decides what a run
    without a real PV curve should do.
    """
    last_error = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=PVGIS_TIMEOUT_S)
            response.raise_for_status()
            hourly = response.json().get('outputs', {}).get('hourly', [])
            if hourly:
                return hourly
            last_error = 'the answer carried no hourly series'
        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'
        if attempt < attempts:
            systime.sleep(backoff_s * attempt)
    raise RuntimeError(f"PVGIS did not answer after {attempts} attempts "
                       f"({last_error}).")


def _interpolate_30min(prof24, steps, step_hours):
    """24 hourly kW values -> the model's 30-min grid, linear between integer hours."""
    profile = []
    for t in steps:
        hour = t * step_hours
        lower = min(int(math.floor(hour)), 23)
        # the last half hour holds the final hourly value rather than interpolating into
        # hour 0 of the same day. Wrapping was harmless for PV - both ends of the day are
        # zero - but it is a wrap where a carry was meant, and it would be wrong for any
        # series that is non-zero at midnight.
        upper = min(lower + 1, 23)
        frac = hour - math.floor(hour)
        value = prof24[lower] * (1.0 - frac) + prof24[upper] * frac
        profile.append(max(0.0, float(value)))
    return profile


def _profiles_from_hourly(hourly, steps, step_hours):
    """Every complete calendar day in a seriescalc year -> 30-min curves keyed MM-DD."""
    hours_of_day = {}
    unparsed = []
    for record in hourly:
        stamp = _parse_pvgis_timestamp(str(record.get('time', '')))
        if stamp is None:
            unparsed.append(str(record.get('time', '')))
            continue
        hours_of_day.setdefault((stamp.month, stamp.day), {})[stamp.hour] = (
            float(record.get('P', 0.0) or 0.0) / 1000.0
        )
    if unparsed:
        raise RuntimeError(
            f"PVGIS returned {len(unparsed)} timestamp(s) in an unknown format, "
            f"e.g. {unparsed[:3]}. Expected one of {list(PVGIS_TIMESTAMP_FORMATS)}.")

    profiles = {}
    incomplete = []
    for (month, day), hours in hours_of_day.items():
        missing_hours = [h for h in range(24) if h not in hours]
        if missing_hours:
            incomplete.append(f"{day:02d}.{month:02d}. ({missing_hours})")
            continue
        prof24 = [hours[h] for h in range(24)]
        profiles[_md_key(month, day)] = _interpolate_30min(prof24, steps, step_hours)
    if incomplete:
        raise RuntimeError(
            f"PVGIS returned incomplete day(s) in the representative year "
            f"{PVGIS_REP_YEAR}: {incomplete[:8]}"
            + ("..." if len(incomplete) > 8 else "")
            + ". The day has to be complete before it can be used.")
    return profiles


def _fetch_year_profiles(plant, steps, retries, backoff_s, announce=True):
    """One seriescalc round trip -> 30-min curves for every day of the representative year."""
    url = _pvgis_url(plant)
    if announce:
        plant = _canonical_plant(plant)
        print(f"querying PVGIS seriescalc for {PVGIS_REP_YEAR} "
              f"({plant['pv_peak_power_kW']:g} kWp at "
              f"{plant['pv_latitude_deg']:.3f}/{plant['pv_longitude_deg']:.3f}) ...",
              flush=True)
    hourly = _pvgis_hourly(url, retries, backoff_s)
    profiles = _profiles_from_hourly(hourly, steps, STEP_HOURS)
    if announce:
        print(f"  PVGIS returned {len(profiles)} calendar days", flush=True)
    return profiles


def _simplified_pv_profile(steps, peak_kW):
    """Fallback bell-shaped intraday PV generation curve [kW]."""
    profile = []
    for t in steps:
        hour = t * STEP_HOURS
        if 6 <= hour <= 20:
            profile.append(peak_kW * math.sin((hour - 6) / 14 * math.pi))
        else:
            profile.append(0.0)
    return profile


def _missing_trip_days(profiles, dates):
    return [date for date in dates if _date_md(date) not in profiles]


# 6 PIPELINE
def generate_pv_profile_cache(force=False, announce=True,
                              retries=DEFAULT_RETRIES, backoff_s=DEFAULT_BACKOFF_S):
    """Return the PVGIS year cache, rebuilding it when the inputs/ files have moved on.

    force=True queries PVGIS even when the fingerprint still matches. A fingerprint
    mismatch always rebuilds, including when only order_dataset.xlsx changed: the
    cache file is rewritten so it names the current files. The year is re-queried
    only when the stored curves would actually be wrong (plant, grid, version, or
    a trip day the year does not cover).
    """
    sources = current_cache_sources()
    document = _read_cache_document()
    steps = list(range(sources['n_steps']))

    if not force and _document_matches_sources(document, sources):
        _warm_memory(document)
        if announce:
            print(f"{PV_PROFILE_CACHE_JSON.name} matches the current order and depot "
                  f"datasets (fingerprint {sources['fingerprint'][:12]}) - "
                  f"PVGIS skipped, {len(document['profiles'])} calendar days loaded.",
                  flush=True)
        return document

    if document is None:
        reason = f"{PV_PROFILE_CACHE_JSON.name} is missing, unreadable or in the legacy format"
    elif force:
        reason = "--force requested"
    elif document.get('source_fingerprint') != sources['fingerprint']:
        stored = (document.get('source_fingerprint') or '')[:12] or 'none'
        reason = (f"{PV_PROFILE_CACHE_JSON.name} was built from different inputs/ files "
                  f"({stored} != {sources['fingerprint'][:12]})")
    else:
        reason = f"{PV_PROFILE_CACHE_JSON.name} does not cover the current trip days"
    if announce:
        print(f"PVGIS cache rebuild required: {reason}.", flush=True)

    plant_ok = document is not None and _plants_match(document.get('plant'), sources['plant'])
    version_ok = document is not None and document.get('version') == GENERATOR_VERSION
    grid_ok = document is not None and int(document.get('n_steps') or 0) == sources['n_steps']
    year_ok = document is not None and int(document.get('rep_year') or 0) == PVGIS_REP_YEAR
    reuse_curves = (
        not force
        and plant_ok and version_ok and grid_ok and year_ok
        and _profiles_cover(document, sources['dates'], sources['n_steps'])
    )

    if reuse_curves:
        profiles = {md: [float(v) for v in curve]
                    for md, curve in document['profiles'].items()}
        if announce:
            print("  plant and trip days unchanged - refreshing the fingerprint, "
                  "PVGIS not queried.", flush=True)
    else:
        profiles = _fetch_year_profiles(
            sources['plant'], steps, retries, backoff_s, announce=announce)
        missing = _missing_trip_days(profiles, sources['dates'])
        if missing:
            raise RuntimeError(
                f"PVGIS returned no complete curve for {len(missing)} day(s) present in "
                f"{TRIPS_CSV.name} (e.g. {missing[:8]}). The representative year "
                f"{PVGIS_REP_YEAR} has to cover every trip date before the cache can "
                f"be used."
            )

    document = _cache_document_from_parts(sources, profiles)
    _write_cache_document(document)
    _clear_memory_cache()
    _warm_memory(document)
    if announce:
        print(f"  -> {PV_PROFILE_CACHE_JSON.relative_to(PROJECT_ROOT)} "
              f"({len(document['profiles'])} calendar days, "
              f"{len(sources['dates'])} trip dates, "
              f"fingerprint {sources['fingerprint'][:12]})",
              flush=True)
    return document


def generate_pv_intraday_profile(steps, peak_kW, date, lat, lon,
                                 tilt_deg=35.0, azimuth_deg=180.0,
                                 retries=None, retry_backoff_s=None,
                                 allow_synthetic='off'):
    """Intraday PV power profile [kW] for one date and the depot plant.

    Looks up month and day in the year cache (built from depot_dataset.xlsx and
    order_trips.csv). A date the cache does not yet hold is filled from a fresh PVGIS
    year for this plant. A failing round trip raises unless allow_synthetic is
    'on', in which case the simplified bell curve is returned and deliberately
    not written to disk.

    Returns (profile, source) with source in {'cache', 'pvgis', 'synthetic'}.
    """
    retries = DEFAULT_RETRIES if retries is None else retries
    retry_backoff_s = DEFAULT_BACKOFF_S if retry_backoff_s is None else retry_backoff_s
    steps = list(steps)
    plant = _requested_plant(peak_kW, lat, lon, tilt_deg, azimuth_deg)
    md = _date_md(date)
    key = _memory_key(len(steps), plant, md)

    if key in _pv_profile_cache:
        return list(_pv_profile_cache[key]), 'cache'

    document = _pv_cache_document if _pv_cache_document is not None else _read_cache_document()
    if document is not None and _plants_match(document.get('plant'), plant):
        entry = (document.get('profiles') or {}).get(md)
        if isinstance(entry, list) and len(entry) == len(steps):
            profile = [float(v) for v in entry]
            _pv_profile_cache[key] = profile
            return list(profile), 'cache'

    # not in the depot year cache: either it has not been built yet, or this is a
    # date / plant the fingerprint did not cover. One seriescalc call fills the year.
    target = _as_datetime(date)
    try:
        profiles = _fetch_year_profiles(
            plant, steps, retries, retry_backoff_s, announce=False)
        if md not in profiles:
            raise RuntimeError(
                f"PVGIS returned no value for {target.strftime('%d.%m.')} in the "
                f"representative year {PVGIS_REP_YEAR}. The day has to be complete "
                f"before it can be used.")
        profile = list(profiles[md])
        _pv_profile_cache[key] = profile

        # merge into the depot cache only when this is the depot plant; a different
        # plant must not overwrite the fingerprinted year
        if document is not None and _plants_match(document.get('plant'), plant):
            merged = {k: [float(v) for v in curve]
                      for k, curve in (document.get('profiles') or {}).items()}
            merged.update(profiles)
            sources = {
                'fingerprint': document.get('source_fingerprint'),
                'order_sha': document.get('order_dataset_sha256'),
                'depot_sha': document.get('depot_dataset_sha256'),
                'trips_fingerprint': document.get('trips_source_fingerprint'),
                'dates': document.get('trip_dates') or [],
                'plant': document.get('plant'),
                'n_steps': int(document.get('n_steps') or len(steps)),
            }
            written = _cache_document_from_parts(sources, merged)
            _write_cache_document(written)
            _warm_memory(written)
        return profile, 'pvgis'

    except Exception as exc:
        if allow_synthetic != 'on':
            raise RuntimeError(
                f"Could not obtain the depot PV curve for {target.strftime('%d.%m.%Y')} "
                f"at {lat}/{lon}: {exc}\n"
                f"The curve drives every energy figure of the run, so it is not "
                f"substituted silently. Retry when PVGIS is reachable, or set "
                f"pv_allow_synthetic_profile = 'on' to accept a simplified bell curve - "
                f"the run then reports pv_profile_source = 'synthetic'."
            ) from exc
        print(f"WARNING: PVGIS unavailable ({exc}). Using the simplified PV curve; "
              f"every PV figure of this run is synthetic.", flush=True)
        return _simplified_pv_profile(steps, peak_kW), 'synthetic'


# 7 MAIN
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--force', action='store_true',
                        help='query PVGIS even if the cache already matches the current files')
    arguments = parser.parse_args()
    generate_pv_profile_cache(force=arguments.force)
    print('\nDONE :)\n')
