# HDV Disposition Optimization

MILP disposition model for HDV (heavy duty vehicle) fleets, with a Streamlit web
interface as its standard operating environment.

## Layout

```
main.py         entry point - starts the web interface by default
inputs/      the four primary Excel datasets. Authored, never written to.
data/   what the pipeline derives from them so the model can run - the routed
                trip set, the depot and price curves, the PVGIS and routing caches.
                All `input_*`, all regenerable: delete it and the next run rebuilds it.
results/    the answers - run figures and the run summary CSVs. Nothing in here is
                read back by a later run.
src/            all source: the model, the input generators and the web interface
```

The three data directories are split by **what a file is**, not by when it was made. They
were two, with the derived inputs and the answers sharing `results/`, and that made "can I
clear this out?" unanswerable without knowing each filename one at a time: the routed trip
set and the PVGIS cache cost an hour of geocoding and routing to rebuild, and they sat in
the same listing as a plot from a run nobody kept. Now the answer is a property of the
directory — `data/` is always safe to delete, `results/` is what you keep, and
`inputs/` is the only one that is irreplaceable.

`src/` holds the whole application:

| Module | Role |
| --- | --- |
| `hdv_disposition_optimization.py` | the MILP model and the batch parameter sweep |
| `hdv_cost_parameter_generation.py` | `costs_dataset.xlsx` -> energy cost parameters |
| `hdv_depot_load_profile_generation.py` | `depot_dataset.xlsx` -> depot load profile, PV plant, charging stations |
| `hdv_trip_generation.py` | `order_dataset.xlsx` -> geocoded, routed trips |
| `hdv_route_chaining.py` | trip locations -> home-depot classification, approach/return legs, chain candidates |
| `hdv_driver_scheduling.py` | finished schedule -> duty blocks, driver roster and cost |
| `hdv_grid_plots.py` | the shared grid design every schedule figure is drawn in |
| `hdv_web_interface.py` | the whole web interface: launcher, model bridge and Streamlit app |

## Auto-sizing: designing a fleet instead of dispatching one

With **Auto-Sizing** on, *4 · Run → Asset Sizing* answers a different question from
the rest of the model: not *how should this fleet run these days* but *what fleet should we
buy for these days*. It is **one fleet for the whole range** — the ownership decision is
shared by every day, the operating variables are per day (model section 3.5).

That sharing is the whole point. Sizing day by day and taking the largest answer cannot see
that a truck bought for Tuesday is free on Wednesday: each day pays for it alone, so each
day under-buys. Here the fleet is paid for once over the horizon while every day gets to
use it, which is the trade a fleet buyer actually faces. The days couple through nothing
else — every battery returns to its starting level at 24:00, so no charge carries over.

**What it chooses from.** The `synthetic_fleet` sheet of `fleet_dataset.xlsx` lists vehicle
*types*, one per row, with no `vehicle_id` — trucks that could be bought, not trucks that
exist. The model instantiates several copies of each and decides which to own. Copies of one
type are identical, so they are forced to be taken in order; without that, an answer owning
*k* of a type has *k*! equally good relabellings for branch-and-bound to walk.

**What stops it buying trucks to farm V2G.** Ownership is priced from `vehicle_price`:
straight-line depreciation to a residual value plus interest on the capital tied up, spread
over the operating days of a year (`vehicle_service_life_years`,
`vehicle_operating_days_per_year`, `vehicle_residual_value_share`,
`vehicle_capital_interest_rate`, section 1.2e). On the example types that is EUR 47/day for the
diesel and €52–130/day for the battery trucks, against a V2G spread worth a small fraction
of that on one battery — so a truck that does not drive cannot pay for itself.
`penalty_vehicle_use` is still there and still a tie-break; it is now the smaller of the two
by two orders of magnitude.

**The depot does not limit the fleet.** Depot charging is stated per truck against the
strongest station, which only holds while every station a truck could land on is at least as
strong as the truck — so the depot's own `charging` sheet would cap the battery fleet at
however many of its stations reach the strongest bev's power, regardless of how much work
the days carry. That is the depot answering a question the design run did not ask. So a
design run charges against a depot built to fit: **one station per battery candidate, each
at the highest charging power any bev type in `synthetic_fleet` asks for**. The fleet is then
sized by the work and by what a truck costs.

**The charging infrastructure comes back as a result, not a premise.** No station is a
decision variable and none carries a cost; what the run reports is read off the finished
schedules. A truck takes the strongest free station, so the stations a day occupies are
always its strongest *k* — which makes them comparable across days by rank. Ranking each
day's peak draws and taking the highest at every rank gives the list that would have served
every day of the range: **as many chargers as the busiest day plugged in, each rated for the
hardest that charger was ever pushed on any day.** It is shown as a table and downloadable
as CSV.

**Figures and the schedule CSV are off during a design run**, and the switch is greyed out
to say so. A design run solves every day of the range against one fleet and its answer is a
fleet; drawing a full set of figures per intermediate day is time spent on pictures nobody
asked for. Both come back the moment auto-sizing is switched off.

**An interrupted solve keeps its answer.** A design model over several days routinely hits
`optimization_time_limit_s` with a perfectly usable incumbent — a real schedule with every
number in it, missing only the proof that nothing better exists. Those runs report
`optimization_status: incumbent` rather than being discarded.

**It needs a tight MIP gap to mean anything.** A design answer is a shopping list, and at a
wide gap the list is an upper bound — the incumbent will buy more than it has to. On the
example data, days 1-2 at a 5 % gap size 4 ice + 3 bev (EUR 910,000); the same range stopped at
34 % buys the entire pool. The Results tab warns above 5 %.

## Requirements

- Python 3.10+
- streamlit>=1.28
- pandas
- numpy
- matplotlib
- openpyxl — pandas' engine for the `.xlsx` datasets; required, though no module here imports it by name
- requests
- geopy
- tqdm
- schedule
- slack_sdk — only for the optional Slack notification; imported when a message is sent, never at import time
- Gurobi with a valid licence, importable as `gurobipy` in the same environment
- The four primary datasets present in `inputs/`: `costs_dataset.xlsx`, `depot_dataset.xlsx`, `fleet_dataset.xlsx`, `order_dataset.xlsx`

### Slack notification (optional)

A run can post to Slack when it finishes. Switch it on under **3 · Settings → Notifications**
in the interface, or set `slack_notification_status = 'on'` near the top of
`src/hdv_disposition_optimization.py` for a batch sweep.

The bot token is read from the **`SLACK_BOT_TOKEN` environment variable** and from nowhere
else — not from a parameter, not from a file in this repository, and not from anything the
interface stores. A token written into the source is a token in every copy of it, and
rotating it then means finding all of them.

```
setx SLACK_BOT_TOKEN "xoxb-..."
```

Then **open a new terminal**: a process only ever sees the environment it was started
with, so an interface already running will not pick up a variable set after it launched.
Without the variable the switch in the Settings tab stays disabled and says so. A refused
post — no token, a channel the bot was never invited to, Slack unreachable — never fails
the run: the result is kept and the reason is shown on **4 · Run**.

## Quick start

```bash
cd 02_Modell

# 1. install the dependencies (there is no requirements.txt - this is the list)
pip install "streamlit>=1.28" pandas numpy matplotlib openpyxl requests geopy tqdm schedule gurobipy slack_sdk

# 2. check that gurobipy + licence work in the same environment
python -c "import gurobipy as gp; print(gp.gurobi.version())"

# 3. launch - the web interface is the default environment of main.py
python main.py


The browser opens at <http://localhost:8501>. Use `python main.py --port 8600` for a
different port.

## First run

1. Configure the scenario in the sidebar.
2. Click **Run Optimization**.
3. Explore the result metrics, the schedule table and the generated figures.

There is no preparation step to remember. Everything the model reads from `results/` is
derived from the Excel datasets in `inputs/`, so anything missing there is built on sight —
on the first run, and again whenever a derived file is deleted. The interface shows a
spinner while it works.

Budget time for the first build: geocoding every location and routing every
origin–destination pair of `order_dataset.xlsx` is rate-limited by the external services,
and a build from an empty `results/` took **~40 minutes** for this dataset (599 locations,
1024 OD pairs). The answers land in `data/cache_routing.json`, so it only happens once —
delete that file and the next build pays the full price again.

Only `inputs/` is irreplaceable. Deleting anything in `results/` is safe.

**Rebuild derived inputs** on the Run tab forces a refresh of files that are already
there, which is the only case auto-building does not cover. The same without a browser:

```bash
python main.py --prepare    # regenerate the derived inputs only
python main.py --optimize   # prepare, then run the parameter sweep
```

See **Running from the command line** below for the full flag set, and for running a single
day by date with a chosen MIP gap.

## Data flow

The model is driven exclusively by the four Excel datasets in `inputs/`. Everything
else is derived and lives in `results/` - `inputs/` is never written to.

> ### The four datasets are examples. Replace them with your own.
>
> `inputs/` ships with a complete, working set of example workbooks - a fleet, a depot, an
> energy price set and an order book - so the tool runs end to end the moment it is
> installed and every screen has something in it to look at. **They are illustrative test
> data and nothing more.** The trucks, the chargers, the PV plant, the prices, the metered
> site load and the orders are invented for the demonstration; none of them describes a
> real operation, and no number taken from them should be quoted as a result or used to
> size anything.
>
> To model your own case, replace the four workbooks and keep the structure. **The
> structure is the contract, the values are not.** Sheet names, column headings and units
> are what the readers check and what they fail on; everything else - how many vehicles,
> how many chargers and at what power, which years, which prices, how many days of orders
> - is yours to set, and the model reads whatever it finds. `inputs/originals/` keeps a
> pristine copy of the examples, so there is always a state that runs to get back to.
>
> Where this document uses a figure from the example data to make a point concrete, it
> says so. Read those as "this is the shape of the answer", never as a default or a
> recommendation.

| Primary input (`inputs/`) | Derived file (`results/`) | Produced by |
| --- | --- | --- |
| `costs_dataset.xlsx` sheet `energy_yearly` | `cost_parameter_yearly.csv` | `src/hdv_cost_parameter_generation.py` |
| `costs_dataset.xlsx` sheet `energy_daily` | `cost_parameter_hourly.csv` | `src/hdv_cost_parameter_generation.py` |
| `depot_dataset.xlsx` sheet `consumption` | `depot_load_profile.csv` | `src/hdv_depot_load_profile_generation.py` |
| `depot_dataset.xlsx` sheet `generation` | `depot_pv_parameters.csv` | `src/hdv_depot_load_profile_generation.py` |
| `depot_dataset.xlsx` sheet `charging` | `depot_charging_stations.csv` | `src/hdv_depot_load_profile_generation.py` |
| `order_dataset.xlsx` | `order_trips.csv` | `src/hdv_trip_generation.py` |
| `depot_dataset.xlsx` sheet `generation` + `order_trips.csv` | `cache_pv_profile.json` | `src/hdv_pv_profile_generation.py` |
| `fleet_dataset.xlsx` | read directly by the model | — |

`depot_dataset.xlsx` describes the site in one sheet per aspect: `consumption` is the
metered site load, `generation` the on-site PV plant and `charging` the charging
stations. Only `consumption` is a time series and gets resampled onto the 30-min grid.

The day of that series the model uses is picked by **day and month, ignoring the year**.
The sheet is a metered year and the disposition date is whatever
day is being planned, so the two can never share a year — but 7 November is 7 November, and
what a depot draws on it is a property of the season and the weekday pattern rather than of
which year it was measured in. Several years in the file are averaged per date, which reads
extra years as repeat measurements of the same day rather than as more days.

This replaced an annual average, and the difference is not cosmetic — the baseline is what
the fleet's charging is stacked on top of, so it sets the grid peak the demand charge is
billed on and decides how much PV the site's own load has already eaten before a truck sees
any of it:

| depot baseline | mean | peak |
| --- | --- | --- |
| annual average *(previous behaviour)* | 22.0 kW | 33.9 kW |
| 07.11 (the example disposition date) | 15.1 kW | 30.2 kW |
| 15.01 | 31.1 kW | 55.7 kW |
| 15.07 | 18.7 kW | 36.4 kW |
| 01.12 | 34.4 kW | **68.0 kW** |

A flat average quietly removed every seasonal effect the disposition date was chosen to
capture - in the example data the busiest day peaks at about twice the annual mean curve,
and a real metered year is unlikely to be flatter.

A `consumption` sheet is expected to cover every date a run may ask for. One it does not
cover is an error naming the date rather than a silent fallback, so a gap in the metering
shows up as a question about the sheet and not as a plausible curve. 29 February is the
one to check: a metering year that is not a leap year has no such date in it.

`costs_dataset.xlsx` carries the same four price groups on two sheets — `energy_yearly`
by `year` and `energy_daily` by `hour` (1–24):

| Column group | Unit | Varies over the day |
| --- | --- | --- |
| `electricity_spot_price_€/kWh` | € per kWh, public spot price of electricity | yes |
| `flexibility_spot_price_€/kWh` | € per kWh fed back, public spot price of flexibility | yes |
| `public_charging_price_€/kWh` | € per kWh, external charger | no |
| `public_diesel_price_€/l` | € per litre | no |

The first two were called `energy_spot_price_€/kWh` / `private_charging_price_€/kWh` and
`public_flexibility_price_€/kWh` before. Those are **public spot prices** of electricity,
not tariffs this depot negotiated, so the names now say so — and the distinction matters
for the PV accounting further down. The old headings no longer load: `COLUMN_GROUPS` in
`hdv_cost_parameter_generation.py` maps one heading to each price, so two names for one
price cannot arise. A sheet still on an old heading fails naming the group it is missing,
which says what to rename.

**The two sheets are shaped differently, and are meant to be.** `energy_yearly` is an
outlook, so it brackets every year with a `low`/`medium`/`high` band under each price
heading. `energy_daily` is one operating day, so it states a single series per price and no
band at all — there is one intraday shape, and the scenario decides what *level* it sits
at rather than what shape it has — under a note row naming the day it describes.

Neither sheet's header position is assumed. It is **located by what it contains**
(`locate_sheet_header`, generator section 2.0): the heading row is the one carrying the
price prefixes, anything above it is a note, and a row directly below it naming
`low`/`medium`/`high` is the band. Everything else follows — where the data starts, and
which column is the index, matched without regard to case so `hour` and `Hour` are the same
name. A sheet that gains another note row, or loses its band, keeps loading. A band-less
sheet is read as all three levels at once, so everything downstream keeps one structure.

**Every cell under a price heading has to be a number.** A blank or non-numeric one is
refused in the reader (generator section 2.1b), naming the sheet, the column and the
`hour` or `year` it sits on. It used to coerce to `NaN`, reach the derived CSV, reach the
objective, and fail the solve hundreds of lines later with `GurobiError: Multiplier is Nan
or Inf` — a message naming none of those three. The run was never *wrong*, but it never
said where to look either.

The note on `energy_daily` is carried through and shown — on the Inputs tab and in the
generator's own output — but **nothing is parsed out of it and nothing is decided by it**.
The day a run plans is `date_disposition`, which also picks the trips, the depot load and
the PV curve; a line in a price sheet is in no position to overrule those. It is there so
that a curve labelled for one day and a run made for another is visible rather than
discoverable.

Column order in the sheet does not matter — the groups are found by heading, not by
position. `low`/`medium`/`high` become the model's `min`/`mean`/`max`, so a row where
`medium` falls outside `[low, high]` is reported as a warning rather than accepted
silently — the `best case` / `worst case` scenarios would otherwise stop bracketing the
medium case. On `energy_daily` that check holds trivially, the three levels being one
series.

#### What a run writes

Two facts about a run decide its outputs, and neither is a setting — they are written down
by whichever entry point started it (model 1.4b1):

| | `run_kind` | `run_host` |
| --- | --- | --- |
| | `disposition` · `sizing` · `sweep` | `terminal` · `interface` |

and the rules read off them are:

- **a disposition run always generates and saves** its figures and its terminal report.
  That run is one solve, it owns the output filenames outright, and those figures are its
  whole output.
- **a sizing run and a sweep never do.** Every solve of either writes to the *same*
  filenames — `plot_suffix` (section 5.1) is only ever set by a design run — so run
  sequentially they overwrite each other and run through the Pool they can be read
  half-written. Nothing is lost: every number either reports is in the summary CSV.
- **only a terminal run shows them.** It opens the figures in a window when it finishes;
  the web interface renders the same PNGs on its Results tab, so popping windows out of
  the server process would be both useless and wrong. A machine with no display falls back
  to the Agg backend and keeps the files, which is the case `--no-interface` exists for.

This replaces the `show_outputs` parameter, which was the wrong shape in both directions:
it could be switched on for a sweep, where the figures collide, and off for a disposition
run, which is the one run whose whole output they are. Neither is a choice worth offering.

On the command line the run kind follows from `parameter.py`: one combination of the
variation lists is a disposition run, several are a sweep (model 6.0).

#### The shape, and the level it sits at

`energy_daily` is the only sheet with an intraday shape, and the two price curves — the
electricity spot price and the flexibility spot price — always take theirs from it.
Arbitrage earns the spread inside that shape, so it has to survive on every kind of run.
What the kind of run decides is the **level** that shape sits at, and
`energy_price_basis` (model 1.4b2) names it:

| Price | `'daily'` — a disposition run | `'yearly'` — a sizing run or a sweep |
| --- | --- | --- |
| electricity spot curve | `energy_daily`, as written | `energy_daily` shape, levelled onto the year & band |
| flexibility spot curve | `energy_daily`, as written | `energy_daily` shape, levelled onto the year & band |
| public charging | `energy_daily` (one number) | `energy_yearly`, year & band |
| diesel | `energy_daily` (one number) | `energy_yearly`, year & band |

Set by the interface in its 3.6 / 3.6b / 3.6c, by `sweep_solve` and the command-line
entry point in the model's 6.0 / 6.2a, or by hand in `parameter.py`.

A disposition plans one concrete day, so it is planned on that day's prices, taken at face
value: every figure in the result traces back to a cell in the workbook, with no factor in
between — the day's diesel and the day's public charger included. A sizing run and a sweep
move the **year** and the **scenario**, so both have to set the level. The two prices with
no intraday shape are read straight off the outlook; each curve is normalised by its own
daily mean and multiplied by that cell's value. A pure scaling keeps whatever each curve is
anchored on intact (the electricity curve reproduces its band's mean, the flexibility curve
its band's floor) without the model having to know which, and it states the rule directly
rather than by naming a base year.

**`energy_daily` has neither a year nor a scenario band, so on the `'daily'` basis neither
reaches a single price.** A `best case` 2025 and a `worst case` 2045 disposition run are
priced identically, and the two settings survive on the result as labels. That is what
pricing one concrete day off one concrete day's prices means. A comparison of years or
scenarios is a sweep, and a sweep is on the `'yearly'` basis, where both axes set the
level. The Disposition tab therefore does not offer **Cost scenario** or **Scenario year**
at all — a control that moves no price reads as a comparison the run can make. The model
is still handed both and still reports them, as labels on the result; the Asset Sizing tab,
which is priced off the outlook, keeps them.

The diesel price is flat on every kind of run. Which sheet states it is not a question
about its shape.

Where the daily curve already averages out to the band it is run under, the two bases are
identical and the factor is 1 — which the shipped electricity curve is for 2025 `low`, and
no other band. `hdv_cost_parameter_generation.py` prints the factor for every band on every
rebuild: that factor is exactly how far a sizing or sweep solve of the base year sits from
a disposition solve of the same day.

`energy_price_basis = 'auto'` is the default and resolves to `'yearly'` on a sizing run and
`'daily'` otherwise. Every entry point states the basis outright, so `'auto'` only has to
decide for a run that went through none of them. The basis a run used is a column of the
run summary (`energy_price_basis`, section 5.9): two rows made on different bases are not
comparable, and nothing else in that file says which one a row is.

`public_charging_price` and `public_diesel_price` have no intraday shape, so there is
nothing to scale — each is one number per run. `energy_daily` states them per hour only
because the sheet has one shape; the model averages each back to the single number it
bills (model 2.1.4b).

### Vehicle-to-vehicle (V2V)

When one truck discharges while another charges in the same half hour, both at the depot,
the energy crosses the yard's own busbar instead of the meter:

```
E_v2v[t] = min( what the depot is charging , what the depot is discharging )
```

Those kWh are never bought and never sold, so they carry **neither overhead** — the saving
per kWh is `grid_energy_overhead + energy_selling_overhead`, the same 15 ct/kWh that makes
own PV worth having, and for the same reason: the spot price is on both sides and cancels.

Both sides are measured at the charger terminals, so they are the same AC kWh. The losses
are unchanged — the energy still passes the discharging truck's inverter and the charging
truck's rectifier, so it is taxed by both efficiencies exactly as a grid round trip would
be. **V2V removes the fees, not the physics.** That is also why it creates no incentive to
shuffle energy for its own sake: a kWh moved between trucks still loses both conversions,
and restoring the sending truck costs that loss at the full buy price, so the saving is
only ever worth having when the discharge and the charge were both worth doing anyway.

V2V **competes with own PV** for the same charging demand (`E_v2v + E_pv_charging <=
depot charging`). Both are local supply saving the same fees, so which one feeds a given
kWh does not change the bill — but counting a kWh as fed by both would credit one delivery
twice.

This is a correction as much as a feature. 3.3.15 already netted the discharge against the
charging when computing the site's grid draw, so the *peak* was right — while the objective
went on billing the gross charging at the buy price and crediting the gross discharge at
the sell price. The depot was paying import fees on kWh its meter never saw. For the same
reason the **demand charge is not part of the reported saving**: those kWh never inflated
the peak, so there is nothing there to give back.

Reported as `v2v_kWh`, `v2v_steps`, `v2v_saved_grid_fees_€`, `v2v_saved_selling_fees_€` and
`v2v_saved_total_€`, with `v2v_status` to switch it off and compare.

### The two V2G channels

A discharged kWh is sold into one of two channels, selected with `v2g_price_mode`:

| Channel | Priced from | What is being sold |
| --- | --- | --- |
| `arbitrage` | `electricity_spot_price_€/kWh` | the energy, at the electricity spot price |
| `flexibility` | `flexibility_spot_price_€/kWh` | the service, not the energy |

Arbitrage settles on the **same curve the truck buys on**, hour by hour, so its earning is
the intraday spread and not a margin handed to it by a second price series. Buying at a
flat daily price while selling on the curve would pay the truck the peak price without
ever charging it at that price — a spread the depot does not actually earn — so depot
charging is priced per 30-min step off the same curve.

The two are not the *same price*, though: the depot buys at spot **+**
`grid_energy_overhead_eur_per_kWh` and sells at the bare spot price. That wedge, plus the
round-trip conversion loss, is what an arbitrage spread has to clear before a V2G slot
earns anything.

`both` sells each step into whichever channel pays more. It is a **choice, not a sum**: a
kWh leaves the battery once, so it can only be sold once. Both prices are known constants
per step, so the better one is settled before the solve rather than by a binary per step,
which could not reach a different answer. The terminal output reports the earnings and
energy of each channel separately.

Note that the curves are hourly, not aggregated into 4h blocks as they once were.
Arbitrage lives on the intraday spread, and averaging into blocks flattens exactly the
differences it trades on.

**Whether arbitrage pays is a property of the data, not of the model.** A round trip earns
the spread and costs battery wear. On a disposition run with the example dataset and fleet
the spread is 0.100 €/kWh (0.150 down to 0.050, straight out of `energy_daily`) while
degradation is 0.1042 €/kWh at aging weight 1.0 and 0.1562 €/kWh at 1.5, so no discharge
covers its own wear and the model correctly does none. Priced at zero wear the same inputs
yield 6562.8 kWh discharged for €984.42 — the channel works; it simply is not profitable at
these numbers. The spread has to clear roughly 10.4 ct/kWh before it is.

On a sizing run or a sweep the same shape is rescaled onto the scenario year and band
(*The shape, and the level it sits at*, above), so the spread scales with the level —
proportionally, since the rescale is a pure multiplication. A `worst case` run, where
electricity sits on the high band, therefore has a *wider* spread than the day as written
and is where arbitrage comes closest to paying; as the electricity outlook falls towards
2045 the spread narrows with it and arbitrage gets further away, while the flexibility
channel rises over the same years and is what carries V2G there.

That comparison is a diagnostic, not a run mode. Battery aging is always priced: there
used to be a `degradation_cost_status` switch and it has been removed, because with it off
the objective paid nothing for wear while the results still reported a `degradation_cost_€`
computed from the solved discharge — a cost the optimiser had never seen, and no way for a
reader to tell. The physical cycle count (`v2g_equivalent_full_cycles`) is reported either
way.

Note also that the model applies no round-trip efficiency loss: a kWh charged is a kWh
available to discharge. Real arbitrage loses 10–20% there, so the spread needed in
practice is higher than the figure above.

**A missing derived file is not an error.** `ensure_derived_inputs()` in the model builds
whatever is absent from `results/` before anything reads it, so every entry point — the
web interface, `python main.py --optimize`, or the model script on its own — works from an
empty `results/` folder without being primed first. The generators are imported only when
something really is missing, so the normal path costs a few `stat()` calls.

The **Rebuild derived inputs** button on the Run tab regenerates all of them regardless,
which is what to use when a derived file is present but stale. `data/order_trips.csv` stores
a fingerprint of the order dataset it was built from; as long as `order_dataset.xlsx` is
unchanged the routing is skipped and only the cheap generators re-run. Tick *Force
re-routing* to route again anyway - with the routing cache intact that is quick, without
it, it is the full ~40 minutes.

`data/cache_pv_profile.json` stores the same kind of fingerprint, over
`order_dataset.xlsx` **and** `depot_dataset.xlsx` (plus the plant and the days in
`order_trips.csv`). Unlike the trip set it **is** rebuilt on sight: one PVGIS seriescalc call
covers the whole year, so a stale curve is not left for the operator. Change either
Excel file in `inputs/` and the next prepare or model import refreshes the cache.

### Routes, chaining and the home depot

Without this the model has no geography at all: any trip may follow any other, and a truck
may plug in whenever it is not driving, wherever it happens to be. That is a fair reading
while trips are independent orders, and wrong as soon as the question is whether *one*
vehicle could physically run them in sequence.

`home_depot_location` names the one place the fleet is based, written the way the trip
locations are in `order_dataset.xlsx` so the two can be compared:

```
home_depot_location = '74635 Kupferzell Deutschland'
```

Everything spatial follows from that one string. **Depot charging and V2G are possible only
there** — a truck standing at a customer yard can use nothing but a public charger. Set
`route_chaining_status = 'off'` to restore the previous, geography-free model.

#### The same-place radius

Two locations count as one place when they are closer together than

```
location_tolerance_share x median trip distance of the loaded trip set
```

A share rather than a fixed distance because "the same place" scales with the journeys:
10 km apart is the same yard on a 200 km tour and two different towns on a 20 km one.

**Check this rather than trusting it.** In the example orders the median trip is ~109 km,
so the 10 % default is a ~11 km radius — wide enough that Untermünkheim, Waldenburg and
Niedernhall all merge into the Kupferzell depot, which takes the number of day-2 trips
"starting at the depot" from 11 to 17. That may be right for a yard with satellite sites
and quite wrong for a single gate. Every run prints the clusters it merged, so lower the
share until the merges are ones you would defend.

Clustering is greedy and frequency-ordered — the busiest location claims its radius first,
so the yard in half the orders becomes the representative. It is deliberately **not**
transitive: locations chained 0.9 radii apart land in different clusters. Single-linkage
would be transitive and would chain whole regions into one place, which for "is this truck
at its own depot" is the worse failure. A location that cannot be geocoded is its own
place; merging it would be an invented fact.

#### How a day becomes routes

`src/hdv_route_chaining.py` does the geography as preprocessing, so the MILP only ever
sees a small pruned candidate set. Two rules generate the chains:

1. **Direct chain** — `g` may run straight after `f` whenever `g` starts where `f` ends.
   No empty running at all. This is the case worth having.
2. **Nearest connection** — otherwise the closest trip that does not already start at the
   depot, one per trip by default (`route_nearest_link_candidates`). A truck finishing away
   from home either drives back or drives to the next job, and the next job worth
   considering is the closest one. Enumerating all of them would square the candidate set
   for chains no dispatcher would run.

Anything else routes through the depot, which is always available: a trip that does not
start at home gets an **approach leg** from the depot, and one that does not end there gets
a **return leg** back. Both are routed through the same service and cache the order data
uses; if the router cannot be reached for a leg the model itself invented, a straight line
× 1.3 stands in rather than failing the day.

#### What the MILP adds (3.3.16)

| variable | meaning |
| --- | --- |
| `route_start[m,f,s]` | `m` runs `f` from `s` as the **first** trip of a route, driving the approach leg before it |
| `route_end[m,f,s]` | ... and as the **last**, driving the return leg after it |
| `chain[m,f,g]` | `m` runs `g` directly after `f`, paying the empty run between them |
| `at_depot[m,t]` | 1 while `m` stands at the home depot |

Flow balance gives every assignment exactly one predecessor and one successor, so a route
is a path from the depot back to the depot and cannot be left open at either end. A chain
also has to survive the clock: `g` may not start until `f` has finished *and* the empty run
between them has been driven.

`route_start` and `route_end` are offered only at start times where the leg they carry
lands inside the hours that trip may be driven in (2.6c), which is what keeps the empty
legs inside the working hours as well as the loaded trips — the approach leg sits at
`s - approach_steps` and the return leg after the trip, so where those two binaries exist
is the only thing that decides where a leg may be placed. A chain between two in-hours
trips needs no bound of its own: it runs after `f` ends and before `g` starts, so it is
inside the window whenever they are.

`at_depot` is a balance rather than a lookup — the vehicle leaves when a route starts and
returns when a route ends, and nothing else moves it:

```
at_depot[m,t] = at_depot[m,t-1] - departures at t + arrivals at t
```

It is continuous, not binary: departures and arrivals are binaries and a vehicle is only
ever in one place, so the balance is integral wherever they are. The `0..1` bounds do the
real work — they forbid leaving twice without coming back, and coming home while already
home. Note it is 0 for **waiting between two chained trips** as well as for the driving,
because that waiting happens at a customer yard.

Empty running is real driving, so each leg is **a trip in its own right**: it occupies its
own steps (3.3.16g), spends its charge in them, and is drawn as its own bar. Its kilometres
are charged to the SoC balance, the diesel bill, the toll and the battery degradation
alike.

A leg needs no assignment variable of its own — `route_start`, `route_end` and `chain_from`
already fix when it runs, because a leg only happens as the consequence of an assignment.
Making the legs occupy time also closed a hole: 3.3.4 was written before they existed, so
nothing had stopped a truck being dispatched on a second trip during the very hour it was
repositioning for the first.

In `results/disposition_schedule.csv` and the disposition figure they appear as their
own activity:

| activity | meaning | label |
| --- | --- | --- |
| `trip` | driving under load | `T7` |
| `deadhead` | driving empty | `>7` approaching trip 7, `7>` returning from it, `7>9` running between two chained trips |
| `v2g_charge` | charging that funds an arbitrage resale — the *buy* half of V2G | |
| `v2g_discharge` | selling back — the *sell* half, labelled with its channel | |
| `parking` | idle **at the home depot** — chargers and V2G within reach | |
| `standby_away` | idle **at a customer yard** — neither within reach | |

`v2g_charge` is an **attribution**, not a model output. Charge in a battery is fungible and
nothing in the MILP distinguishes a kWh bought to drive on from one bought to sell back, so
the convention is the one arbitrage itself implies: per vehicle, take the metered kWh needed
to cover the day's arbitrage discharge (grossed up by both conversion efficiencies, since a
sold kWh has to be bought back with the losses on top) and attribute it to that vehicle's
**cheapest charging steps** until it is covered. The flexibility channel is excluded — it is
paid for a service rather than for energy, so its discharge is not the second half of a
purchase.

The last two used to be one grey bar. Standing at home and standing 200 km from it look
identical in a schedule and are not the same thing at all, and showing them alike suggested
a truck could have charged during a wait where it had nothing to plug into.

Runs report `routes_driven`, `chains_used`, `direct_chains_used`, `deadhead_km` split by
approach / return / chain, and `fleet_at_depot_share`. That last one is worth watching: it
is the entire window in which depot charging and V2G were possible at all, so it caps
everything the V2G business case can earn.

#### Known limits

- External (public) charging is not gated by location, which is the point — a driver can
  stop anywhere. It is no longer possible *during* a leg, though: 3.3.16g counts the legs
  among a vehicle's activities, so a truck cannot charge and reposition in the same step.
- Chaining candidates are pruned to direct matches plus the single nearest trip, and the
  nearest is chosen among those that could still fit in the clock — a chain no timetable
  could accommodate no longer consumes that one slot. A chain that is second-nearest but
  better on timing is still not offered; raise `route_nearest_link_candidates` to widen
  it, at a roughly linear cost in binaries.
- Closed loops need no elimination constraints. Flow balance alone would allow a cycle
  `f -> g -> h -> f` that never touches the depot, with every trip holding one
  predecessor and one successor and the vehicle showing as home all day while driving
  all three. Summing the chain timing bound around such a cycle gives
  `0 >= sum of durations and empty runs`, which no real trip can satisfy. Time does not
  run in circles, so the timetable does the work.
- A vehicle may arrive from one route and depart on the next in the same 30-min step.
  Modelling a minimum turnaround would be a policy decision, so none is imposed.
- Chaining and start-time sampling interact: a chain that would only work at a start time
  the draw discarded cannot be found. **`monte_carlo_samples_per_trip = 5` is the measured
  floor**, not a guess. Sweeping days 2 and 3 over three seeds each, 3 samples came back
  infeasible in 4 of 6 runs and 4 samples in 1 of 6, while 5 never did and was the only
  count to reach a 10 % gap inside 7 minutes. 6-8 chain better (11 chains against 8, ~50 km
  less empty running) but converge more slowly; 12 and above found no feasible solution at
  all inside 7 minutes, and 20 is already identical to exact because most trips have fewer
  start times than that.

  Below 5 the draw does not merely degrade the answer, it **fabricates an impossible one**:
  every trip can always be run from the depot and back, so a day reported infeasible has
  had the start times that make a consistent timetable removed from under it. Raise the
  sample count before believing an infeasible result. Note the draws are not nested, so
  more samples is better in expectation rather than monotonically.

### Drivers

`fleet_operation_mode` decides whether there is a driver at all. It is a **scenario
switch**, and running the same day both ways is what makes "what would this fleet cost if
it drove itself" a question the model can answer:

| mode | what applies |
| --- | --- |
| `crewed` *(default)* | Lenkzeit and Arbeitszeit limits (3.3.17), Lenkzeitpause in long trips and legs (2.6b), trips no driver could run legally removed (2.6c). The wage is rostered and reported, not optimised |
| `autonomous` | none of it — no crew limits, no driver cost, no mandatory breaks, nothing removed. A truck needing 13 h away and 11 h of driving simply drives for 13 hours |

Day 2, the same fleet and the same orders, both ways:

| | crewed | autonomous |
| --- | --- | --- |
| trips removed as undrivable | 5 (1899 km) | 0 |
| distance served | 3215 km | **5113 km** |
| drivers / driver salary | 10 / €1900 | 0 / €0 |
| energy + toll | €2270 | €3797 |
| longest unbroken absence | 10.5 h | 21.5 h |
| fleet at the depot | 63 % | 43 % |

> Measured before the shift limit became a hard constraint. The crewed column's 10.5 h
> absence is what the priced version allowed the solver to buy; it cannot occur now — the
> longest absence a crewed run returns is `driver_max_shift_hours` exactly. Every other row
> reads the same way it did.

The wage bill is the obvious difference and the least interesting one — and it is no longer
the one the optimizer sees, since the salary is reported rather than minimised (below). The
crewed fleet **cannot serve 1899 km of the day's work at all** from this depot — five Ruhr
loads whose approach leg alone is 6–6.5 h — while an autonomous one takes them without
comment. And because no shift limit binds it, the autonomous fleet is content to leave a
truck parked out for 21.5 h, which halves the window depot charging and V2G have to work in
(63 % → 43 % of vehicle-steps at home). Autonomy buys reach and wages; it costs the
discipline that was keeping trucks near their chargers.

Crew rules are **inside** the optimization (3.3.17) and the roster is still built afterwards
(`src/hdv_driver_scheduling.py`) from the schedule they shaped.

| parameter | default | meaning |
| --- | --- | --- |
| `driver_hourly_rate_eur` | 20 €/h | **reporting only** — prices the roster; not in the objective |
| `driver_max_driving_hours` | 9 h | **Lenkzeit** — hours actually driving in one driver's day |
| `driver_max_working_hours` | 9 h | **Arbeitszeit** — duty one person may perform, breaks excluded |
| `driver_mandatory_break_hours` | 0.75 h | the Lenkzeitpause, 45 min |
| `driver_max_shift_hours` | *derived* | **shift span** = working duration + mandatory break |
| `driver_shift_limit` | `hard` | the shift span is a constraint, not a price; `priced` restores the slack |
| `driving_time_before_break_minutes` | 270 | 4.5 h of driving before a break is due |
| `penalty_driver_use` | 20 € | tie-break towards fewer, longer shifts |
| `penalty_crew_rule_breach` | 500 € | per half-hour over the **driving** limits |
| `driver_roster_feedback` | `on` | one re-solve when the roster needs more heads than the peak |

#### The driver's time is optimised; the driver's pay is not

The wage is **not in the objective**, and that is a deliberate split rather than an omission.

A driver costs about 20 €/h and a truck is away for most of the day, so the wage bill was
the largest single term the solver carried — on the order of €1900 on day 2 of the example
order book, against tens of euros of V2G earnings and a similar order of battery wear.
Every effect this model exists to measure was therefore a rounding error on the term it was
measured beside: at a 10 % `MIPGap` the solver may leave €190 on the table, several times
the whole V2G business case, so a schedule that takes all of V2G and one that gives it up
entirely were indistinguishable to the search. And because the same trips need roughly the
same hours however they are arranged, most of what the wage contributed was a near-constant
offset that inflated the gap's denominator without steering anything.

What the wage *was* steering is kept, and kept as constraints rather than as a price:

- no absence longer than `driver_max_shift_hours` — a **hard** constraint by default, see
  the next section. This is what brings a truck home rather than leaving it parked in a
  customer yard, and it does it exactly instead of by price.
- no more than `driver_max_driving_hours` of driving inside one absence
- a 45-minute stop every `driving_time_before_break_minutes` of driving
- volume bounds on `drivers_needed` from the working-time and driving limits

The salary is still computed and still reported. `hdv_driver_scheduling` builds the roster
from the finished schedule, and `driver_cost_€` is what the operator pays for it — inside
`operating_cost_€` with every other real cost of the day. It is the one term of the
three-way split in **Conventions** that is money the solver never weighed:
`objective_€` does not contain it, `operating_cost_€` does, and `objective_residual_€`
closes because the reconciliation takes it back out.

`driver_cost_in_objective_€` survives with a narrower meaning: it is now the **head charge
alone**, `penalty_driver_use × peak concurrency`, which is a steering term and is counted
under `steering_penalties_€`. `driver_cost_gap_€` is therefore no longer "how far the proxy
missed the bill" — it is the whole bill less that steering charge.

Two consequences worth stating rather than discovering:

- `driver_roster_feedback` re-prices heads on the head count alone —
  `new price × peak = run price × roster heads`. The roster's cash bill used to be in that
  numerator, back when the proxy was meant to reproduce the wage; leaving it there would
  put the entire salary back into the objective through the head price, scaled by `1/peak`.
- an **asset-sizing** run chooses its fleet on `design_objective_EUR`, which carries no
  wage. Ownership and `penalty_driver_use` are the only pressure towards a fleet that needs
  fewer drivers. Where the wage is the thing being decided, compare designs on
  `design_operating_clean_EUR`, which does include the rostered salary.

#### The shift limit is a rule, not a price

`driver_shift_limit = 'hard'` (default) means the window constraint of 3.3.17a carries no
slack: **every duty block the run returns fits inside one lawful shift**, so
`driver_blocks_over_shift` is 0 by construction and a schedule no roster could staff is
simply not in the feasible set.

The escape hatch the priced version existed for is already covered upstream, and covered
better. 2.6c removes every trip whose *cheapest* shape — depot → trip → depot, nothing else
interfering — is longer than a shift, so after that filter every remaining trip can be run
inside one absence by one driver and a legal schedule always exists: one trip per absence.
The price was not buying feasibility. It was buying the solver permission to return a day
nobody could crew, which the run then reported in a warning that named no action — the
trips were legal, the schedule was not.

`at_depot` is continuous in [0, 1] but is an exact balance of the route-start and route-end
binaries (3.3.16d), so it is integral in any integer-feasible solution. That is what makes
the hard form safe to state on it: "at least one at-depot step in every window of
`max_shift_steps + 1`" really does mean the truck came home, not that twenty fractional
halves added up to one.

What it can still cost is a day where the shift limit and the fleet size collide. Every
trip is mandatory (3.3.1), so with too few trucks a day may need absences that chain
several trips together — and that now comes back `INFEASIBLE` rather than expensive. The
solver names this parameter when it does (4.3b). Re-run with `driver_shift_limit = 'priced'`
to test it: if that returns a schedule with over-shift blocks in it, the finding is that
this fleet cannot serve this day legally, and the fix is trucks or depots rather than a
solver setting.

The **driving** limits stay priced, and for the reason the shift limit no longer needs:
some trips cannot be crewed legally from one depot at all, and unlike an over-long absence
there is no legal rearrangement to fall back on. `penalty_crew_rule_breach` and
`crew_breach_h` are about those two limits now.

**Three limits, three different quantities, and only two of them are set.** `span >= duty
>= wheel`, so none implies the others: the span counts the depot waiting between two duty
blocks, the duty does not, and the driving counts neither that nor the loading, yard time
or Lenkzeitpause inside a block.

The span is **derived**, not stated:

```
driver_max_shift_hours = driver_max_working_hours + driver_mandatory_break_hours
                       = 9 h + 0.75 h = 9.75 h
```

because that is what it is — the span a driver is committed for is the work they may do
plus the break they must take while doing it. It used to be its own parameter at 12 h
against a 10 h working limit, which made it a second opinion about the same day: the MILP
accepted a 12 h absence the roster could not give to anybody who did anything else.
Deriving it means an absence the optimisation accepts is always one a single driver can
lawfully cover, which is the premise the whole crew side rests on. Setting it directly has
no effect — `build_runtime_context()` recomputes it on every build, exactly so the three
limits cannot drift apart again.

`driver_mandatory_break_hours` is likewise the *only* statement of the break. It used to be
`driving_break_duration_minutes` as well, and two numbers for one physical fact can only
drift: the same 45 minutes is the rest a long trip or empty leg carries inside it (2.6b),
the penalty-free window it opens at a public station (3.4), and the gap between working
duration and shift span. The minutes are derived from the hours.

**Off-grid limits floor rather than round.** 9.75 h is 19.5 steps, and which way that half
step goes decides whether the model is stricter or looser than the rule. The MILP used to
round — 20 steps, a 10 h absence against a 9.75 h limit — which also left it *more*
permissive than the roster, which floors the same figure to 19. Both floor now, which is
also the direction every other limit in this file leans: never allow more than the rule
does.

**A Lenkzeit larger than the Arbeitszeit containing it is rejected**, with both figures
named. Driving is part of the working time, not additional to it, so that pair could never
bind the driving limit and the run would quietly plan days no driver may work. The
interface caps the driving input at the working duration so the combination cannot be
reached from the UI at all.

Tightening the limits cost nothing in served demand: over days 1-30 of the example data the
same four trips (1537 km) are removed by 2.6c under the old 10 h span and under the new
9.75 h one, because every trip that fails the span was already failing the 9 h Lenkzeit.
The binding limit here is the driving, not the spread.

**The Lenkzeit is enforced in the roster, and it has to be.** The MILP caps driving per
*absence*, with a counter the depot resets — correct only while one absence is one driver's
day, which is a promise only the roster can keep. It is the roster that decides whether two
absences share a driver, so it is the roster that checks the daily driving, using the
model's own `drive_since_depot` figures rather than a second implementation of them.

**Why waiting in a customer yard still costs the fleet something.** A driver is tied to a
vehicle exactly while it is away from the depot, so `away` is the driver's clock — and the
shift limit is a hard bound on how long that clock may run. An hour spent standing in a
yard is an hour of the only span a driver has, so the truck has to come home to get
another one. That used to be a wage in the objective; it is a constraint now, and the
constraint is the half that was doing the work. Nothing had made idleness away from home
cost anything before either version.

**The Lenkzeitpause is built into durations (2.6b), not enforced step by step.** A 6 h run
is a 6 h 45 min job, because the driver must stop. The break is added to the *occupancy* —
the truck is standing at a rest area — but not to the driving, which is why
`trips_driving_steps` is tracked separately from `trips_duration_steps`. Empty legs long
enough to need a break carry one too.

**The driving limits are priced, not hard.** Some trips cannot be crewed legally from one
depot at all: day 2 of the example order book has five whose approach leg alone is 6.5 h,
making depot → trip → depot 13 h of absence and 11 h of driving before anything else is
scheduled. A hard constraint answers that with a bare "infeasible" naming nothing. At
`penalty_crew_rule_breach` the solver breaks the Lenkzeit and the Lenkzeitpause only where
no legal schedule exists, and `crew_breach_h` reports every half-hour it had to. The
**shift** limit is not among them — see *The shift limit is a rule, not a price* above.

**Undrivable trips are removed before the solve (2.6c).** A trip's cheapest possible shape
is depot → trip → depot with nothing else interfering. If even that breaks the Lenkzeit or
the Arbeitszeit, no schedule can fix it and every route through that trip will break the law
whatever else the fleet does. Keeping one is worse than wrong: it would put a fixed
unavoidable penalty in the objective, or — now that the shift limit is hard — make the
whole day infeasible on one trip. Each removal is named at the end of the run with the
figure that disqualified it - in the example data, five trips of one day, all of them loads out of the
Ruhr whose approach leg alone is 6-6.5 h.

This filter is also what makes the hard shift limit safe: after it, every trip left in the
model fits one absence on its own, so a legal schedule always exists.

**The run solves twice (2.8a).** The crew constraints tie every step of a vehicle to every
earlier one, which leaves the LP relaxation a poor guide and makes *finding a first feasible
schedule* the hard part rather than closing the last few percent. So the day is solved once
without 3.3.17 — quick — and the result handed to the constrained solve as a MIP start. Both
builds take identical arguments, so the variable sets match by name and the hand-over is a
straight copy; variables the relaxed model lacks (the crew slacks, the driving counters)
simply have no start value and Gurobi completes them.

`MIPFocus=1` used to be set alongside it, on the same reasoning. It no longer is — see
**Search effort** below, which is a measurement rather than an argument.

**The crew side is a sequential decomposition, and the seam is reported.** The vehicles are
optimised, then the people are fitted to the result. The objective counts crew by proxy — a
flat charge on the day's peak concurrency — and the roster is then built under limits *per
person* that the proxy cannot express. Peak concurrency is only a lower bound on the head
count: what actually decides it is packing indivisible absences under the duty and driving
limits, and a 9 h absence cannot be split between two people however much slack the totals
leave. Day 1 of the example data rosters four drivers against a peak of two.

Three things follow, all of them in the output rather than in a footnote:

- `driver_cost_in_objective_€` is reported beside `driver_cost_€`, with `driver_cost_gap_€`
  between them. The first is the head charge the objective carried, the second the salary
  the operator pays.
- `drivers_beyond_model` counts the heads the roster needs past the peak the objective was
  charged for.
- the MILP carries two volume bounds on `drivers_needed` — total away hours over
  `driver_max_working_hours`, total driving over `driver_max_driving_hours`. Both are valid
  (no driver absorbs more than their own limit) and both are one constraint. They do not
  close the gap and are not meant to: a bound on totals cannot express indivisibility. What
  they remove is the class of day where the model believed two people could absorb thirty
  hours of duty. Before this, `driver_max_working_hours` had no presence in the optimisation
  at all.

**And one re-solve, when the roster disagrees (`driver_roster_feedback`).** If the roster
needs more heads than the peak the objective counted, the head charge is re-priced so that
`new price × peak = configured price × roster heads`, and the day is solved once more. Both
schedules are then scored the same way — each one's objective with its own head proxy
removed and the run's own per-head price applied to the heads its roster actually needs —
and the better is kept. It cannot make the answer worse, because the first schedule is one
of the two candidates and the comparison is on the measure that matters.

The roster's cash bill used to sit in that numerator, back when the proxy was meant to
reproduce a wage the objective carried. It does not any more, and putting it back would
reintroduce the entire salary through the head price, scaled by `1/peak`.

It is a heuristic: one iteration, no convergence argument, and re-pricing the peak is a
lever on the packing rather than a model of it. What it is not is a substitute for modelling
drivers properly. That needs driver-indexed assignment variables, which multiply the model
by the fleet size, and the design scaling measured in **3.5** shows there is no room for
that — the horizon model already fails to finish a root node at three days.

The head price the surviving schedule was actually built with travels into the results as
`penalty_driver_head_price_€`, so a re-priced run is visible rather than silently different
from its own configuration.

The effect on day 2 of the example order book, when the crew rules first moved inside the
optimization, was the difference between a usable model and an unusable one:

| | before | after |
| --- | --- | --- |
| result | 78 % gap after 25 min | **optimal, 38.8 % gap in 5 min** |
| crew limits broken | ~150 h | **0 h** |
| blocks longer than a shift | 8 of 17 | **0 of 18** |
| fleet at the depot | 42 % | **63 %** |

That last row is the point of the whole exercise: held to one lawful shift per absence, the
model brings trucks home instead of leaving them idling in customer yards, and depot
charging and V2G get half again as much window to work in. The "after" column was measured
while the wage was still in the objective; the mechanism behind it — an absence a driver
can cover — is now the hard shift limit rather than a meter running on somebody's hours, so
the third row is 0 by construction rather than by incentive.

**Search effort — `optimization_MIPFocus` (1.3).** Where Gurobi spends its time is a
parameter now, and it is set to 0 (balanced) rather than to 1 (hunt incumbents). The
argument for 1 was the one made just above — under the crew rules, finding *any* feasible
schedule is the hard part — and it was right about the model it was written for. With V2G
on it is not: the incumbent arrives in the first seconds and then barely moves while the
dual bound crawls, so pushing harder on incumbents aims at the half of the gap that is
already closed. Measured over two days and three random seeds each:

| | `MIPFocus=1` | `MIPFocus=0` |
| --- | --- | --- |
| day 2, median of 3 seeds | 256 s | **126 s** |
| day 2, spread across seeds | 72 % | **17 %** |
| day 95, reached the 10 % target in 1500 s | **0 of 3** | **3 of 3**, ~450 s |

The variance matters as much as the median: a 132-day sweep is a sum over runs, and it is
the tail that hurts. The price is a schedule 0.6-1 % dearer, because closing the gap from
the bound side polishes the incumbent less — but on day 95 the trade runs the other way,
since the old setting's cheaper-looking incumbents were never certified within the target
at all. Set it back to 1 for a crewed fleet with **V2G off**, where feasibility is the
binding difficulty again; that case is outside what was measured.

> **These figures are historical and do not reproduce on the model as it stands.** They
> were taken before the September 2026 changes and on inputs that have since moved. Two
> things are now wrong with them rather than merely old:
>
> - **Day 95 is infeasible.** With the example 1 ice + 10 bev roster it returns
>   `INF_OR_UNBD`, which `DualReductions = 0` resolves to INFEASIBLE, and it stays
>   infeasible at 14, 18, 24 and 30 bev — so it is not a fleet-size problem. The same holds
>   for every day of 45–63 trips tried, which is the median day of the set. Only days of
>   1–10 trips produce a schedule at all; days of 12–30 trips are feasible but find no
>   incumbent inside 45 s.
> - **Day 2 does not reach 10 % in 126 s.** Re-measured at the current code and inputs, no
>   setting reached the 10 % target on day 2 within 900 s; three seeds landed at 16–21 %.
>
> `MIPFocus = 0` and `DegenMoves = 0` are still the right settings — they were re-validated
> on Gurobi 13 — but the *timings* above should be read as a record of a decision, not as
> numbers to compare a run against, and re-measuring is the only way to get figures that
> describe the model as it stands.

**Degenerate simplex moves — `optimization_DegenMoves` (1.3).** Off. A time-indexed
schedule is massively degenerate: swapping which of two interchangeable trucks takes a slot,
or which of several equally-priced half hours a charge sits in, changes the simplex basis
without changing the schedule or its cost. The solver can spend its iterations walking
between bases that all describe the same answer — 125 simplex iterations per node in the
original log is what that looks like. Switching the moves off stops it.

Measured as an A/B with both halves run back to back on the same seed — this machine
slows by roughly a factor of two under sustained load, so a comparison whose halves are
hours apart measures the thermal state as much as the parameter:

| seed | `DegenMoves=-1` | `DegenMoves=0` | speed-up |
| --- | --- | --- | --- |
| 1 | 225 s | 148 s | 1.52× |
| 7 | 202 s | 166 s | 1.21× |
| 13 | 301 s | 148 s | 2.04× |

**Median 1.52× on day 2, 1.25× on day 95, 3 of 3 seeds in favour, objective +0.12 %.**

The mechanism is *cheaper nodes, not fewer of them*: on day 2 the setting explores **more**
nodes than the default (7,078 against 5,981) at roughly **twice the rate** (90 against 44 per
second). Node counts are the only hardware-independent number in this study, which is why
they are quoted alongside the times.

**Do not pair it with `ImproveStartGap`.** Together they were the fastest thing measured on
day 2 (1.63×) and *slower than `DegenMoves=0` alone* on day 95, because `ImproveStartGap` is
a 0.89× regression there. It is the combination that looks best on one instance and loses on
the other, which is the whole reason both are measured.

Two things that did *not* help, measured rather than assumed, so they are not tried again:
finite bounds on the energy variables (0.80x), aggressive presolve (0.75x), concurrent MIP
(0.68x / 0.34x), aggressive symmetry detection (no effect), and linearizing the SoC aging
weight to escape the non-convex MIQCP class (0.88x — it does remove all 235 bilinear terms,
but a MILP with 1880 extra binaries is not faster here).

A second sweep of 21 further options added: `OBBT`, `PreQLinearize`, `PreMIQCPForm` and
`Disconnected` change nothing at all (identical node counts — `PreQLinearize` only acts on
products involving *binaries*, and these 235 are continuous × continuous); `MIPFocus` 2 and 3
are both worse than balanced; strong branching is 0.49x; barrier at the nodes does not
converge. Gurobi's own tuner managed two candidate settings in 928 s — it is built for models
where a solve takes seconds — and its one suggestion had a standard deviation larger than the
improvement it claimed.

The raw measurements behind the paragraphs above were never committed to this repository —
The raw measurements behind the paragraphs above are not in this repository - `benchmarks/`
has been referenced here for a long time and has never been committed. Treat every timing
in this section as a record of a decision that was taken, not as a figure to reproduce.

The roster afterwards. That split is deliberate: which truck runs which trip is an energy and cost decision the
MILP is built to make, while crewing the movements that result is a rostering problem that
follows from it and would only make the MILP larger to no purpose.

The method rests on one observation: **a driver is tied to a vehicle exactly while that
vehicle is away from the home depot.** A truck on a charger needs nobody; a truck on the
road, waiting in a customer yard, or repositioning empty needs somebody who cannot leave it
until it is home. So the day splits into *duty blocks* — one per continuous absence — and
each is indivisible. The operator's rules then follow directly:

| rule | how it falls out |
| --- | --- |
| drivers change vehicles at the home depot | blocks are separated by depot time, so any free driver may take the next block on any vehicle |
| a driver works at most `driver_max_shift_hours` | measured as the span from signing on for the first block to signing off after the last |
| breaks are taken at the depot | the gaps between one driver's blocks are exactly that, and they are at the depot by construction |

Blocks are packed into shifts by a greedy best-fit: taken in start order, each goes to the
free driver whose span it grows least. Covering interval jobs under a per-worker span limit
is bin-packing, so this is a heuristic; the run also reports `drivers_lower_bound`, the most
vehicles away at once, which no roster can beat. Where no shift limit binds the two agree
and the greedy is optimal.

`driver_hourly_rate_eur` (20 €/h) is paid on the **shift span**, so a break at the depot
inside a shift is paid and no minimum shift is applied. Charge `driver_driving_h` instead if
the contract pays by the wheel.

Two things to keep in mind when reading the figures:

- **The salary is reported, not minimised.** It is computed from the finished schedule, so
  the optimizer never traded driver hours against energy — it could not see them.
  `driver_cost_€` is what the chosen schedule implies, and changing the hourly rate changes
  that number and `operating_cost_€` without changing the schedule. What the optimizer *did*
  see is the driver's time: the shift limit, the Lenkzeit, the Lenkzeitpause and the volume
  bounds on the head count.
- **A block longer than one shift cannot appear.** `driver_blocks_over_shift` is 0 by
  construction under the default `driver_shift_limit = 'hard'` — an absence that long is not
  in the feasible set, so a non-zero count means either the run was made with `'priced'` or
  the schedule was read back wrongly. Under `'priced'` the old behaviour returns: the block
  still gets a driver and is still paid for, because the work exists and dropping it would
  report a cost that does not pay for the day.

Output: `drivers_required`, `driver_cost_€`, `driver_paid_h` / `driver_driving_h` /
`driver_break_h`, `driver_vehicle_changes`, `driver_blocks_over_shift`,
`driver_shift_limit`, a per-block roster in `results/driver_schedule.csv`, and — on a
disposition run — `results/plot_driver_schedule.png`.

That figure answers **who drives what and when**: one row per driver, one cell per
30-minute step, the vehicle number standing in the cell, so a driver changing trucks at
the depot is a change of number down a row rather than a number in a table. Paid time at
the depot with no vehicle is a `B`, off-duty time a dash, and any block longer than a
legal shift is red. Not drawn for an autonomous fleet, which has duty blocks but nobody
to put on the rows.

The earlier two-panel Gantt — the same rows as bars, over a count of who is on duty at
each moment — has been removed; the grid figure below replaced it. The
duty count is the one thing it showed that the grid does not: a count over time is a
curve, and a grid has no room for one.

## What the interface provides

- **Two ways to run**, as the first two tabs. **Disposition** plans one operating day
  against the roster as given, with auto-sizing off. Its one control is the **date**, and
  that single setting picks all three things that have to agree: the trips of that day
  (matched on `trip_date` in `data/order_trips.csv`), the depot's metered baseline load for
  that day and month, and the PV yield PVGIS reports for it. Only dates the order data
  covers can be chosen - the example set has 132, 02.01. to 02.07.2024, and the 51 gaps
  inside that span are all weekends; pick one and the tab says so and names the nearest
  dates that do have trips.

  **Asset Sizing** answers *what should we buy*. Its day-range slider is its only run
  control, and **one fleet is chosen for the whole range**: what to buy is a single decision
  shared by every day, and how to run each day is a separate set of variables under it (see
  *Auto-sizing* above). Each day brings its own date, so the depot load and the PV yield
  always belong to the trips being sized against — a range spanning seasons is sized against
  each of those seasons. There used to be a second field picking one day out of the range,
  and before that a free date picker, which made it possible to size a January order set
  against July sunshine.

  **Scenario Sweep** answers *what would this cost under other assumptions*. It leaves the
  fleet alone and sweeps two ways at once: over the days of the range, and over four
  scenario axes — **scenario years**, **cost scenarios** (best/worst case), **V2G** on/off
  and **external charging** on/off. Every combination is one full MILP solve, so the count
  is the product of all of them; the tab states it before the button and warns past five.
  Each axis defaults to the single value the sidebar already carries, so the tab opens as a
  plain day sweep and only widens when asked — which keeps a sweep a comparison, with one
  thing moving at a time unless you ask for more.

  Past **five solves** the figures and the schedule CSV are turned off for that run whatever
  the switch says: every solve writes over the previous one's files, so all but the last
  would be drawn and thrown away. The switch stays live and still decides at five or fewer.

  Results gets one row per solve, with a column for each axis that actually moved — an axis
  held fixed is not shown, since a column of one repeated value is noise in a table whose
  point is the comparison. The schedule CSV and the figures are files in `results/` that each
  solve overwrites in turn, so those are always the last one — run that day on its own under
  **Disposition** to see its schedule.

  Everything all three tabs share stays in the sidebar.
- **The tabs read as steps, and reading the first one is a step.** `1 · Start Here ·
  2 · Inputs · 3 · Settings · 4 · Run · 5 · Results`: numbered because the order is not
  obvious from the names — the inputs have to be there before the settings mean anything,
  the settings before a run, and a run before there is a result to read. *Start Here* takes
  the first number rather than sitting outside the sequence, so a reader who has not used
  this before lands on it and is told to read it rather than invited to skip it. The tabs
  are styled as buttons — full width, split evenly, large enough to be the obvious next
  thing to click. `.streamlit/config.toml` sets a dark base and one
  accent colour, and a small CSS layer in `_apply_theme()` adds the translucent panels —
  metrics, tables, expanders and alerts let the page's own background through instead of
  stacking opaque grey on grey. All of it is presentation: every selector is allowed to
  miss, so a Streamlit release that renames a test id costs the styling and nothing else.
- **The fleet and the chargers are listed, not summarised.** Both are datasets rather than
  settings, so the sidebar shows every vehicle (id, type, kWh, kW, consumption) and every
  station (id, kW). Which truck has which battery and which charger can take it is what
  decides whether a plan is possible; "10 vehicles @ 50 % bev" is precisely the part that
  does not help.
- **Closing the browser tab stops the server.** `streamlit run` is a server and would
  otherwise outlive its tab, leaving `python main.py` hanging and a Gurobi licence held for
  nobody. A watchdog polls the session manager and exits once the last client has been gone
  for ten seconds — long enough to ride out a page refresh. It waits for a first browser
  before arming itself, so a headless start is unaffected, and if a future Streamlit moves
  the private API it touches, it gives up quietly and the old behaviour returns.
- `.streamlit/config.toml` sets `toolbarMode = "minimal"`, which removes the Deploy button.
  This runs against a local licence and local datasets; there is nothing to deploy.
- Sidebar controls for the full single-run parameter set: trip day, disposition date,
  working hours, scenario/year, V2G, solver gap, feature flags, prices, tolls,
  degradation and penalties. Three things are deliberately not among them, because they
  are datasets rather than parameters and are shown read-only: the fleet (the roster of
  `fleet_dataset.xlsx`), the PV plant and the charging infrastructure (the `generation`
  and `charging` sheets of `depot_dataset.xlsx`).
- **Disposition date** — the calendar date of the dispatched day. It is never typed in:
  both ways of running take it from the trip data, so it is the date of the day being
  planned. It selects the day of year the depot PV curve is generated for and the day of
  the metered year the baseline load is read from, which is what makes a run a winter or a
  summer day. **2 · Inputs** plots that curve against the depot's own load on request, for
  any date you like — that preview is its own thing and never reaches a run.
- **Working hours** — *Earliest trip start* and *Latest trip end* bound the driving day on
  the 30-min grid, `06:00`–`18:00` by default. They are a **preference, not a curfew**:
  each trip's own window from the order data is narrowed to them wherever that still
  leaves the trip somewhere to run, and a trip that cannot be fitted inside them keeps its
  own window instead of making the day infeasible. So an early-morning delivery that has
  to be done by 07:00 still runs before 06:00. The run names every trip that had to fall
  back — `trips_outside_work_hours` in the result table, and a line in the run summary —
  so the exception is never silent. Only driving is restricted at all; depot charging and
  V2G are available around the clock. Set `00:00`–`24:00` to impose nothing.

  The hours hold the **empty legs** too, not only the loaded trip. A trip is the middle of
  what the truck actually drives — it pulls out of the depot an approach leg before the
  trip starts and gets home a return leg after it ends — and those used to be bounded by
  the calendar day alone, so a 06:30 pick-up five hours out had the truck leaving at 01:00
  inside a `06:00`–`18:00` day. A trip is now held to the hours only if the *whole*
  depot-to-depot excursion still fits inside them at some start time; one whose legs
  cannot be fitted keeps the whole day for them and is named in the run summary, the same
  preference-not-curfew rule the trip windows follow. That test deliberately ignores
  chaining: letting a trip keep the hours on the strength of a chain partner would rest
  its only way home on a chain the solver may not be able to use. Expect a slightly dearer
  day when the window is tight — the legs have less room to be placed cheaply.
- **Home depot & routes** — *Home depot location* names the one place the fleet is
  based, and it is the only place depot charging and V2G are possible. *Chain trips
  into routes* turns the geography on; *Same-place radius* sets how close two
  locations have to be to count as one. See the routing note above.
- **Conversion losses** — *Charging efficiency* (97 %) and *Discharging efficiency* (93 %)
  are two separate sliders, one per direction, and the sidebar shows the resulting round
  trip beneath them. They are the only link between the metered energy the run bills and
  the battery energy it schedules; see the modelling note below for the measurements they
  come from. Set both to 100 % for a lossless run.
- **Battery day boundary** — *Day start & target end SoC* is one figure for both ends of
  the day, 50 % by default. Every bev starts at 00:00 with that share of its own capacity
  and has to be back at it by 24:00, so driving and V2G discharge both have to be charged
  back before the day closes and the schedule cannot be paid for by running the batteries
  down overnight. Ending above the target is allowed, ending below it is not. The first
  half hour is a step like any other: `x_m_SoC[.,0]` is the level *after* step 00:00–00:30,
  not the day-start level, so energy moved in that step reaches the battery.
- **Disposition figure** — `results/plot_disposition_schedule.png` always spans the
  full day, `00:00`–`24:00`, whatever the working hours are. Charging and V2G run around
  the clock and trips may fall outside the window, so a narrower frame would hide activity
  and suggest an idle fleet where there is none.

  Colour is the activity; the two characters in a cell are the detail the colour leaves
  out. A trip shows its number and an empty leg where it is heading (`>7` approaching trip
  7, `7>` home from it). V2G discharging names the channel it settled in — `AD` arbitrage,
  `FD` flexibility — and the arbitrage buy that funds a resale is `AC`. Depot charging
  names **where the energy came from**: `PC` the site's own PV, `VC` another truck (V2V),
  `GC` the grid. That last one is per step and fleet-wide, because the model decides how
  much of a half hour's charging the sun covered and never which truck stood in it — so
  every truck charging in the same step carries the same letter, and the letter is the most
  valuable source that supplied at least a fifth of the step. It says which, never how
  much; `pv_charging_kWh`, `v2v_kWh` and `grid_charging_kWh` in the run summary are the
  quantities. `PC` is rarer than the legend suggests: on the example depot PV averages
  about 9 % of a charging step, so most steps are carried by V2V or the grid.
- **V2G depth is bounded by the battery, not by a quota** — a step may discharge what the
  inverter delivers in half an hour (`P_v2g x 0.5 h`); how deep the battery may be drawn
  follows from the SoC balance and the 24:00 target. The former `v2g_allowed_battery_usage`
  quota is gone: as a per-step cap it was inert for any value above ~44 % of capacity, and
  it never expressed the SoC window its name promised.
- **A trip is checked against the SoC it departs with** — the level at the end of the step
  before it starts, or the day-start level for a trip leaving at 00:00. Testing the SoC of
  the starting step instead asked for the trip's energy *plus its own first half hour*,
  since that level is already net of the driving in it — about `trip_energy / duration`
  too much, and double for a one-step trip. The reserve kept trucks off trips they could
  finish; the full usable range is now available.
- **`E_pos` and `E_neg` are the battery flow** — they are pinned to it by the identity in
  3.3.11c, so every kWh of V2G earnings and every kWh of degradation is charged on energy
  that actually left a battery. The earlier one-sided bounds (`E_pos >= x_E`,
  `E_neg >= -x_E`) let both exceed the real flow, and since the objective *pays* for
  `E_neg` the solver pushed it to its cap while the battery stood still.
- **Conversion losses are one parameter per direction** — everything the model buys, sells
  and peak-shaves (`E_private`, `E_public`, `E_neg`) is energy **at the meter**; everything
  it stores (`x_m_SoC`, `x_m_t_E`) is energy **in the battery**. Constraint 3.3.11c is the
  only place the two frames meet:

  ```
  charging_efficiency · E_pos  −  E_neg / discharging_efficiency  ==  x_m_t_E
  ```

  So a drawn kWh arrives as `charging_efficiency` kWh of charge, and putting a kWh on the
  grid costs `1 / discharging_efficiency` kWh out of the pack. Setting both to 1.0
  reproduces the lossless model exactly.

  | parameter | default | direction |
  |---|---|---|
  | `charging_efficiency` | **0.97** | meter → battery |
  | `discharging_efficiency` | **0.93** | battery → meter |
  | round trip | 0.902 | |

  The two are deliberately **not** the same number, and the asymmetry is the point: the
  0.902 round trip is the spread a V2G slot has to beat before it earns anything, and it is
  also why degradation is charged on `E_neg / discharging_efficiency` rather than on `E_neg`
  — the cells give up more than the grid receives.

  Sources for the defaults:

  - **Sevdari, K., Pedersen, K. L., Fabbri, G., Knudsen, R. M., Alami, A., Engelhardt, J.,
    Marinelli, M. (2025).** *Efficiency of bidirectional EV charging: Key insights from
    implementing ISO 15118-20 CCS2 and CHAdeMO.* Sustainable Energy Technologies and
    Assessments **83**, 104654. [doi:10.1016/j.seta.2025.104654](https://doi.org/10.1016/j.seta.2025.104654).
    Measured on a bidirectional DC charger at the terminals: **97 % charging and 93 %
    discharging at rated current**, collapsing to 80 % / 60 % at 2 A. These are the defaults
    above — one source, both directions, same hardware and same method, so the asymmetry is
    measured rather than assembled from two unrelated papers.
  - **Apostolaki-Iosifidou, E., Codani, P., Kempton, W. (2017).** *Measurement of power loss
    during electric vehicle charging and discharging.* Energy **127**, 730–742.
    [doi:10.1016/j.energy.2017.03.015](https://doi.org/10.1016/j.energy.2017.03.015). The
    first end-to-end measurement of V2G losses and the source that established the
    asymmetry: **83.5–99.2 % charging against 78.2–91.7 % discharging** on the same vehicle,
    with the losses concentrated in the power electronics and worst at low power and low
    SoC. Its headline round trip of 53–62 % is *not* used here — it was measured on a 2017
    single-phase onboard charger run mostly far below its rating.

  Taking the **rated-point** figures rather than a part-load curve is a property of this
  model, not a convenience: depot charging is pinned to full power (see below), so a
  plugged-in truck is never in the part-load region where both papers find the efficiency
  collapsing. The exceptions are the 1 kWh floors on public charging and on a V2G slot,
  small enough that the error sits well below the MIP gap.

  Caveat for any write-up: both measurements are car-scale converters (11 kW and ~10 kW).
  No peer-reviewed measurement of a *bidirectional megawatt-class truck* charger exists yet.
  The values transfer because a truck-scale DC charger here runs at or near its rated
  point, which is where converter efficiency is highest - and they are converter figures,
  so the pack's own internal losses are not included in them. They are defaults, to be
  replaced if you have measurements for your own hardware.
- **An occupied slot has to move energy** — every assignment is gated from **both** sides.
  A V2G slot: `E_neg <= E_v2g_max · z` and `E_neg >= v2g_min_discharge_kWh · z`; external
  charging likewise, with `charging_min_energy_kWh` as the floor (1 kWh per 30-min step by
  default, capped by what the truck can take). With only the upper bound, `z = 1` with no
  flow was feasible — a V2G slot doing no V2G, a truck blocking a charger it never draws
  from — and since it cost nothing the solver set such slots arbitrarily. The two-sided
  form makes an assigned slot a real one by construction, whatever the MIP gap. Claiming a
  slot stays optional; the floors only say what claiming one means.
- **Depot charging runs at full power, never throttled** — a depot charger does not
  modulate, so `E_private` is *pinned* to the lowest of the three limits rather than
  bounded by them:

  ```
  E_private[m,t] == min( truck power , station power , what the battery can still take ) · z
  ```

  The first two are one constant per truck, `min(P_ch[m], strongest station) · 0.5 h`. The
  two battery limits collapse into a single linear term: filling a headroom of `cap − SoC`
  takes `(cap − SoC) / charging_efficiency` at the meter, the charging-curve derate is
  `K · (cap − SoC)` with `K = E_step_max / (0.2 · cap)`, so the tighter is
`min(1/charging_efficiency, K) * (cap - SoC)` and no case distinction is needed. Which of
the two binds is a property of your fleet rather than of the model: whenever a truck can
move more than about a fifth of its pack in one 30-min step - high charging power against
a small battery - free capacity binds first and the 80 % taper never actually bites. Check
it against your own `charging_power_kW` and `energy_storage_kWh` before reading anything
into the taper; on the example fleet it does not bite.

  Stating `min(constant, linear)` exactly costs one binary per bev per step. It is not a
  free choice: with the binary on one side the station limit is forced and the upper bounds
  make that infeasible unless it really is the smaller, and the other way round — so
  feasibility pins it, the solver does not select it. Step 0 needs no binary, since the
  day-start level is a constant.

  This makes `charging_min_energy_kWh` redundant on the depot side — an equality at full
  power is a far stronger statement than a 1 kWh floor — so that floor now applies only to
  external charging.

  Because the rule is per truck against the *strongest* station, it agrees with the
  fleet-level greedy bound only while every station a truck can occupy is at least as
  strong as the truck. A truck on a weaker station would charge at *that* station's power,
  and the greedy rule does not fix which truck lands there — the two statements would
  contradict and the model would return infeasible with no explanation. The model checks
  this up front and fails with the offending station powers named.
- **Occupied slots are priced** — `penalty_charging_use` is charged per 30-min slot in
  which a vehicle occupies a charger, charges externally **or is assigned to V2G**, all at
  the same rate, so claiming a slot is never free.
- **Public charging costs energy *and* time** — a truck may charge away from the depot at
  `public_charging_price_€/kWh` from `costs_dataset.xlsx`. That energy is billed at the
  external rate and, unlike depot charging, does **not** pass the depot meter, so it never
  raises the site peak. On top of the energy it costs the driver time, priced per minute by
  `penalty_charging_external_time` (*Strafe pro Minute externes Laden*, €10/min by
  default — at 30 min per step that is €300/step, so it dominates the energy price).

  The penalty is **waived inside a Lenkzeitpause**, the driver's statutory rest break: the
  truck stands still regardless, so the charging time is not an extra loss. Two parameters
  describe the break, both editable in the sidebar and both set to their statutory values
  under EU Regulation 561/2006:

  | Parameter | Default | Meaning |
  | --- | --- | --- |
  | `driving_time_before_break_minutes` | 270 (4.5 h) | *Nach welcher Fahrdauer* a break is due |
  | `driver_mandatory_break_hours` | 0.75 (45 min) | *Dauer* of the break, and so of the free window. Set under **Drivers**, because the same break is also what separates a driver's working duration from their shift span |

  A trip opens a penalty-free window of the break's length at its end, but **only if its
  own driving time reaches the threshold**. The window used to open after every trip
  however short, which handed the fleet free public charging after a 30-minute run — the
  break is the reason the penalty is waived, and there was no break.

  Two limits worth knowing. The test is **per trip**, not on driving accumulated across
  trips: three two-hour trips are six hours of driving and would oblige a break in
  practice, but none reaches 4.5 h alone, so none opens a window here. Tracking
  accumulated driving needs a per-vehicle counter that breaks reset — a scheduling problem
  of its own. And in the example order data no trip exceeds 2.8 h, so **no window ever
  opens** and every minute of public charging is charged in full.

  `break_active` is pinned from **both** sides. It only ever relieves a penalty and carries
  no cost of its own, so with lower bounds alone the solver set it to 1 everywhere and the
  external-time penalty fell away entirely — runs charged freely at public stations while
  nominally paying €10/min for it. The upper bound `break_active <= Σ z` over the trips
  that could open the window makes it a real disjunction.
- **Activity labels** — a step counts as `charging_in` or `v2g_discharge` only if energy
  actually moved in it (`E_pos` / `E_neg` above zero); everything else on site is
  `parking`. This is the guarantee that the disposition figure and the SoC figure agree,
  and it holds no matter how loose the MIP gap is. The slot penalty above makes a stray
  assignment cost money, but €1 a slot is small against a 10 % gap on a four-figure
  objective, so the solver may still leave some inside its tolerance — the labelling does
  not depend on that.
- **SoC figure** — `results/plot_SoC_charging_power.png`, one row per bev over the full
  day. The colour is the state of charge, on a scale pinned to 0–100 % so two runs are
  comparable; the number in each cell is the metered power in kW, signed `+` for charging
  and `-` for V2G discharge. One figure rather than two because *how full* and *how hard
  the battery is being pushed* are always read together. A dash on a coloured cell is a
  battery at that level and not plugged in.
- **Depot power overview** — `results/plot_depot_power_overview.png` puts everything that
  meets at the grid connection on one axis: the depot's own consumption, its PV
  generation, the aggregated charging-infrastructure power, the V2G discharge, and the
  resulting grid draw **with and without the bev fleet**. Negative means the depot feeds
  the grid rather than drawing from it. The "without" curve is the same site with no
  charging and no V2G — site load minus PV — so the gap between the two curves is what
  electrifying the fleet does to the grid connection.
- **Run** tab — input status, input preparation, single-scenario run, result metrics,
  schedule table and generated figures.
- **Configuration** tab — read-back of exactly the values sent to the model, downloadable.
- **Input Data** tab — the fleet roster, the routed trips, the cost parameters and the
  mean diurnal depot load.
- **Results** tab — explorer for earlier batch result CSVs with quick charts. It does not
  repeat the latest run's schedule and figures: Streamlit puts every tab into the same
  page, so a second copy would be sent to the browser on every rerun.
- **Figures in the browser** — the Run tab embeds only the two figures the optimization
  itself writes. The input-preparation diagnostics are written to `results/` but not
  embedded, because the browser would lay them out again on every interaction. A sizing
  run and a sweep generate no figures at all (*What a run writes*, below).

Every figure is written as **PNG** at `FIGURE_DPI = 150`, set per module next to the other
figure defaults. The format is chosen for the browser rather than for the file size: a
raster is a single `<img>` however large it is, while the vector versions cost the page
1000–2600 DOM nodes each and were re-laid-out on every widget interaction. On file size
the trade is mixed — the depot timespan plot went 305 kB → 111 kB, the SoC plot 239 kB →
476 kB. Raise `FIGURE_DPI` if a figure has to go into print.

## How execution works

`src/hdv_web_interface.py`:

1. Executes `src/hdv_disposition_optimization.py` into an isolated namespace
   (the batch `__main__` block is stripped, so it never runs in the web process).
2. Applies the UI overrides on top of the model defaults.
3. Calls the model's own `build_runtime_context()`, which recomputes every derived
   value — trip slice, cost curves, solver grids, depot and PV profiles.
4. Calls `run_optimization(...)` for the chosen day.

Step 3 is the important one: the runner never re-implements model logic, so the UI
cannot drift out of sync with the model. Behaviour for a given parameter set is
identical to the batch script.

## Running from the command line

The interface is the standard environment and `main.py` starts it by default. The flags
below are the headless face of the same code — nothing here re-implements the model, and a
given parameter set gives the same answer whichever way it is reached.

| command | what it does |
| --- | --- |
| `python main.py` | start the web interface on <http://localhost:8501> |
| `python main.py --port 8600` | ... on another port |
| `python main.py --prepare` | regenerate the derived inputs in `results/`, then stop |
| `python main.py --optimize` | prepare the derived inputs, then run the parameter sweep |
| `python main.py --no-interface` | the same run, spelled for the case it exists for: a remote server, or any machine with no browser |
| `python main.py --force-routing` | with `--prepare`/`--optimize`: re-route the order data even if `order_trips.csv` is current |
| `python main.py --no-plots` | with `--prepare`/`--optimize`: skip the diagnostic figures of the preparation steps |
| `python src/hdv_disposition_optimization.py` | run the sweep directly, skipping the preparation step |

That is the whole flag set. **There is no `--date` and no `--mipgap`**: what a run plans and
how hard it solves are module parameters, not arguments, and they are reached in one of the
two ways below.

### The parameter block

Sections 1.3/1.4 at the top of `src/hdv_disposition_optimization.py` hold every parameter of
a run. Edit them and use `python main.py --optimize`. The two that decide a single run are:

```python
order_data_days      = [2, 2]   # first and last day of the trip data to plan (1-127)
optimization_MIPGap  = 0.1      # 0...1
```

### Picking the date

**The date is not typed in.** `date_disposition` near the top of the file does *not* select
the day — it only decides which depot load and PV curves are pre-built, and the day's own
date overrides them anyway (2.1.1d). What a run plans is chosen by `order_data_days`, and
the calendar date of each day is read from `trip_date` in `data/order_trips.csv`. So a run with
`date_disposition = '07.11.2025'` and `order_data_days = [3, 3]` plans **04.01.2024**, which
is day 3 of the shipped order book.

`order_data_days = [N, N]` is therefore the single-day form, and the day's position *inside*
the chosen range — always `1` for a single day — is what `run_optimization` is passed. The
range is renumbered from `order_data_days[0]`, so this stays `1` whichever day `N` is.

### One day, by date, with a MIP gap

The cleanest way to do it without editing the file is `model_parameters()`, a context manager
that sets module parameters for the length of a block and puts them back afterwards. It
refuses a name that is not already a module global, so a typo fails loudly instead of
quietly creating a dead parameter. Run this from the project root:

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

The lookup is only there to spare you translating a date into a `day_ID` by hand. The tuple
handed to `run_optimization` is `(scenario, scenario_year, v2g_status, day)` — the first
three are single values from the `scenario` / `scenario_year` / `v2g_status` lists of the
parameter block.

Call `build_runtime_context()` **inside** the block, as above. It reads `order_data_days`
when it loads the trip set, so a parameter set after it has run has nothing left to affect.
Anything else from sections 1.3/1.4 can go in the same call — `work_hours_start='06:00'`,
`optimization_time_limit_s=600`, `route_chaining_status='off'`.

Outputs land in `results/` under the run's own timestamp, so a command-line run never
overwrites an earlier one and shows up in the Results tab like any other.

### On Linux

Nothing in the model is platform-specific, and a single-day run keeps multiprocessing off —
the sweep only enables it above four iterations — so the `spawn` start method is not
involved. You need `gurobipy` with a valid licence in the same environment, plus the
packages listed under **Requirements**. Run from the project root: the model anchors its own
I/O to the repository through `project_path()`, but the snippet above reads `src/` and
`data/order_trips.csv` relative to the working directory. If `€` or `ö` come back garbled, that is
a console codepage and not the model — it does not happen under a UTF-8 locale.

## Conventions

**The objective is not a bill, and the results no longer pretend it is.** Alongside the
money — fuel, electricity, tolls, battery wear, the demand charge — the objective carries
terms that exist only to make the search behave: a flat charge per truck used, another per
occupied charger slot, another per plug-in, a nudge to spread battery aging, a deterrent on
public charging, a head count standing in for a roster the MILP cannot build, and a price on
breaking the driving limits so an undrivable trip does not come back as a bare "infeasible".
On an **asset-sizing** run it also carries a charge per cable the day needs at its busiest;
a disposition or a sweep does not, because there the station list is given and cannot be
decided (see *The charger-use penalty is an asset-sizing term* below).

None of those is money anyone pays. Nobody invoices the operator €1 for occupying a charger
or €500 for a half-hour over the Lenkzeit. Reading `ObjVal` as "what the day cost"
therefore inflates the figure by an amount that depends on how the search was *tuned*:
sharpen `penalty_vehicle_use` to improve the sizing and the day appears to get dearer.

**And once it runs the other way.** The driver wage is a real cost the objective
deliberately does *not* carry (see **Drivers**), so `operating_cost_€` holds one term that
`objective_€` never weighed. That is the only such term, and the reconciliation below takes
it back out explicitly.

Choosing between interchangeable trucks is **no longer** one of those terms. It used to be
`penalty_vehicle_id_order`, one euro per id on every truck used, and it was removed in
September 2026 because a price is the wrong instrument for it: it biased a design run
towards whichever vehicle type happened to draw the low ids. It is now a constraint
(3.3.10b) — `y[next] <= y[previous]` between trucks the model cannot tell apart, which
removes the duplicate schedules from the feasible set instead of making them unattractive,
and costs the objective nothing. `postprocess` keeps the check that it is gone: anything
left on a `y` coefficient beyond `penalty_vehicle_use` is reported as a residual.

So a run reports three numbers instead of one:

| field | what it is |
| --- | --- |
| `operating_cost_€` | the day's economics: energy + tolls + degradation + demand charge + the driver roster, less V2G and V2V earnings. No penalty is in it. |
| `steering_penalties_€` | the search apparatus, itemised alongside in `penalty_*_€` fields |
| `objective_€` | what the solver minimised, for reconciliation |

with `objective_residual_€` closing the loop: `objective_€` equals `operating_cost_€` with
the driver salary taken back out and the reported aging swapped for the aging the objective
carried, plus `steering_penalties_€`. It comes out at zero on the example data, and a large
residual means a term was added to the objective and not to the accounting — worth knowing
before the figures are quoted. On day 1 the penalties are €158 of a €1399 objective: 11 %
that was previously indistinguishable from cost.

The residual earned its place immediately. It came back non-zero on design runs only, and
the cause was that `postprocess` reads the depot's baseline load and PV curve as module
globals rather than arguments — so while a design run builds each day of the range against
that day's own curves, it was *reporting* every day against the single day the module had
been configured for. The demand charge, the grid peak, the no-BEV counterfactual and the
grid figure were all a different day's weather. The objective had it right and the report
did not, which is precisely the class of error a reconciliation exists to catch and the one
the design run was built to avoid.

The design run does the same. `objective_EUR` is kept because it is what the lattice ranked
candidates on, and `total_cost_clean_EUR` is added beside it — ownership plus penalty-free
operating cost, which is the figure two designs should be compared on. The two now also
differ by the driver salary, which `objective_EUR` does not carry and
`operating_cost_clean_horizon_EUR` does.

### The charger-use penalty is an asset-sizing term

`penalty_charger_use` (20 €) prices **how many cables the day needs at its busiest**, and
the run mode decides whether it applies at all:

| run | applies | why |
| --- | --- | --- |
| **Asset Sizing** (`auto_sizing = 'on'`) | yes | the depot is a design variable in all but name — the run charges against an infrastructure built to fit and reports what the schedules turned out to need, so without a price on concurrency the answer is "one cable per truck" |
| **Disposition**, **Scenario Sweep** | no | the station list of `depot_dataset.xlsx` is given, 3.3.12(a) already forbids more trucks plugged in at once than there are stations, and nothing in the run can change that number |

Pricing it on a disposition bought nothing and cost something. It is a charge on using
hardware the operator has already paid for, so it pushes the fleet into serial charging —
and with `charging_power_modulation` on, serial charging is exactly what raises the peak
the demand charge is billed on. It also sat in `steering_penalties_€` on every day of a
sweep, moving the objective and nothing else.

The counterweight `charging_power_modulation` needs is still there on every run:
`penalty_charging_use` charges per occupied half hour and `charging_min_energy_kWh` forbids
holding a cable at zero, so spreading a charge thinner is never free.

The gate is one function, `charger_use_price()` (1.5a), read by both the objective (3.4c)
and the reconciliation that checks it (5.8b) — if the two disagreed about whether the term
was there, `objective_residual_€` would report it as a mystery. `chargers_concurrent_peak`
is reported on every run regardless: how many cables the fleet was on at its busiest is a
fact about the day, and only the *price* is mode-dependent. On a disposition the peak comes
back as a figure with €0.00 beside it.

`penalty_charging_block` (5 €) is a handling charge per visit to a depot charger, on every
run. It is deliberately small: measured on day 93, pricing plug-ins does not change how many
the fleet makes (12 arrivals at €0, 12 at €10, 11 at €50 — and 17 at €2, which is *above*
the unpriced baseline and so is seed noise across alternative optima rather than a
dose-response). What it does buy is a tie-break the price
curve cannot give, since depot energy holds one price across 28 steps of the day and every
arrangement inside a price window is an alternative optimum of equal cost. Pushed harder it
would hold a truck on a charger through a dearer step to avoid a second visit, which is a
distortion rather than a preference; check `charging_blocks` in the run summary against one
visit per truck before raising it.

Everything in `results/` is named for what it is, with no `hdv_` prefix: the whole
directory belongs to this model, so repeating that on each of two dozen files said
nothing and only pushed the distinguishing part of every name three characters to the
right. Sources under `src/` keep the prefix - those sit on the import path, where the
namespace really is shared. Files written by an earlier version still carry the old
names; they are stale rather than wrong, and any run overwrites them under the new ones.

Vehicles use the lower-case schema throughout: the column is `vehicle_id` and
`vehicle_type` is `bev` or `ice`. `hdv_disposition_optimization.normalize_fleet()`
maps other spellings onto it, so `fleet_dataset.xlsx` can use either case.

The fleet is never synthesized. `inputs/fleet_dataset.xlsx` is the roster, one row per
vehicle, and it is used verbatim - fleet size and bev share are properties of that
file, not run parameters.

**The charging infrastructure is never synthesized either.** The `charging` sheet of
`inputs/depot_dataset.xlsx` is the station list, one row per station with its own
`charger_power_kW`, and it is used verbatim - the number of stations and the power of
each are properties of that sheet, not run parameters.

**Which station a truck gets is not optimised.** A truck that plugs in takes whichever
free station has the most power, so with `k` trucks plugged in at once the fleet occupies
the `k` strongest stations. The model therefore decides only *whether* a truck charges in
a step, and constraint 3.3.12 holds the fleet's draw inside the cumulative power of those
`k` stations - a piecewise-linear bound that is exact at every integer `k`, because the
cumulative power of a descending list is concave. A single truck is capped by the
strongest station and by its own charging power.

That removes the per-(vehicle, station, step) assignment binaries: on a 10-station depot
with 10 bev, 4800 of them, about three quarters of the whole model. The trade is that the model can no longer put a
truck on a weak station to keep a strong one free for another - a choice a depot does not
make in practice either. Results still name the stations by their `charger_id`
(`chargers_used_ids` in the result table, `charging_station_<id>` in the schedule); the
schedule reconstructs the assignment by applying the same rule the model was built on.

The depot's **own PV generation is priced at its opportunity cost**. The plant of the
`generation` sheet is queried from PVGIS for the disposition date, at that sheet's
location, peak power, tilt and azimuth, which yields an intraday curve on the same
30-min grid as everything else. The site's own baseline load sits behind the same meter
and is inelastic, so it is served from the plant first; only the surplus can reach a
truck.

Nothing this depot does transacts at the bare spot price, so every price in the model is
built from it by one of two overheads:

| direction | price | default |
|---|---|---|
| every kWh **bought** | `electricity_spot_price_€/kWh` **+** `grid_energy_overhead_eur_per_kWh` | spot + 15 ct |
| every kWh **sold** | `electricity_spot_price_€/kWh` **−** `energy_selling_overhead_eur_per_kWh` | spot − 0 ct |

Two rules, and everything follows from them:

| flow | settles at |
|---|---|
| depot charging from the public grid | the buy price |
| depot charging from own PV | the **sell** price — that kWh could have been sold, so the revenue given up is its cost |
| V2G into `arbitrage` | the sell price |
| V2G into `flexibility` | the **bare** `flexibility_spot_price_€/kWh`, no overhead deducted |

Buying carries everything the market price does not — grid fees, levies, taxes, supplier
margin. Selling loses whatever marketing it takes — direct-marketing or platform fees, EEG
deductions. The PV plant's LCOE does not appear anywhere; it is a sunk average and the
plant is built either way.

The gap between the two prices is

```
(spot + grid_overhead) - (spot - selling_overhead)  =  grid_overhead + selling_overhead
```

— **15 ct/kWh at the defaults, independent of the spot price**, which cancels. That single
number does double duty:

- it is the **PV advantage** per self-consumed kWh, reported as `pv_energy_saving_€`. The
  case for self-consumption here is a levy-and-tax argument rather than a cheap-sunlight
  one: a kWh that never crosses the meter is never taxed or tariffed.
- it is the **wedge a V2G round trip has to clear**, since the depot sold the kWh at the
  sell price and buys it back at the buy price. Together with the 90.2 % round-trip
  conversion loss, that is a high bar — an arbitrage spread has to beat both before a V2G
  slot earns anything.

Both prices are per **metered** kWh, measured at the charger input, so the cost of a kWh
actually *stored* is the buy price divided by `charging_efficiency` (see the conversion-loss
note above). At the defaults a grid kWh in the battery costs `(spot + 0.15) / 0.97`.

Nothing is floored at zero. A spot price below the selling overhead means a kWh put on the
grid earns less than it costs to place — real on a negative-price hour — and the model
simply declines those V2G slots, since claiming one is optional. On the same steps own PV
becomes a credit rather than a cost, which the run prints a note about.

The `flexibility` channel is the one exception to the two rules: it settles at the **bare**
`flexibility_spot_price_€/kWh`, with no overhead deducted. `energy_selling_overhead` prices
the marketing of an *energy* sale, and this channel is paid for a service rather than for
energy, so that deduction does not describe it. If flexibility marketing has a cost at this
site, it belongs in the flexibility curve of `costs_dataset.xlsx` rather than here.

Self-consumption itself is a fact of the wiring, not a decision: the model constrains the
PV share of the depot charging to exactly `min(PV surplus, depot charging)` per step
rather than bounding it from above, and that equality is pinned by **feasibility** — the
two big-M lines leave exactly one branch feasible per step. So the reported PV share stays
exact even under the default, where the objective coefficient on `E_pv_charging` is zero
and nothing would otherwise push the variable anywhere. The accounting cannot drift from
the physics whatever the price is; what changes with the price is only where the optimizer
prefers to place the charging, never whether the sun ends up in the battery. The cost of
that guarantee is one binary per step with a surplus, which under the default buys
reporting only.

The day is **closed at both ends by the same state of charge**. `initial_soc_fraction`
(the sidebar's *Day start & target end SoC*, 50 % by default) fixes every bev's SoC at
00:00 and is at the same time the level it has to have reached again at 24:00. The
terminal form is `>=`, not `==`: finishing above the target is operationally harmless, and
since every kWh is paid for, the optimum sits on the target unless peak shaving — or own
PV given a below-retail opportunity price — makes ending higher worthwhile. The point of the constraint is that a day's
schedule cannot be financed by depleting the batteries — every kWh driven or sold to the
grid has to be bought back within the same day, which is what makes consecutive days
comparable.

The **DSO demand charge is billed on the increment the fleet causes**, not on the whole
site peak. The counterfactual is the peak the depot would draw with no bev at all — its
own metered load net of its own PV, floored at zero per step — and the charge is

```
(peak_power_price_eur_per_kW / 365) x (grid peak with bev  -  grid peak without bev)
```

The depot draws that baseline whether or not a single truck is electric, so it is not a
cost of the disposition; attributing it to the fleet only inflated every figure the run
reported by the same amount. On the example dataset the site peaks at 27.2 kW on its own,
which at the default 17 €/kW/year is €1.27/day of demand charge no longer charged to the
fleet.

The term is **signed** on purpose. If V2G pulls the peak below what the site alone would
have drawn, the difference is negative and the depot genuinely pays a smaller demand
charge than it would without the fleet — peak shaving is one of the things V2G is for, and
a floor at zero would hide it. The credit is bounded, since `site_import >= 0` per step
means the peak cannot fall below zero.

Because the counterfactual holds no decision variable, this shifts the objective by a
constant and so cannot move the *exact* optimum — it changes what the run attributes to
the fleet, not what the fleet does. The solution actually returned can still differ,
because `MIPGap` is a *relative* tolerance and a constant shift moves the point at which
it is met.

### Battery degradation

Battery degradation is charged on **V2G discharge only**. Driving and ordinary charging
still age the battery, but that wear follows from operating the truck at all, so pricing
it would bias the V2G business case rather than inform it. The fleet-fairness term
(`degradation_distribution_penalty`) deliberately still works on total throughput, since
it spreads physical wear regardless of who pays for it.

**Two assumptions this aging model rests on.** It carries cycle count and the SoC window
(below) and nothing else. The two stressors the cell literature usually puts beside them
are left out on purpose, and both omissions are statements about **BEV trucks** rather than
simplifications of convenience:

- **a) C-rate is not modelled, because these packs never leave the flat part of the curve.**
  A heavy BEV truck carries 500–900 kWh, and everything connected to it moves that energy at
  **around 1C at most**: a 350 kW depot charger into a 600 kWh pack is ~0.6C, V2G discharge
  is bounded by `vehicle_v2g_power` — the same converter or smaller — and traction draw is a
  few hundred kW at motorway speed. Even megawatt charging only reaches ~1.7C, and only over
  the stretch below 80 % SoC before `charging_curve_status` derates it. Rate-dependent aging
  is flat across that whole envelope; the steep part of every such study lives at 2–4C and
  belongs to small packs, not to truck packs. Adding a C-rate term would multiply the model's
  size — the rate is a decision variable here, so the term would be bilinear — to buy a
  coefficient that is constant everywhere these trucks can operate.
- **b) Temperature is not modelled, because the pack is conditioned.** Every modern BEV
  truck has a battery thermal management system: the pack is liquid-conditioned and held
  near its design point whether the truck is driving, fast-charging or standing on a depot
  charger overnight. The cells therefore do not see ambient temperature, they see the
  setpoint, and an aging term in a variable held constant by design is a constant. What a
  thermal model *would* add is the energy the conditioning itself draws — a consumption
  question rather than an aging one, and already inside `vehicle_consumption`.

Both hold for the fleet this model is built for. A study of light vehicles, of small packs
pushed to 3C, or of unconditioned packs standing in a cold yard would have to revisit them
— and revisit them in 3.4d of `src/hdv_disposition_optimization.py`, which is where every
kWh of wear is priced and where both assumptions are written down beside the expression
they justify.

The discharged energy is weighted by the state of charge it comes out of:

```
w = 1 + 4 × soc_weight_factor × (SoC / capacity − 0.5)²
```

so with the default `soc_weight_factor = 0.5` a kWh counts 1.0× taken at half charge and
1.5× taken at either extreme — a cell ages fastest held full and fastest again run flat.
Being quadratic, the penalty stays nearly flat across the working middle and steepens into
the corners. An earlier form rose only towards full, which made the last kWh out of a
nearly empty battery the cheapest of the day and quietly rewarded running it down.

The parabola is **charged as the maximum of its tangents** — `soc_weight_breakpoints = 9`
of them, within 0.008 of the curve and exact at the breakpoints. That keeps it to linear
constraints. Carrying the square itself needs a quadratic constraint, and `w × E_neg`
would be cubic; measured on one day that cost 4× the solver throughput (2 631 nodes in
120 s against 10 453) for the last 1.6 % of the shape. `soc_weight_factor = 0` removes the
weighting and every quadratic term with it, leaving a pure MILP.

**`degradation_cost_€` is the weighted figure.** `v2g_equivalent_full_cycles` stays an
unweighted physical cycle count, so the two are no longer related by a single €/EFC factor
— they differ by the mean weight of the discharge, between 1.0 and 1 + `soc_weight_factor`.
Reporting the unweighted cost instead, as the model used to, understated the wear by up to
50 %.

It is *not* quite what the objective charged, and both numbers are now reported.
`degradation_cost_€` evaluates the piecewise-linear weight at the SoC each step started
from — the weight that discharge actually earns. The objective instead holds a variable per
step, bounded below by the tangents of the curve, and the minimisation only presses it down
onto them as far as closing the MIP gap is worth a node. Where it was left slack and the
step discharged, the objective charged more aging than the curve says:
`degradation_cost_in_objective_€` is that figure, ~€0.80 above the reported one on day 1 at
the default 10 % gap. The reported cost is the physically correct one; the difference is an
artefact of the linearisation, and it is named rather than left to surface as an
unexplained residual in the reconciliation above.

`vehicle_battery_warranty` states the warranted number of **equivalent full cycles**
for each bev; it is the denominator of that vehicle's degradation price
`€/EFC = vehicle_price × battery_price_share / vehicle_battery_warranty`. It stays
blank for ice rows, which have no battery.

**Nothing in the fleet roster is defaulted.** `vehicle_consumption` and `vehicle_price`
are required of every row; `vehicle_energy_storage`, `vehicle_charging_power` and
`vehicle_battery_warranty` of every bev row, where ice rows may leave the last three
blank because nothing reads them there. A missing column, a blank cell or a value `<= 0`
is rejected with the offending vehicle IDs named. Substituting a figure would misprice
that vehicle — a default price or warranty misprices its degradation, a default charging
power silently turns a truck into one that cannot charge or feed back, and a default
capacity moves every SoC bound it has.

The same rule holds for the depot load: `depot_load_profile.csv` has to cover all 48
half hours of the day. A step it never covers is a hole in the measurement, not an hour in
which the site drew nothing, and reading it as 0 kW would understate the grid peak and
overstate the PV surplus left for the trucks. The missing times of day are named.

## Notes

- **Compute** — full MILP runs can be expensive. Start with a small roster in
  `fleet_dataset.xlsx`, a higher MIP gap (5–10 %) and a single day. Every charging station
  of the `charging` sheet adds one binary per 30-min step per bev, so a long station list
  costs solver time even when most of it stays idle.
- **Station symmetry** — there is nothing left to break. No variable in the model is
  indexed by station (3.3.12 replaced the per-(vehicle, station, step) binaries with three
  aggregate statements), so equal-power stations cannot multiply the solution count: the
  solver never chooses between them, and the assignment is reconstructed after the solve.
  Adding ordering rows over the stations was measured on the older tiered depot and made
  things *slower*, which is moot now but worth not repeating.
- **Charger tiers cost binaries** — 3.3.12(c) writes one binary per (truck, step, tier
  *below the strongest*), and none at all on a single-power depot. `depot_dataset.xlsx`
  lists 10 x 600 kW in the example data, so that count is **zero**. Flattening it from the earlier
  2×100 / 3×300 / 5×600 removed 960 binaries and, measured on day 93, took the solve from
  250 s to 70 s. Giving the chargers distinct powers to "tell them apart" runs this in
  reverse: ten distinct powers means nine tiers and 4,471 → 8,791 binaries.
- **Parallel sweeps** — above four iterations the sweep runs on a process pool.
  `parallel_worker_limit` (default 4) caps how many Gurobi sessions run at once; the
  remaining cores are handed to each solver as threads, so `workers × threads` always
  fills the machine and a low limit wastes nothing. Raise it only if the licence permits
  more simultaneous sessions.
- **PV profile cache** — PVGIS seriescalc is asked once for a representative year of the
  plant in `depot_dataset.xlsx` (sheet `generation`). Every calendar day of that year is
  interpolated onto the 30-min grid and stored in `data/cache_pv_profile.json`, which
  is enough for every day in `order_trips.csv` and for any other disposition date (looked up
  by month and day, year ignored). The file carries a `source_fingerprint` of
  `order_dataset.xlsx` and `depot_dataset.xlsx`; a mismatch rebuilds it, including from
  `ensure_derived_inputs()`, so the cache always matches the current files in `inputs/`.
  `python src/hdv_pv_profile_generation.py --force` queries PVGIS even when the
  fingerprint still matches. A failed query falls back to a simplified bell curve only
  when `pv_allow_synthetic_profile` is on, and that stand-in is deliberately *not*
  cached.
- **PV outages** — a failing PVGIS round trip is retried (`pv_profile_retries`, linear
  backoff); one that keeps failing **raises**. The curve decides how much of the depot's
  own generation reaches the trucks, so a synthetic stand-in would move every energy
  figure of the run while looking exactly like a real curve. Set
  `pv_allow_synthetic_profile = 'on'` to accept the simplified bell curve anyway — the run
  then prints a warning, the Run tab shows one, and `pv_profile_source` in the result table
  reads `synthetic` instead of `pvgis` or `cache`. Timestamps are parsed strictly against
  the documented PVGIS formats and the target day has to be complete; a record the formats
  do not cover raises rather than being skipped, which would have punched holes in the day.
- **Geocoding and routing outages** — both retry with exponential backoff, and a few
  unusable addresses or unroutable pairs are reported and dropped as before. Beyond
  `MAX_UNRESOLVED_LOCATION_SHARE` / `MAX_UNROUTED_ORDER_SHARE` (5 % each) the build stops
  instead: losing a large share of the orders means the service was down, and finishing
  quietly would hand the model a decimated trip set indistinguishable from a complete one.
  Everything resolved so far is in `data/cache_routing.json`, so a later run resumes
  from it rather than starting over.
- **PV weather** — PVGIS is asked for one representative historical year, and the
  disposition date selects that calendar day from it. The curve is therefore a real day's
  weather rather than a clear-sky ideal, and two neighbouring dates can differ by a factor
  of three. Pick the date deliberately, or compare a few.
- **Licence** — Gurobi must be importable with a valid licence.
- **Figures** — a disposition run always writes its PNG figures into `results/`, and a
  terminal one opens them when it finishes. A sizing run and a sweep write none. This
  is not a setting (*What a run writes*, below).

## Troubleshooting

- *Run button disabled* → either a dataset in `inputs/` is missing (those cannot be
  regenerated) or a derived input failed to build; the Run tab names which.
- `ModuleNotFoundError: gurobipy` → install gurobipy + licence in the same environment.
- Solver status `infeasible` → the fleet cannot serve that day's trips. Add rows to the
  `charging` sheet of `inputs/depot_dataset.xlsx` or raise their kW, choose a day with
  shorter trips, or edit `inputs/fleet_dataset.xlsx` to add vehicles or lower the bev share.
  Both are datasets, so their derived files rebuild on the next run by themselves.
- *"trip(s) are longer than their own time window in the order data"* → the named trips
  cannot be scheduled at all, whatever the working hours are: the duration exceeds the
  window `order_dataset.xlsx` gives them. Correct the window or the duration there. The
  working hours never cause this, since a trip that does not fit inside them falls back to
  its own window.
- Path / file not found → launch from the `02_Modell` folder.
- *Port 8501 already in use*, or the browser shows a **Script execution error** naming a
  file that does not exist → a Streamlit server from an earlier session is still holding
  the port, and the browser is talking to that one rather than to the app just started.
  `python main.py` now refuses to start in that case and names the two ways out: pick
  another port with `--port`, or stop the process holding 8501:

  ```powershell
  Get-NetTCPConnection -State Listen -LocalPort 8501 | Select OwningProcess
  Stop-Process -Id <OwningProcess> -Force
  ```
