"""Match drivers to the vehicle schedule the MILP produced.

This runs *after* the optimization, on the finished disposition. That is a deliberate
split: which truck runs which trip is an energy and cost decision the MILP is built to
make, while covering the resulting movements with drivers is a rostering problem that
follows from it and would only make the MILP larger and slower to no purpose.

The whole method rests on one observation: **a driver is tied to a vehicle exactly while
that vehicle is away from the home depot.** A truck sitting on a charger needs nobody; a
truck on the road, waiting in a customer yard, or repositioning empty needs somebody, and
that somebody cannot leave it until it is home again. So the day decomposes into *duty
blocks* - one per continuous absence from the depot - and each block is indivisible.

From there the rules the operator gave us follow directly:

  - drivers change vehicles at the home depot: blocks are separated by depot time, so any
    driver free at that moment may take the next block on any vehicle
  - a driver works at most `max_shift_hours`: measured as the span from signing on for
    their first block to signing off after their last
  - breaks happen at the depot: the gaps between one driver's blocks are exactly that, and
    they are idle time at home by construction because that is where every block ends
  - and a driver drives at most `max_driving_hours` over the day - the Lenkzeit, which is
    a limit on the person and not on the truck

That last rule has to live here, and only here. The MILP caps driving *per absence* with
a counter the depot resets (its 3.3.17b), which is right under its own premise that one
absence is one driver's work. But the premise is this module's to keep or break: the
greedy below is what decides whether two absences share a driver, and if it gives both
9 h absences of a 9 h-Lenkzeit day to one person, the model has already checked each of
them separately and found nothing wrong. Nothing else in the pipeline is in a position to
notice - the MILP has no driver entity, and the vehicle schedule is finished by the time
it gets here. So the accumulated driving is carried on the blocks and checked on the
driver, which is the only place both halves of the fact are present at once.
"""

# 1 SETUP
import math


class DutyBlock:
    """One continuous absence of one vehicle from the home depot.

    wheel_hours is the driving inside that absence, as distinct from its length: an
    absence also holds the loading, the waiting in a customer yard and the Lenkzeitpause,
    none of which is Lenkzeit. It is passed in because only the model knows it - the
    figure comes off its own drive_since_depot counter. Left unset it falls back to the
    whole block, which is the conservative reading and the right one when the caller has
    no geography and `away` was built from the loaded driving anyway.
    """

    def __init__(self, vehicle, first_step, last_step, step_hours, wheel_hours=None):
        self.vehicle = vehicle
        self.first_step = first_step
        self.last_step = last_step            # inclusive
        self.step_hours = step_hours
        self._wheel_hours = wheel_hours

    @property
    def steps(self):
        return self.last_step - self.first_step + 1

    @property
    def hours(self):
        return self.steps * self.step_hours

    @property
    def wheel_hours(self):
        """Driving inside this absence [h] - never more than the absence itself."""
        if self._wheel_hours is None:
            return self.hours
        return min(float(self._wheel_hours), self.hours)

    def overlaps(self, other):
        return not (self.last_step < other.first_step or other.last_step < self.first_step)

    def __repr__(self):
        return (f"DutyBlock(vehicle={self.vehicle}, steps "
                f"{self.first_step}-{self.last_step}, {self.hours:g} h, "
                f"{self.wheel_hours:g} h driving)")


class Driver:
    """One driver's day: the blocks they cover, in order."""

    def __init__(self, driver_id):
        self.driver_id = driver_id
        self.blocks = []

    @property
    def sign_on(self):
        return min(b.first_step for b in self.blocks)

    @property
    def sign_off(self):
        return max(b.last_step for b in self.blocks)

    @property
    def shift_hours(self):
        """Sign-on to sign-off, breaks included - the span the driver is committed for."""
        if not self.blocks:
            return 0.0
        step_hours = self.blocks[0].step_hours
        return (self.sign_off - self.sign_on + 1) * step_hours

    @property
    def driving_hours(self):
        """Only the time actually spent with a vehicle away from the depot."""
        return sum(b.hours for b in self.blocks)

    @property
    def wheel_hours(self):
        """The driving itself - the Lenkzeit this driver's day accumulates.

        Not the same as driving_hours above, which is every hour the driver had a vehicle
        including the ones it stood still for. This is what the daily driving limit is
        measured against and the only quantity that limit may be checked on.
        """
        return sum(b.wheel_hours for b in self.blocks)

    @property
    def worked_steps(self):
        """Duty actually performed, in steps - the blocks themselves, not the span.

        The span from sign-on to sign-off includes the waiting at the depot between two
        blocks, which is not work. This is what a daily working-time limit is measured
        against, and it is what assign_drivers accumulates.
        """
        return sum(b.steps for b in self.blocks)

    @property
    def break_hours(self):
        return self.shift_hours - self.driving_hours

    @property
    def vehicles(self):
        return sorted({b.vehicle for b in self.blocks}, key=str)


# 2 DUTY BLOCKS
def build_duty_blocks(away_by_vehicle, step_hours, wheel_hours_by_vehicle=None):
    """Turn per-step 'this vehicle is away from the depot' flags into indivisible blocks.

    away_by_vehicle: {vehicle: sequence of bool, one per time step}
    wheel_hours_by_vehicle: optional {vehicle: sequence of float}, the driving in each
        step. Summed over a block it gives that absence's Lenkzeit. Absent, every block
        counts as driving throughout.
    """
    blocks = []
    for vehicle in sorted(away_by_vehicle, key=str):
        away = away_by_vehicle[vehicle]
        per_step = (wheel_hours_by_vehicle or {}).get(vehicle)

        def make(first, last):
            wheel = (None if per_step is None
                     else sum(per_step[s] for s in range(first, last + 1)
                              if s < len(per_step)))
            return DutyBlock(vehicle, first, last, step_hours, wheel_hours=wheel)

        first = None
        for step, is_away in enumerate(away):
            if is_away and first is None:
                first = step
            elif not is_away and first is not None:
                blocks.append(make(first, step - 1))
                first = None
        if first is not None:
            blocks.append(make(first, len(away) - 1))
    blocks.sort(key=lambda b: (b.first_step, b.last_step, str(b.vehicle)))
    return blocks


def concurrent_block_peak(blocks):
    """Most blocks running at once - a hard lower bound on the number of drivers.

    Independent of any shift limit: that many vehicles are away simultaneously, and each
    needs its own driver. Reported next to the roster the heuristic actually builds, so
    the gap between the two is visible rather than implied.
    """
    events = []
    for block in blocks:
        events.append((block.first_step, 1))
        events.append((block.last_step + 1, -1))
    events.sort()
    running = peak = 0
    for _step, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


# 3 ROSTERING
def assign_drivers(blocks, max_shift_hours, step_hours, max_working_hours=None,
                   max_driving_hours=None):
    """Cover every duty block, using as few drivers as the rules allow.

    Blocks are taken in start order and offered to the drivers already on shift. A driver
    may take one if they are free for its whole length and if adding it keeps their span
    within max_shift_hours, their duty within max_working_hours and their driving within
    max_driving_hours. Among those who can, the block goes to the driver whose span it
    grows least, so the slack that remains stays with whoever can still use it; ties go
    to the driver who finished most recently, which keeps a roster from drifting into many
    half-used shifts.

    The three limits are three different quantities and all three have to be checked:

        span    sign-on to sign-off, the depot waiting in between included
        duty    the blocks themselves - the span less that waiting
        wheel   the driving inside the blocks - the duty less loading, yard time and the
                Lenkzeitpause

    span >= duty >= wheel always, so the tests do not imply each other in the direction
    that would let one stand for the rest. The wheel test is the Lenkzeit, and it is the
    one the model upstream cannot make: it caps driving per *absence* and resets at the
    depot, which is correct only while one absence is one driver's day. This is where
    that stops being true, so this is where the daily figure is enforced.

    This is a heuristic. Covering interval jobs with a limit on each worker's span is a
    bin-packing problem and exact answers need their own solver, which is not worth another
    MILP for a roster of this size. concurrent_block_peak() gives the lower bound to judge
    it against - on a day where no limit binds, the greedy is provably optimal and the two
    agree.

    A block that breaks a limit on its own is still covered - by a driver of its own,
    flagged. Leaving it out would be the worse answer: the work exists and somebody has
    to do it, and a roster that quietly drops the hardest blocks reports a cost that does
    not pay for the day. The counts come back in over_shift and over_driving so the breach
    is explicit.

    Returns (drivers, over_shift, over_driving).
    """
    max_steps = None
    if max_shift_hours is not None:
        max_steps = int(math.floor(float(max_shift_hours) / step_hours + 1e-9))
    work_steps = None
    if max_working_hours is not None:
        work_steps = int(math.floor(float(max_working_hours) / step_hours + 1e-9))
    wheel_cap = None if max_driving_hours is None else float(max_driving_hours)
    # the blocks carry hours as multiples of step_hours, so compare with a tolerance
    # rather than exactly - a 9.0 h block against a 9 h cap must pass
    wheel_slack = 1e-9

    drivers = []
    over_shift = []
    over_driving = []
    for block in sorted(blocks, key=lambda b: (b.first_step, b.last_step, str(b.vehicle))):
        # For ONE absence the span limit is the whole test, and the duty limit must not be
        # applied to it as well. max_shift_hours is derived upstream (2.1.1b) as exactly
        # "the work a driver may do plus the break they must take while doing it", so a
        # block that fits the span contains its own rest by construction and its duty is
        # the span less that rest. Testing the span against the duty limit too charged the
        # break as working time and then refused it: with the shipped 9 h duty and a
        # 1-step-rounded 45-minute rest, every absence longer than 9 h - including one of
        # exactly max_shift_hours - was reported as breaking a limit it satisfies.
        #
        # The duty limit still does its own work below, where several blocks are laid on
        # one driver: there the waiting between blocks is not duty and the spans have to be
        # summed rather than spanned.
        breaks_span = max_steps is not None and block.steps > max_steps
        breaks_wheel = wheel_cap is not None and block.wheel_hours > wheel_cap + wheel_slack
        if breaks_span or breaks_wheel:
            # no shift can hold this one absence. It still needs a driver, who still has to
            # be paid, and the limit is recorded as broken rather than the block dropped.
            if breaks_span:
                over_shift.append(block)
            if breaks_wheel:
                over_driving.append(block)
            driver = Driver(len(drivers) + 1)
            driver.blocks.append(block)
            drivers.append(driver)
            continue

        best = None
        for driver in drivers:
            if any(block.overlaps(other) for other in driver.blocks):
                continue
            if max_steps is not None and any(b.steps > max_steps for b in driver.blocks):
                continue                      # already over the limit; do not pile more on
            if wheel_cap is not None and driver.wheel_hours > wheel_cap + wheel_slack:
                continue
            span_steps = (max(driver.sign_off, block.last_step)
                          - min(driver.sign_on, block.first_step) + 1)
            if max_steps is not None and span_steps > max_steps:
                continue
            # and the working time, which is not the same thing. The span above includes
            # the waiting at the depot between two blocks; this counts only the duty. A
            # driver may therefore be inside their spread and still out of hours, which is
            # the case the span test alone could never catch: blocks laid end to end fill a
            # span completely, so a full shift of back-to-back duty passed it.
            if work_steps is not None and driver.worked_steps + block.steps > work_steps:
                continue
            # ... and the Lenkzeit, which is smaller again. Two absences of 5 h driving
            # are 10 h at the wheel however tidily they fit inside a 10 h duty day.
            if (wheel_cap is not None
                    and driver.wheel_hours + block.wheel_hours > wheel_cap + wheel_slack):
                continue
            key = (span_steps, -driver.sign_off)
            if best is None or key < best[0]:
                best = (key, driver)

        if best is None:
            driver = Driver(len(drivers) + 1)
            drivers.append(driver)
        else:
            driver = best[1]
        driver.blocks.append(block)

    for driver in drivers:
        driver.blocks.sort(key=lambda b: b.first_step)
    return drivers, over_shift, over_driving


# 4 THE ANSWER
def schedule_drivers(away_by_vehicle, step_hours, max_shift_hours, hourly_rate_eur,
                     max_working_hours=None, max_driving_hours=None,
                     wheel_hours_by_vehicle=None):
    """Blocks -> roster -> cost. The one entry point the disposition model calls.

    Pay follows the *shift span*, sign-on to sign-off, so a break at the depot inside a
    shift is paid. That is the conservative reading and the one that matches a driver being
    committed to the day rather than to the hours they happen to be moving; charge only
    `driving_hours` instead if the operator's contract pays by the wheel. No minimum shift
    is imposed - a driver signed on for one hour costs one hour.
    """
    blocks = build_duty_blocks(away_by_vehicle, step_hours,
                              wheel_hours_by_vehicle=wheel_hours_by_vehicle)
    drivers, over_shift, over_driving = assign_drivers(
        blocks, max_shift_hours, step_hours,
        max_working_hours=max_working_hours,
        max_driving_hours=max_driving_hours)

    paid_hours = sum(d.shift_hours for d in drivers)
    return {
        'drivers': drivers,
        'blocks': blocks,
        'over_shift': over_shift,
        'over_driving': over_driving,
        'longest_block_h': max((b.hours for b in blocks), default=0.0),
        'longest_block_wheel_h': max((b.wheel_hours for b in blocks), default=0.0),
        'driver_count': len(drivers),
        'driver_lower_bound': concurrent_block_peak(blocks),
        'paid_hours': paid_hours,
        'driving_hours': sum(d.driving_hours for d in drivers),
        'wheel_hours': sum(d.wheel_hours for d in drivers),
        'max_driver_wheel_h': max((d.wheel_hours for d in drivers), default=0.0),
        'break_hours': sum(d.break_hours for d in drivers),
        'cost_eur': paid_hours * float(hourly_rate_eur),
        'vehicle_changes': sum(max(0, len(d.vehicles) - 1) for d in drivers),
    }
