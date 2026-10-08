"""Spatial preprocessing: which trips start and end at the home depot, and how they chain.

The disposition model on its own has no geography. It assigns trips to vehicles and
assumes a truck can follow any trip with any other and be at the depot to charge in
between. That is fine while trips are read as independent orders, and wrong as soon as
the question is whether one vehicle could physically do them in sequence.

This module supplies the missing geography as *preprocessing* rather than as decision
variables, so the MILP only ever sees a small pruned set of candidate chains:

    - every location is geocoded and then clustered, so two postcodes closer together
      than the tolerance radius are one place (section 3)
    - each trip's start and end is classified as at the home depot or away (section 5)
    - a trip that does not start at the depot gets an approach leg from it, and a trip
      that does not end there gets a return leg back (section 5)
    - a trip may instead be chained directly behind a trip that ends where it starts, or
      connected to the nearest trip that does not start at the depot (section 5)

Nothing here decides anything. It enumerates what is possible and what each option costs
in km and hours; the MILP picks.
"""

# 1 SETUP
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hdv_trip_generation as trip_generation

# 1.1 defaults - the disposition model overrides these from its own parameter block
DEFAULT_TOLERANCE_SHARE = 0.10   # of the median trip distance, see tolerance_radius_km()
# what a straight line understates a road by, and how fast a truck covers it. Only used
# when the router cannot be reached for a leg this module invented; every leg that comes
# from the order data is routed for real by hdv_trip_generation.
GEODESIC_DETOUR_FACTOR = 1.30
FALLBACK_SPEED_KMH = 65.0
EARTH_RADIUS_KM = 6371.0


# 2 GEOMETRY
def haversine_km(a, b):
    """Great-circle distance between two [lon, lat] pairs."""
    if a is None or b is None:
        return None
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def tolerance_radius_km(trip_distances_km, share=DEFAULT_TOLERANCE_SHARE):
    """The radius within which two locations count as one place.

    Stated as a share rather than as a distance because what counts as "the same place"
    scales with the journeys being planned: 10 km apart is the same yard on a 200 km tour
    and two different towns on a 20 km one. The reference is the median trip distance of
    the set being planned, so the radius follows the data instead of being guessed - on
    the shipped orders the median is about 109 km, which puts the default radius near
    11 km, roughly the span of a rural postcode area.
    """
    distances = sorted(float(d) for d in trip_distances_km if d and float(d) > 0)
    if not distances:
        return 0.0
    middle = len(distances) // 2
    median = (distances[middle] if len(distances) % 2
              else 0.5 * (distances[middle - 1] + distances[middle]))
    return float(share) * median


# 3 LOCATION INDEX
class LocationIndex:
    """Geocoded locations, grouped so that near-duplicates resolve to one place.

    Clustering is greedy and frequency-ordered: the most-used location claims its radius
    first, so the yard that appears in half the orders becomes the representative and the
    stray geocodes around it join it rather than the other way round.

    Greedy grouping is deliberately not transitive. With locations in a chain each 0.9
    radii apart the two ends land in different clusters, which is a real limitation - but
    the transitive alternative (single-linkage) chains whole regions into one place, and
    for deciding "is this truck at its own depot" that failure is far worse.
    """

    def __init__(self, locations, cache, radius_km, extra_locations=()):
        self.radius_km = float(radius_km)
        self.cache = cache
        self.coords = {}
        self.unresolved = []

        counted = {}
        for location in locations:
            key = str(location).strip()
            counted[key] = counted.get(key, 0) + 1
        for location in extra_locations:
            counted.setdefault(str(location).strip(), 0)

        for key in counted:
            coords = trip_generation.geocode_location(key, cache)
            self.coords[key] = coords
            if coords is None:
                self.unresolved.append(key)

        # most-used first, so the busiest location defines its neighbourhood
        ordered = sorted(counted, key=lambda k: (-counted[k], k))
        self.representative = {}
        self._members = {}
        for key in ordered:
            coords = self.coords.get(key)
            if coords is None:
                # an unresolved location is its own place: it cannot be shown to be near
                # anything, and merging it into a cluster would be an invented fact
                self.representative[key] = key
                self._members.setdefault(key, []).append(key)
                continue
            for rep in self._members:
                rep_coords = self.coords.get(rep)
                if rep_coords is None:
                    continue
                if haversine_km(coords, rep_coords) <= self.radius_km:
                    self.representative[key] = rep
                    self._members[rep].append(key)
                    break
            else:
                self.representative[key] = key
                self._members[key] = [key]

    def place(self, location):
        """The representative location of the cluster this location belongs to."""
        key = str(location).strip()
        return self.representative.get(key, key)

    def same_place(self, a, b):
        return self.place(a) == self.place(b)

    def straight_km(self, a, b):
        return haversine_km(self.coords.get(str(a).strip()), self.coords.get(str(b).strip()))

    def clusters(self):
        return {rep: sorted(set(members)) for rep, members in self._members.items()}


# 4 DEADHEAD LEGS
def leg_cost(start_location, end_location, index, allow_geodesic_fallback=True):
    """(distance_km, duration_h) for an empty run between two locations.

    Routed through the same service and the same cache the order data uses, so a leg this
    module invents is measured the way a leg from the orders is. The straight-line
    fallback exists because these legs *are* the model's own invention: refusing to plan a
    day because a repositioning run could not be routed would be a worse failure than
    planning it with a leg that is a few percent off. Callers that would rather know are
    given the choice.
    """
    if index.same_place(start_location, end_location):
        return 0.0, 0.0

    start_key, end_key = str(start_location).strip(), str(end_location).strip()
    cache_key = start_key + '|' + end_key
    routes = index.cache.setdefault('routes', {})
    cached = routes.get(cache_key)
    if cached and cached[0] is not None:
        return float(cached[0]), float(cached[1])

    distance_km, duration_h = trip_generation.route_pair(
        index.coords.get(start_key), index.coords.get(end_key))
    if distance_km is not None:
        routes[cache_key] = [distance_km, duration_h]
        return float(distance_km), float(duration_h)

    if not allow_geodesic_fallback:
        return None, None
    straight = index.straight_km(start_key, end_key)
    if straight is None:
        return None, None
    distance_km = straight * GEODESIC_DETOUR_FACTOR
    return distance_km, distance_km / FALLBACK_SPEED_KMH


def _hhmm_to_hours(value):
    """'HH:MM' -> hours as a float. '24:00' is the end of the day, not the start."""
    hours, minutes = str(value).strip().split(':')
    return int(hours) + int(minutes) / 60.0


# 5 THE CHAIN GRAPH OF ONE DAY
class DayRouting:
    """Everything spatial the MILP needs about one operating day.

    Attributes, all keyed by trip_ID:
      starts_at_depot / ends_at_depot : bool
      approach : {f: (km, h)} the empty run depot -> start(f), for trips starting away
      ret      : {f: (km, h)} the empty run end(f) -> depot, for trips ending away
      links    : {(f, g): (km, h)} f may be followed directly by g, at this empty-run cost
                 (0, 0) when g starts where f ends - the direct chain
    """

    def __init__(self, starts_at_depot, ends_at_depot, approach, ret, links,
                 index, depot_location, notes):
        self.starts_at_depot = starts_at_depot
        self.ends_at_depot = ends_at_depot
        self.approach = approach
        self.ret = ret
        self.links = links
        self.index = index
        self.depot_location = depot_location
        self.notes = notes

    def summary(self):
        direct = sum(1 for cost in self.links.values() if cost[0] <= 0.0)
        return (f"{len(self.starts_at_depot)} trips: "
                f"{sum(self.starts_at_depot.values())} start and "
                f"{sum(self.ends_at_depot.values())} end at the depot; "
                f"{len(self.approach)} approach and {len(self.ret)} return legs; "
                f"{len(self.links)} chain candidates ({direct} of them direct)")


def build_day_routing(trips, depot_location, cache, tolerance_share=DEFAULT_TOLERANCE_SHARE,
                      radius_km=None, index=None, nearest_links=1,
                      allow_geodesic_fallback=True):
    """Classify one day's trips against the home depot and enumerate the chains.

    trips is the day's slice of trips.csv - it needs trip_ID, trip_start_location,
    trip_end_location, trip_distance_km and trip_duration_h.

    The chain candidates are deliberately few. Two rules generate them:

      1. a direct chain, g straight after f, whenever g starts where f ends. This is the
         cheap case and the one worth having: no empty running at all.
      2. otherwise the nearest trip that does not already start at the depot, one per
         trip by default. A truck that finishes away from home either drives back or
         drives to the next job, and the next job worth considering is the closest one -
         enumerating all of them would square the candidate set for chains no dispatcher
         would run.

    Everything else routes through the depot, which every trip can always do because the
    approach and return legs are always available.
    """
    if index is None:
        locations = list(trips['trip_start_location']) + list(trips['trip_end_location'])
        if radius_km is None:
            radius_km = tolerance_radius_km(trips['trip_distance_km'], tolerance_share)
        index = LocationIndex(locations, cache, radius_km, extra_locations=[depot_location])

    notes = []
    if index.unresolved:
        notes.append(f"{len(index.unresolved)} location(s) could not be geocoded and are "
                     f"each treated as their own place: "
                     f"{', '.join(sorted(index.unresolved)[:3])}")
    if index.coords.get(str(depot_location).strip()) is None:
        raise ValueError(
            f"the home depot location {depot_location!r} could not be geocoded, so no trip "
            f"can be told to start or end there and neither depot charging nor V2G would "
            f"ever be possible. Give it in the same form as the trip locations, "
            f"for example '74635 Kupferzell Deutschland'.")

    starts_at_depot, ends_at_depot = {}, {}
    start_of, end_of = {}, {}
    earliest_start_h, latest_finish_h, duration_h = {}, {}, {}
    for _, row in trips.iterrows():
        f = row['trip_ID']
        start_of[f] = row['trip_start_location']
        end_of[f] = row['trip_end_location']
        starts_at_depot[f] = index.same_place(start_of[f], depot_location)
        ends_at_depot[f] = index.same_place(end_of[f], depot_location)
        earliest_start_h[f] = _hhmm_to_hours(row['trip_window_start_time_hhmm'])
        latest_finish_h[f] = _hhmm_to_hours(row['trip_window_end_time_hhmm'])
        duration_h[f] = float(row['trip_duration_h'])

    def chain_fits_in_time(f, g, leg_hours):
        """Could g still start after f has finished and the empty run been driven?

        Checked against the widest each trip's own order window allows - f as early as it
        may go, g as late - so this only ever rules out a pair that no timetable could
        rescue. The disposition model narrows both windows further (working hours, and the
        Monte-Carlo draw over start times), so a chain that survives here may still turn
        out impossible there; 3.3.16c is what decides. Ruling the hopeless ones out this
        early keeps them from consuming the one nearest-neighbour slot each trip gets, and
        keeps their binaries out of the model.
        """
        earliest_finish = earliest_start_h[f] + duration_h[f] + leg_hours
        latest_start = latest_finish_h[g] - duration_h[g]
        return earliest_finish <= latest_start + 1e-9

    # 5.1 the legs that put a trip on and off the depot
    approach, ret = {}, {}
    for f in start_of:
        if not starts_at_depot[f]:
            km, hours = leg_cost(depot_location, start_of[f], index, allow_geodesic_fallback)
            if km is None:
                raise ValueError(
                    f"no route from the home depot to the start of trip {f} "
                    f"({start_of[f]!r}), so the trip cannot be reached. Check the location "
                    f"or the routing service.")
            approach[f] = (km, hours)
        if not ends_at_depot[f]:
            km, hours = leg_cost(end_of[f], depot_location, index, allow_geodesic_fallback)
            if km is None:
                raise ValueError(
                    f"no route from the end of trip {f} ({end_of[f]!r}) back to the home "
                    f"depot. Check the location or the routing service.")
            ret[f] = (km, hours)

    # 5.2 rule 1 - a direct chain wherever one trip ends where another starts
    links = {}
    skipped_on_time = 0
    skipped_on_rank = 0
    for f in start_of:
        for g in start_of:
            if f == g:
                continue
            if index.same_place(end_of[f], start_of[g]):
                if not chain_fits_in_time(f, g, 0.0):
                    skipped_on_time += 1
                    continue
                links[(f, g)] = (0.0, 0.0)

    # 5.3 rule 2 - otherwise the nearest trip that does not already start at the depot.
    # A trip already starting at the depot needs no one to drive to it: the vehicle comes
    # from home, which is the approach leg, and offering a connection to it as well only
    # duplicates that option at a higher cost.
    reachable = [g for g in start_of if not starts_at_depot[g]]
    for f in start_of:
        if ends_at_depot[f]:
            continue
        candidates = []
        for g in reachable:
            if g == f or (f, g) in links:
                continue
            straight = index.straight_km(end_of[f], start_of[g])
            if straight is None:
                continue
            # the empty run costs time as well as distance, so the nearest trip in
            # kilometres is not automatically reachable in the clock. Screened on the
            # straight line first, which understates the drive and so never rules out a
            # pair the road would have allowed; the routed hours are checked again below.
            if not chain_fits_in_time(f, g, straight / FALLBACK_SPEED_KMH):
                skipped_on_time += 1
                continue
            candidates.append((straight, g))
        candidates.sort()
        keep = max(0, int(nearest_links))
        skipped_on_rank += max(0, len(candidates) - keep)
        for _straight, g in candidates[:keep]:
            km, hours = leg_cost(end_of[f], start_of[g], index, allow_geodesic_fallback)
            if km is not None and chain_fits_in_time(f, g, hours):
                links[(f, g)] = (km, hours)

    if skipped_on_time:
        notes.append(f"{skipped_on_time} candidate chain(s) dropped because no timetable "
                     f"could fit them, even at the widest each trip's own window allows")
    # counted and reported for the same reason skipped_on_time is: this narrows the set of
    # chains the MILP may choose from, so a day that ends up more expensive - or infeasible
    # - can have its reason here rather than nowhere. It was silent, which made it the one
    # restriction on the model's freedom that never appeared in any output.
    if skipped_on_rank:
        notes.append(f"{skipped_on_rank} further candidate chain(s) dropped as not among "
                     f"the {int(nearest_links)} nearest for their trip "
                     f"(route_nearest_link_candidates)")

    return DayRouting(starts_at_depot, ends_at_depot, approach, ret, links,
                      index, str(depot_location).strip(), notes)
