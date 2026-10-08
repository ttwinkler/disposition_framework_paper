# Constraint Model Runtime Optimization Assessment
## User

Do a comprehensive runtime optimization assessment of the model without loosing any features. Follow these steps:
1. do a deep web and code research on possible options and theories to speed up the runtime for example rewriting the constraints or different optimization and solver parameters but also every other method you can find
2. implement und run all found options in separate containers so the original code wont be affected
3. show me a summary table of all assessed options with the following results for each: runtime saving, downsides of the implementation

## Assistant

I'll start by mapping the model, solver setup, and existing benchmark containers so the research and later experiments stay grounded in this codebase.The repo already has a large benchmark suite; I’ll inspect the current solver path and harness next, then research techniques that have not been tried yet.I’ll inspect the solver setup, unrun variants, and the formulation hotspots, and search the literature for MIQCP/Gurobi techniques that this suite has not tried.I have a clear gap list against the current baseline. Next I’ll implement the untested options as isolated variants and start paired runs.Containers apply cleanly. I’ll smoke-test the new formulation variants at a short time cap so a crash doesn’t burn a multi-hour run.Screening is running: 43 options, each paired against the current baseline on day 2. I’ll fill the summary table from those measurements when the batch finishes.Screening finished. I’ll replicate the few options that looked faster on extra seeds, so we don’t treat a lucky search path as a real saving.Replication is running on seeds 7 and 13 for the shortlist. I’ll merge those numbers into the full assessment table when it finishes.Replication is still running; the final table will use those extra-seed numbers, not the single-seed screen.The extra-seed run is in progress. Next step is the summary table once those pairs finish.Screening is complete (86/86 records). Waiting on the extra-seed replication before writing the summary table.The production model was not changed. Every option ran as an isolated parameter patch or a private source container.

**Current baseline** (already in `src/`): Gurobi 12.0.2, `MIPFocus=0`, `DegenMoves=0`, day 2, all features on, stop at 10% gap. New numbers are paired against that baseline (variant immediately after its own baseline, same seed) so machine slowdown does not leak into the ratio.

## What actually saves time now

Three things replicated on seeds 1, 7 and 13, all feature-preserving:

| option | runtime saving | speed-up (median, range) | objective vs baseline | downsides |
| --- | --- | --- | --- | --- |
| **`c_pvchain`** — native PV `min` + chain-timing indicators + `GomoryPasses=0` | **36%** | **1.57×** (1.33–1.65×), 3/3 seeds | +0.28% | Not measured on day 95 (the last day-2-only win, `ImproveStartGap`, reversed there). Three changes at once. Slightly dearer incumbent, still inside the 10% gap. Extra general constraints make the LP a bit less transparent. **Not applied.** |
| **`m_chainind`** — indicator instead of big-M on chain timing | **29%** | **1.42×** (1.21–1.92×) | −0.13% | Two auxiliary start-time variables per chain. Objective wandered ±0.7% across seeds. Day 95 untested. |
| **`p_cuts1`** — conservative cuts | **26%** | **1.35×** (1.07–1.50×) | −0.13% | Seed 1 was only 1.07×. Weaker cut generation can stall the dual bound on harder days. Day 95 untested. |
| **`p_gomory0`** — skip Gomory cuts | **16%** | **1.19×** (1.06–1.24×) | +0.19% | Dense Gomory cuts are dropped; models that need them for the bound will suffer. Inside the combination above. |
| **`m_pvmin`** — `addGenConstrMin` for PV share | **7%** | **1.07×** (1.04–1.24×) | +0.20% | Screening looked like 1.24×; extra seeds knocked it down. Not worth applying alone. |
| **`p_method2`** — barrier at the root only | **9%** | **1.10×** (0.99–1.29×) | +0.33% | Seed 13 was a **loss**. Not consistent. |

The combination is the only new result that is both consistent and large. It was **not** written into `src/hdv_disposition_optimization.py`, on purpose: day 95 is untested, and this study has already seen a day-2 win reverse on the harder day.

---

## New options vs current model (43 + combination)

Runtime saving is \(1 - 1/\text{speed-up}\). Positive = faster. `NO-OP` means identical node count and objective: the option did not change the search, so any clock difference is noise.

### Replicated (3 seeds)

| option | runtime saving | downsides |
| --- | --- | --- |
| `c_pvchain` | **36% faster** | See above. Strongest new result; day 95 missing. |
| `m_chainind` | **29% faster** | Extra vars; objective noise ±0.7%; day 95 missing. |
| `p_cuts1` | **26% faster** | May weaken the bound on harder instances. |
| `p_gomory0` | **16% faster** | Loses Gomory cuts, which some MIPs need. |
| `m_pvmin` | **7% faster** | Too small to trust as a standalone change. |
| `p_method2` | **9% faster, not consistent** | One seed slower; barrier root + simplex nodes. |

### No effect (bit-identical search)

| option | runtime saving | downsides |
| --- | --- | --- |
| `p_miqcp0` MIQCPMethod=0 | none (NO-OP) | Ignored on this non-convex bilinear model. |
| `p_method1` dual simplex at root | none (NO-OP) | Concurrent root was already choosing dual. |
| `p_sifting2` | none (NO-OP) | Sifting never engaged. |
| `p_lpwarm2` | none (NO-OP) | Default LP warm start already sufficient. |
| `p_predual2` | none (NO-OP) | Dual presolve already on auto. |
| `p_integrality1` | none (NO-OP) | No trickle-flow issue to fix. |
| `m_spatialpri` high priority on bilinear factors | none (NO-OP) | Spatial brancher already owns those variables. |
| `m_reach` fix `z=0` when trip energy > battery | none (NO-OP) | Presolve already proves those assignments infeasible (`SoC ≤ cap < trip energy`). |

### Screening only, 1 seed — no demonstrated win

Anything here is one search path. Treat as “did not help on seed 1”, not as a measured speed-up.

| option | runtime saving (1 seed) | downsides |
| --- | --- | --- |
| `p_cuts0` no cuts | **0%** (0.99×) but **5× more nodes/s** | Cheaper nodes, much weaker bound; wall clock unchanged. Classic degeneracy trade. |
| `p_scale2` | ~7% | Below noise; not replicated. |
| `m_branchpri` assignment branching priority | ~5% | Can bias the tree toward a worse incumbent. |
| `p_branchdir1` branch up first | ~4% | Opposite of bound-focused search. |
| `p_numeric1` | ~4% | Extra numerical work for no proven gain. |
| `p_pricing1` steepest-edge | ~1% | More expensive iterations. |
| `p_aggregate0` | ~1% | Larger, less reduced LP. |
| `p_symmetry0` | ~2% slower | Detection cost is small; turning it off still changes the path. |
| `p_heurskip` pump/minrel/subMIP off | ~2% slower | Incumbent-side heuristics were cheap here. |
| `p_nlheur0` | ~8% slower | NLP heuristic (on by default in Gurobi 12) was paying for itself. |
| `p_miqcp1` outer approximation | ~5% slower | Extra linearization work, no better bound. |
| `p_clique2` / `p_flowcover2` / `p_implied2` / `p_network2` / `p_zerohalf2` | 8–28% **slower** | More cuts → heavier node LPs. Same failure mode as every earlier LP-tightening. |
| `p_cuts3` very aggressive cuts | **38% slower** | Fewest nodes (2539 vs 5512) but each one is expensive. Bound is not the cheap side. |
| `p_pricing2` Devex | ~9% slower | Wrong pricing for this LP. |
| `p_objscale` | ~23% slower | Rescaling the mixed-euro objective hurt. |
| `p_quad1` quad-precision simplex | **40% slower** | Precision tax with no numerical crisis to fix. |
| `p_varbranch1` pseudo-cost branching | **55% slower** | 34k nodes vs 5.5k; cheap branching, terrible decisions. |
| `m_indicators` SoC big-M → indicator | ~10% slower | Indicators on every (vehicle, trip, start) add overhead. |
| `m_chgmin` native min for full-power charging | ~18% slower | Extra general constraints + product linearization. |
| `m_sos1` SOS1 on start times | ~12% slower | Set branching on a choice the covering constraint already encodes. |
| `m_orbitope` stronger symmetry | ~11% slower | Extra rows; assignment-count ordering is not free. |
| `m_partition` partition heuristic by vehicle | ~20% slower | Vehicles are coupled through the depot/peak; partitions are not independent. |
| `m_lazycrew` crew rows as lazy | ~5% slower | 3× more nodes; lazy cuts delay the bound. |
| `m_qobj` bilinear as Q-constraint | ~16% slower | Same product, worse form (`NumQNZs` 235→0 because it left the objective, not because it became a MILP). |
| `m_nmdt` exact NMDT, bilinear remainder | **25% slower**, obj **+1.15%** | ~940 extra binaries. Remainder still bilinear. Tighter envelopes did not pay. |
| `m_logbins` log-encoded SoC bins → MILP | **61% slower**, obj **+2.1%** | `qnzs` 235→0, a real MILP, and still much slower (31k nodes). Same lesson as `m_lindegrad`: binaries cost more than 235 bilinear terms. |
| `m_lazycrew` / `m_sos1` / `m_orbitope` | see above | Formulation tightens or reorders, node LPs get worse. |

---

## Previously assessed (already in `benchmarks/`, original code unchanged)

These were measured in earlier rounds. Two of them **are** the current default.

| option | runtime saving | downsides | status |
| --- | --- | --- | --- |
| **`MIPFocus=0`** vs old `=1` | **~50%** day 2 (2.0× median); day 95 **0/3 → 3/3** in ~450s | +0.6–1% cost on day 2; set back to 1 if V2G is off | **applied** |
| **`DegenMoves=0`** vs auto | **34%** day 2 (1.52× paired); **20%** day 95 (1.25×) | +0.12% cost; **do not** pair with `ImproveStartGap` | **applied** |
| `ImproveStartGap=0.15` | looked 1.63× with DegenMoves on day 2 | **Slower than DegenMoves alone on day 95** (0.89×) | rejected |
| `Cuts=2` / `p_combo` | 1.6–1.8× vs *old* MIPFocus=1 baseline | Mostly that baseline being a bad seed; not re-won against current | not applied |
| Threads 2/4/8/16 | non-monotonic | Contention and path noise, not a scaling curve | leave default |
| `ConcurrentMIP` 2 / 4 | 0.7× / 0.3× | Splits the machine; both lose | rejected |
| `Presolve=2` + aggregate + sparsify | 0.75× | Heavier presolve, worse tree | rejected |
| `Symmetry=2` | none (NO-OP) | Solver already sees the symmetry | rejected |
| `NoRelHeurTime` 5 or 30 | 0.89× / no help | Incumbent is not the bottleneck | rejected |
| `NonConvex=2` | 1.00× | Already the default for this model | rejected |
| `OBBT=2/3` | NO-OP / slight loss | Auxiliary LPs cost more than they tighten | rejected |
| `PreQLinearize` 1/2 | NO-OP | Only linearizes products with a **binary** factor; these 235 are continuous×continuous | rejected |
| `PreMIQCPForm` | NO-OP | Internal bilinear form already used | rejected |
| `Disconnected` aggressive | NO-OP | Model is one coupled piece (depot/peak) | rejected |
| `Heuristics=0` | 0.61× | Need some primal work | rejected |
| `VarBranch=3` strong branching | 0.49× | Far too expensive per node | rejected |
| `NodeMethod=2` barrier at nodes | 0.13×, often hits the cap | Barrier does not converge on these node LPs | rejected |
| `Aggregate=2` | NO-OP | PDF 2/3 proxy; presolve already aggregates what it can | rejected |
| Finite bounds on all energy vars | 0.80× then ~1.0× on re-test | Bound info does not pay; can change branching for the worse | rejected |
| Bounds only on bilinear factors | mixed, not a win | Mechanism is right (McCormick needs bounds); Gurobi infers enough | rejected |
| `at_depot` declared binary | 0.88× | 480 extra branch candidates; integrality already implied | rejected |
| Lexicographic `y_m` order | 0.62× on re-test | Cuts equivalent schedules that the id-penalty already ranks | rejected |
| 17 aging-weight tangents | 0.92× | ~1880 extra rows; tighter curve, slower LP | rejected |
| Piecewise SoC bins (`m_lindegrad`) | 0.88×, `qnzs` 235→0 | MILP with ~1880 binaries is **slower** than the MIQCP | rejected |
| Split PV big-M (PDF 4) | 0.83× over 6 seeds | Textbook tightening, heavier nodes | rejected |
| Pairwise start-time cuts (PDF 6) | 1.06× (0.86–1.18×) | 14% fewer nodes, more than paid for in LP size | rejected |
| `y_m ≤ sum z` (PDF 7) | 0.95× | Valid, not helpful | rejected |
| Drop crew warm start | 0.97× on re-test | Confirms keeping the warm start | keep warm start |
| Gurobi tuner (`RINS=0`) | noise; sd > claimed gain | Tuner is the wrong tool when a solve is minutes | rejected |
| **`r_socweight_off`** | **~69% (3.2×)** | **Drops SoC-weighted aging** — different problem, MILP instead of MIQCP | reference only |
| **`r_montecarlo3`** (3 start times) | ~5× | Infeasible on some days; worse routes; **not a legal speed-up** | reference only |
| Relax `MIPGap` 0.10→0.11 / 0.125 | ~5× / ~13× | Worse-certified schedule. 81% of runtime buys the last point of gap | quality trade, not an optimization |

---

## Researched, not implemented as a container

These are real methods from the MIQCP / EV-scheduling literature. They were not coded because they either drop features, change the algorithm class, or cannot be expressed as a local patch.

| option | why it is interesting | why it was not run |
| --- | --- | --- |
| **Gurobi 13** | Vendor claims **2.68×** on hard non-convex MIQCP vs 12.0 | This environment is **Gurobi 12.0.2**. Biggest untested external lever. |
| Benders / Lagrangian split (assignment master, energy subproblem) | Standard for EV-VSP | New solver loop, not a formulation patch; optimality control changes. |
| Column generation on routes | Scales when the route set explodes | Day 2 is 13 trips; the bottleneck is bilinear energy, not route cardinality. |
| NMDT / piecewise McCormick as a *replacement* MILP | Exact-ish linearization of bilinear terms | `m_nmdt` and `m_logbins` *are* that family; both lost. |
| Time-block / rolling-horizon | Faster for large horizons | Changes which schedule is feasible; would lose the single-day optimum. |
| Decomposition of overnight V2G steps | Fewer bilinear terms | Aggregating steps changes V2G timing, i.e. a feature. |

---

## How to read this

1. **The model is a non-convex MIQCP** because of `E_neg * soc_aging_w` (235 bilinear objective terms). Turning that weighting off is 3× faster and is **not** allowed under “keep every feature”. Linearizing it (bins, log-bins, NMDT) keeps the feature and **loses** time.
2. **Cost is node LP price, not node count.** `DegenMoves=0` (already applied) and `Cuts=0` (new, 5× more nodes/s, no wall-clock win) both show that. Adding rows or cuts almost always slowed the run.
3. **Single-seed ratios are screening.** Only the six replicated rows above are measurements. Everything else is one Gurobi path.
4. **Day 95 is the veto.** `ImproveStartGap` is the warning: best day-2 combination, worse on the hard day. `c_pvchain` should be paired on day 95 before anyone writes it into `src/`.

Evidence: `benchmarks/results_new.jsonl`, `benchmarks/run_new.log`, `benchmarks/run_new_replicate.log`. Variants live under `benchmarks/variants/` (`p_*` parameters, `m_*` formulation containers, `c_pvchain` the combination). Nothing in `src/` was edited.Replication is complete: **112/112** paired records. The summary table already uses those numbers; nothing in the results changed.

- Updating plan
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\README.md`

```
1→# Runtime assessment

A self-contained study of how long `src/hdv_disposition_optimization.py` takes and what
can be done about it. **Nothing in this directory is imported by the model, and nothing
here edits it.** Every option is applied to a private copy or to a freshly imported module
inside a throwaway process, so the production code is untouched and an option that breaks
something cannot reach it.
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\README.md`

````
1→# HDV Disposition Optimization

MILP disposition model for HDV (heavy duty vehicle) fleets, with a Streamlit web
interface as its standard operating environment.

## Layout

```
…
````
- List `C:\Users\go25pux\Desktop\Framework\02_Modell`
- Read `C:\Users\go25pux\.grok\bundled\skills\long-running-background-tasks\SKILL.md`

```
1→---
name: long-running-background-tasks
description: >-
  Required reading before you start, watch, or wait on anything that keeps running after you launch it — background jobs, watchers, scheduled loops, CI, pull requests, training runs, dev servers, long builds. Read it before you launch such work and before you report on its state. Saying where a running job stands counts as working on it. Use when: about to launch, supervise, inspect, diagnose, or report on work that keeps running after it is started.
---

# Working with long-running background tasks
…
```
- List `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\containers`
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\bench_core.py`

```
1→"""Runtime assessment harness. Nothing here is imported by the model.

Every option under test is a *patch applied at run time* to a freshly imported copy of
src/hdv_disposition_optimization.py. The file on disk is never edited, and each variant
runs in its own process, so an option that corrupts module state cannot reach the next
one or the production code.

What is measured, per run:
…
```
- List `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants`
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_all.py`

```
1→"""Run every variant to the model's own 10 % target and record what it took.

Serially, one subprocess each. Serially because the point of the exercise is wall clock:
Gurobi is given every thread on the machine, so two runs at once would contend for the
same cores and both would be measured slower than they are. One subprocess each because a
variant patches module globals, and a patch that leaked into the next run would quietly
invalidate it.
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_paired.py`

```
1→"""Run each variant immediately next to its own baseline, on the same seed.

    python benchmarks/run_paired.py <tag> <day> <cap-s> <seeds csv> <variant...>

**Use this, not run_matrix.py, for anything that will be quoted.** run_matrix runs all seeds
of one variant, then all seeds of the next, which puts hours between a variant and the
baseline it is compared against. On this machine that is fatal: sustained multi-threaded
Gurobi load slows it by roughly a factor of two, and the drift is larger than almost every
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_matrix.py`

```
1→"""Run a list of variants across a list of seeds, one subprocess each.

    python benchmarks/run_matrix.py <out-tag> <day> <cap-s> <seeds csv> <variant...>

Serial on purpose: Gurobi gets every thread on the machine, so two runs at once would
contend for the same cores and both would be measured slower than they are. One subprocess
each because a variant patches module globals and a leak into the next run would silently
invalidate it.
…
```
- def solve_model|optimization_MIPFocus|optimization_DegenMoves|setParam

```
found 16 matches
```
- ^NAME|^"""

```
found 96 matches
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\summarise_paired.py`

```
1→"""Within-pair ratios - the only valid reading of a paired run.

Absolute times drift across a session; the ratio inside a pair does not, because its two
halves ran minutes apart. Node count and objective are printed because they are hardware-
independent: identical values mean the option changed nothing, whatever the clock says.
"""
import sys, json, pathlib, statistics
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\s_bilinear_bounds.py`

```
1→"""Bound only the two variables that appear in the bilinear terms, and nothing else.

The earlier m_bounds put a finite bound on every energy variable in the model and came back
20 % slower. That result was measured against the MIPFocus=1 baseline, which no longer
exists, so it is worth revisiting - but not by repeating it. The reason to expect anything
from bounds here is specific: Gurobi's MINLP guidance says tight bounds matter for the
variables *participating in a bilinear term*, because the McCormick envelope of a product
is built from exactly those bounds and is only as tight as they are.
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\s_breakpoints17.py`

```
1→"""Seventeen tangents on the aging weight curve instead of nine.

The tangent envelope underestimates the convex weight by at most 0.0078 - 0.78 % of the
weight - at nine breakpoints, uniformly across the SoC range. The error falls as 1/(n-1)^2,
so seventeen cuts it to 0.19 % for eight more *linear* rows per bev-step: about 1880 extra
constraints on this instance, no binaries, no change of solver class.

The question is what those rows cost. Every LP-tightening measured in this study has failed
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
520→# sentinels. Prefer raising optimization_MIPGap over capping the clock: a converged 20%
# answer is at least a bounded one.
optimization_time_limit_s           = None
# where Gurobi spends its effort: 0 = balanced (default), 1 = find incumbents,
# 2 = prove the bound, 3 = bound only. Measured, not guessed - see benchmarks/README.md.
#
# This was 1 whenever the fleet was crewed, on the reasoning that under the crew rules
# (3.3.17) finding *any* feasible schedule is the hard part. That is true of the model
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
3340→        'objective_EUR': round(objective, 2),
        'mip_gap': round(mip_gap, 4),
        'solver_status': solver_status,
        # one row per bought truck, so the answer is a shopping list and not only a count
        'fleet_bought': fleet_bought.to_dict('records'),
        # the depot the run charged against - a premise, not a result
        'design_station_kW': design_station_kW,
        'design_stations_offered': len(design_infrastructure),
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\s_bounds_all.py`

```
1→"""Every energy variable bounded - the earlier m_bounds, re-run against the new baseline.

Not a new idea: m_bounds measured 0.80x and was rejected. It is repeated because that
measurement was taken against a MIPFocus=1 baseline that no longer exists, and a parameter
that changes how the search allocates effort can easily change whether extra bound
information helps or merely changes branching for the worse. Re-testing a rejected option
after the thing it was tested against has changed costs one run and prevents a wrong
conclusion from calcifying.
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\t_tuned.py`

```
1→"""What Gurobi's own tuner recommended, and why it is worth almost nothing here.

Run: 1200 s of tuning on day 2, TuneTrials=3 (each candidate scored on three seeds),
TuneCriterion=0 (runtime). The result:

    Tested 2 parameter sets in 928.57s
    Baseline parameter set:     mean 114.04s   (116.58 / 115.70 / 109.85, sd  2.99)
    Improved parameter set 1:   mean 110.08s   (124.00 / 115.19 /  91.03, sd 13.94)  RINS 0
…
```
- soc_aging|E_neg|NumQNZ|bilinear|weighted_thp|qnz

```
found 69 matches
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_bounds.py`

```
1→"""Give the energy variables the finite bounds the constraints already imply.

x_m_t_E, E_pos, E_neg, E_private, E_public and x_m_SoC are all declared with an infinite
upper bound. Every one of them is in fact bounded - by the truck's charging power, by the
inverter, by the battery - but only through constraints, and a bound reached through a
constraint is worth much less to a solver than a bound on the variable itself. Presolve
uses variable bounds to tighten coefficients and to derive implied bounds elsewhere;
bound-based cut generators need them; and the spatial branching this model needs for its
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_lindegrad.py`

```
1→"""Keep the SoC-weighted aging, but carry it as a MILP instead of a bilinear objective.

This is the root cause of the model's difficulty. The aging of a discharged kWh is scaled
by where in the SoC window it was taken from, and that is written as

    weighted_thp = (E_neg / eta_dis) * soc_aging_w

with both factors variables. Five bevs times 47 steps is 235 such products, and Gurobi
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\pdf4_pv_bigm.py`

```
1→"""PDF suggestion 4: split the shared PV big-M into two branch-specific constants.

The model writes the "E_pv_charging is the smaller of surplus and charging demand"
disjunction with one constant for both branches (hdv_disposition_optimization.py:2808):

    big_m = max(surplus_kWh, max_depot_charge_kWh)
    E_pv_charging[t] >= surplus_kWh         - big_m * (1 - surplus_binding)
    E_pv_charging[t] >= depot_charging_kWh  - big_m * surplus_binding
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_seeds.py`

```
1→"""Repeat the shortlist across random seeds, to separate a real saving from a lucky path.

Batch 2 made this necessary rather than optional. Running the same model on 2, 4, 8 and 16
threads gave 729, 812, 376 and 609 seconds - not a scaling curve, because scaling curves
are monotonic. What that spread actually shows is MIP performance variability: changing
anything that perturbs the search - thread count, a parameter, the seed - sends
branch-and-bound down a different path, and paths differ by far more than most of the
effects being measured here.
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\report.py`

```
1→"""Turn the measurement files into the table that answers the question.

Runtime is reported as time to the model's own 10 % target, because that is what a run
costs in practice. A variant that hit the cap instead has no such time, so it is reported
by the gap it reached - and it is a regression, not a saving.

The objective column is not decoration. Two of these options change what is being solved,
and a speed-up bought by solving something else is not a speed-up. Any variant whose
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2865→    # vehicle-to-vehicle (3.3.15c). Those kWh are billed above as if they had been bought
    # at the buy price and sold at the sell price; neither happened, so both overheads come
    # back. The spot price itself is in both and cancels, which is why the saving is the
    # two overheads and does not depend on the market - the same arithmetic that makes own
    # PV worth what it is worth.
2870→    #
    # It creates no incentive to shuffle energy for its own sake: a kWh moved from one
    # truck to another still loses both conversions, and restoring the sending truck costs
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
1090→    return blocked


def apply_real_vehicle_parameters(fleet_df):
    """Optionally override the bev specs of fleet_dataset.xlsx with fixed reference values."""
    if use_real_vehicle_parameters != 'on':
        return fleet_df
    fleet_df = fleet_df.copy()
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
1960→        model       = gp.Model("fleet_disposition")
        sys.stdout  = sys.__stdout__
        sys.stderr  = sys.__stderr__

    # Grid charging prices per kWh, from energy_dataset.xlsx. They price grid electricity
    # only - the share of the depot charging that the site's own PV plant covers is priced
    # separately, see 3.3.15b.
    # Depot charging follows the intraday electricity curve, step by step: it is the same
…
```
- Web search:
- Web search:
- Web search:
- Web search:
- Web search:
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\p_nonconvex_cuts.py`

```
1→"""Push the cut families that exist for the bilinear terms.

The model is a non-convex MIQCP, not a MILP: the SoC-weighted degradation multiplies
E_neg by soc_aging_w. RLT and BQP cuts are what tighten a spatial relaxation, and the log
shows both firing (RLT 42, BQP 3) at default effort."""
import bench_core
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\s_atdepot_binary.py`

```
1→"""Declare at_depot integral instead of leaving it continuous in [0, 1].

at_depot[m,t] is 480 continuous variables that the model only ever means as yes-or-no: the
truck is at the home depot or it is not. Left continuous, the relaxation can put a vehicle
half at the depot, which is precisely the fractional value the depot-charging and V2G gates
hang off - so a fractional at_depot buys half a charging slot the schedule cannot have.

Declaring it binary gives the solver something to branch on and lets presolve propagate it
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\pdf7_ym_upper.py`

```
1→"""PDF suggestion 7: bound y_m above by the real trip assignments it is meant to summarise.

The model has the lower half of the implication only (hdv_disposition_optimization.py:2497):

    y_m[m] >= z_m_f_s[m, f, s]      for every real trip f and start s

so a used vehicle activates y_m, but nothing stops the LP relaxation floating y_m upward
on its own. Adding
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\s_symlex2.py`

```
1→"""Lexicographic vehicle ordering, re-run against the new baseline.

Same reasoning as s_bounds_all: m_symlex measured 1.76x against the old baseline, which was
mostly the baseline's bad seed rather than the constraint. Whether breaking the symmetry
between interchangeable trucks still pays now that the search is bound-focused is a
different question, and an unanswered one.
"""
import bench_core
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\pdf6_startcuts.py`

```
1→"""PDF suggestion 6: pairwise incompatible-start cuts for each route arc.

The chain timing constraint (3.3.16c) is written on the *weighted sums* of the start-time
binaries:

    start_g >= start_f + gap - big_m_time * (1 - link)

which is valid for integral choices but weak in the LP relaxation, because a fractional
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\n_degenmoves.py`

```
1→"""Turn off degenerate simplex moves.

Aimed at a specific symptom: 125 simplex iterations per node in the original log is high,
and time-indexed scheduling models are massively degenerate - many bases describe the same
schedule. The moves that chase those bases can cost more than they return.
"""
import bench_core
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_symlex.py`

```
1→"""Break the symmetry between interchangeable vehicles with a constraint, not a penalty.

The roster holds five ice trucks identical in every coefficient, and five bevs that fall
into three groups by battery warranty: {6,7}, {8}, {9,10}. Within a group the vehicles are
indistinguishable to the model, so every schedule exists once per permutation of the group
and the search can spend whole subtrees re-deriving the same day under a different name.

The model already prefers low ids through penalty_vehicle_id_order * (m-1) * y_m, but a
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2470→        for f in day_trips_list:
            trip_energy = (vehicle_consumption[m] / 100.0) * trip_distances[f]
            for s in event_possible_starts.get(f, []):
                soc_at_departure = x_m_SoC[m, s - 1] if s >= 1 else day_start_level
                Mbig = trip_energy
                model.addLConstr(soc_at_departure >= trip_energy - Mbig * (1 - z_m_f_s[m, f, s]))

    # 3.3.9 v43 REMOVED: old V2G depot site logic + x_l_bev proxy (parking/chg loc x).
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2760→        depot_charging_power_t = gp.quicksum(E_private[m, t] for m in bev_vehicles) / STEP_HOURS
        v2g_power_t = gp.quicksum(E_neg[m, t] for m in bev_vehicles) / STEP_HOURS
        model.addLConstr(
            site_import[t] >= depot_baseline_load_kW[t] + depot_charging_power_t - v2g_power_t - pv_generation_kW[t]
        )
        model.addLConstr(site_import[t] <= site_peak_limit_kW)
        # the demand charge is billed on the highest grid draw of the day
        model.addLConstr(site_peak_kW >= site_import[t])
…
```
- addGenConstr|addSOS|BranchPriority|VarHint|Partition|indicator|addGenConstrMin

```
found 4 matches
```
- Run `python`

```
$ python -c "import gurobipy as gp; print(gp.gurobi.version()); import json, pathlib; 
from collections import Counter
p=pathlib.Path('benchmarks')
for f in sorted(p.glob('results_*.jsonl')):
    rows=[json.loads(l) for l in f.read_text(encoding='utf-8').splitlines() if l.strip()]
    fams=Counter(r.get('family') or r.get('variant','').split('#')[0] for r in rows)
    print(f'{f.name:30} n={len(rows):3}  {dict(fams)}')"
(12, 0, 2)
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2518→        raise ValueError(
            f"full-power depot charging needs the {occupiable} strongest stations to each "
2520→            f"be at least as strong as the strongest bev ({strongest_truck_kW:g} kW), "
            f"because a truck on a weaker station charges at that station's power and the "
            f"greedy rule does not fix which truck that is. "
            f"{len(weak_stations)} of them fall short: "
            f"{[float(s) for s in weak_stations]} kW in the 'charging' sheet of "
            f"depot_dataset.xlsx. Raise those stations, remove them, or reduce the bev "
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2035→        return_steps = return_steps or {}
        link_steps = link_steps or {}
        last_step = time_steps[-1]

        # 3.3.16a a route boundary is an assignment that also drives a leg. Only offered
2040→        # where the leg fits inside the day: a route cannot start before 00:00 or finish
        # after 24:00, so the approach leg has to fit before the trip and the return leg
        # after it. Where neither is possible at any start time the trip has to be chained
…
```
- gurobi_threads|optimization_MIPGap|monte_carlo

```
found 26 matches
```
- Run `python`

```
$ python -c "
import json, pathlib, statistics
from collections import defaultdict

def load(p):
    return [json.loads(l) for l in pathlib.Path(p).read_text(encoding='utf-8').splitlines() if l.strip()]

def show(path):
…
```
- Run `python`

```
$ python -c "
import json, pathlib, statistics
from collections import defaultdict

def load(p):
    return [json.loads(l) for l in pathlib.Path(p).read_text(encoding='utf-8').splitlines() if l.strip()]

def show(path):
…
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\_dump_results.py`
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2620→            # 3.3.11b a plugged-in truck charges at full power, never throttled
            # The caps above only bound E_private from one side, so the optimizer was free
            # to pick any value between a 1 kWh floor and the cap - it would sit at a
            # charger drawing a trickle whenever that suited the peak or the price curve.
            # A depot charger does not modulate: it delivers the lowest of the truck's
            # charging power, the station's power, and what the battery can still take,
            # and nothing less. So E_private is pinned to that minimum rather than
            # bounded by it. Plugging in at all stays the optimizer's decision (z_chg);
…
```
- Run `python`

```
$ python benchmarks/_dump_results.py
==== benchmarks/results_screen.jsonl n= 20
  baseline               seed=1 tot=   119.4 gap=0.0996 obj=1584.00 nodes=8929.0 qnzs=235  pair=None
  n_obbt2                seed=1 tot=   122.5 gap=0.0996 obj=1584.00 nodes=8929.0 qnzs=235  pair=None
  n_obbt3                seed=1 tot=   129.5 gap=0.0993 obj=1570.28 nodes=6341.0 qnzs=235  pair=None
  n_preqlin1             seed=1 tot=   119.0 gap=0.0996 obj=1584.00 nodes=8929.0 qnzs=235  pair=None
  n_preqlin2             seed=1 tot=   118.6 gap=0.0996 obj=1584.00 nodes=8929.0 qnzs=235  pair=None
  n_premiqcp             seed=1 tot=   120.2 gap=0.0996 obj=1584.00 nodes=8929.0 qnzs=235  pair=None
…
```
- Fetch: https://docs.gurobi.com/projects/optimizer/en/current/reference/parameters.html

```
Tool `web_fetch` failed: HTTP request failed: error sending request for url (https://docs.gurobi.com/projects/optimizer/en/current/reference/parameters.html)
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\p_presolve2.py`

```
1→"""Aggressive presolve, aggregation and sparsification.

Presolve already removes about half the rows at default effort (8783 of 17338). Whether
pushing it harder pays for itself on a model this size is exactly the kind of thing that
has to be measured rather than assumed."""
import bench_core
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\n_improvestart.py`

```
1→"""Switch to hunting incumbents once inside 15 %.

A hedge against the change this study already made. Dropping MIPFocus=1 aimed the solver at
the bound, which is what was slow - but the gap is closed from both ends, and once the
bound has come most of the way the cheapest remaining metre may be on the primal side.
ImproveStartGap says: work the bound until 15 %, then switch. It is the phased version of
the decision MIPFocus makes once and for all.
"""
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
1820→        day_routing, approach_steps, return_steps, link_steps,
        trips_driving_steps, approach_driving_steps, return_driving_steps, link_driving_steps)

    # 2.8-0 a design run (3.4) stops here. Everything above is this day's preparation -
    #       its trips, its routes, its prices, its depot curves - and a multi-day model
    #       needs exactly that for each of its days without solving any of them on its own.
    #       Returning the same tuple the single-day path is about to use is what keeps the
    #       two from drifting: there is one preparation, read twice.
…
```
- 3\.3\.17|crew_rules|driver_max

```
found 39 matches
```
- Run `python`

```
$ python -c "import gurobipy as gp; m=gp.Model(); m.setParam('OutputFlag',0); names=sorted(m.Params._json.keys()) if hasattr(m.Params,'_json') else [];
# fallback
import json
p=[]
for a in dir(m.Params):
    if a[:1].isupper():
        p.append(a)
print('\n'.join(p))
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2380→        #
        # The counter is capped at the length of the day, which is the most driving any
        # step could possibly have accumulated, and big_m is set above that cap plus one
        # step. Both matter: the reset works by making the accumulate-forward bound
        # vacuous at the depot, and it is only vacuous if big_m really does exceed
        # everything the counter can hold. With big_m merely equal to the horizon, a
        # counter near its own bound made "counter >= previous + driven - big_m" demand a
        # positive value in the same step the depot forces it to zero - an infeasibility
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2288→        terms = [km * var for vm, var, _fs, _ns, km, _k, _f, _g in deadhead_legs if vm == m]
        return gp.quicksum(terms) if terms else 0.0
2290→
    # 3.3.17 the crew rules
    #
    # A driver is tied to a vehicle exactly while that vehicle is away from the home depot:
    # a truck on a charger needs nobody, a truck on the road or standing in a customer yard
    # needs somebody who cannot leave it until it is home. So `away` is the driver's clock,
…
```
- x_m_t_E|site_import

```
found 27 matches
```
- Run `python`

```
$ python -c "
import gurobipy as gp
m=gp.Model(); m.Params.OutputFlag=0
for name in ['NLPHeur','ConcurrentMethod','SolutionTarget','ObjScale','MIQCPMethod','SimplexPricing','ScaleFlag','Sifting','PreDual','NetworkAlg','LPWarmStart','Quad','NormAdjust','PartitionPlace','FuncNonlinear']:
    try:
        info = gp.GRB.ParamInfo if False else None
    except: pass
    p=getattr(m.Params, name)
…
```
- Run `python`

```
$ python -c "
import gurobipy as gp
m=gp.Model(); m.Params.OutputFlag=0
for name in ['NLPHeur','ConcurrentMethod','SolutionTarget','ObjScale','MIQCPMethod','SimplexPricing','ScaleFlag','Sifting','PreDual','NetworkAlg','LPWarmStart','Quad','NormAdjust','PartitionPlace','FuncNonlinear']:
    try:
        info = gp.GRB.ParamInfo if False else None
    except: pass
    p=getattr(m.Params, name)
…
```
- Updating plan
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\n_obbt2.py`

```
1→"""Optimality-based bound tightening, aggressive.

The parameter Gurobi's own MINLP guidance names first for non-convex models, and one this
study never tried. OBBT solves auxiliary LPs to tighten variable bounds using *all*
constraints at once, where ordinary feasibility-based propagation only pushes each
nonlinearity around on its own. Tighter bounds on the factors of a bilinear term shrink its
McCormick envelope directly, which is the whole difficulty here: 2160 variables in this
model carry an infinite upper bound, E_neg among them, and E_neg is a factor in all 235
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2643→            if t == 0:
                # the day-start level is a constant, so the minimum is one too
                headroom_0 = (1 - initial_soc_fraction) * vehicle_energy_storage[m]
                model.addLConstr(
                    E_private[m, t] == min(best_station_e, battery_factor * headroom_0) * z_chg)
            else:
                # min(constant, linear) needs one binary to state exactly. It is not a
2650→                # free choice: with full_binds = 0 the station side is forced and the caps
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2458→    # SoC implementation review (TODO.txt item 1): ... (v43: same + V2G/CHG now via explicit trip-like events gating E; drive cons separate as motion loss)

2460→    # 3.3.8 SoC has to be sufficient for whole trip distance at start (using trip_distances dict)
    # Tested on the level the truck *departs* with, which is the one at the end of the
    # previous step: x_m_SoC[m, s] is already net of step s's own driving (3.3.6), so
    # testing it asked for the trip's energy plus its own first half hour on top - about
    # trip_energy / duration_steps too much, and double for a one-step trip. That reserve
    # kept trucks off trips they could finish. At s = 0 the level to depart with is the
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2933→                # variables however tightly the second is bounded: this is what keeps the
                # objective linear, and the whole model a MILP, when the weighting is off.
                if t == 0 or soc_weight_factor == 0:
                    soc_w = soc_aging_weight(initial_soc_fraction if t == 0 else 0.5)
                else:
                    soc_w_var = model.addVar(lb=1.0, ub=1.0 + soc_weight_factor,
                                             name=f"soc_aging_w_{m}_{t}{tag}")
2940→                    for x_i, w_i, slope_i in soc_weight_tangents:
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
2794→    max_depot_charge_kWh = sum(vehicle_charging_power[m] for m in bev_vehicles) * STEP_HOURS
    for t in time_steps:
        surplus_kWh = pv_charging_available_kWh[t]
        # the upper bound already carries "no more than the plant has left over", and it
        # fixes the variable to 0 for every step without surplus - at night, and whenever
        # the site's own load alone exceeds the generation
2800→        E_pv_charging[t] = model.addVar(lb=0, ub=surplus_kWh, name=f"E_pv_charging_{t}{tag}")
        if surplus_kWh <= 0:
…
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\_gen_new_variants.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_indicators.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_chgmin.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_logbins.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_nmdt.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_pvmin.py`
- Run `python`

```
$ python benchmarks/_gen_new_variants.py
wrote parameter variants
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_chainind.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_partition.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_branchpri.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_sos1.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_reach.py`
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
1688→    if show_outputs == 'on' and fleet_operation_mode == 'crewed':
        with_break = {f: trips_duration_steps[f] - trips_driving_steps[f]
1690→                      for f in trips_duration_steps
                      if trips_duration_steps[f] > trips_driving_steps.get(f, 0)}
        if with_break:
            added_h = sum(with_break.values()) * STEP_HOURS
            print(f"Lenkzeitpause: {len(with_break)} of {len(trips_duration_steps)} trip(s) "
                  f"drive longer than {driving_time_before_break_minutes / 60:g} h and carry "
…
```
- trip_distances

```
found 18 matches
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py`

```
1280→    #       plus the buying overhead - see 1.4.
    pv_charging_available_kWh = [max(0.0, pv_generation_kW[t] - depot_baseline_load_kW[t]) * STEP_HOURS
                                 for t in time_steps]


build_runtime_context()
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\results\trips.csv`

```
1→day_ID,trip_ID,trip_window_start_time_hhmm,trip_window_end_time_hhmm,trip_distance_km,trip_duration_h,trip_date,trip_start_location,trip_end_location,source_fingerprint
1,1,00:00,24:00,100.6,1.481,2024-01-02,71706 Markgröningen Deutschland,74677 Dörzbach Deutschland,ae18d2fc99a6656b51ba4236d027216f98b4257e2db9f1b0c029425d6a41443b
1,2,00:00,24:00,22.5,0.503,2024-01-02,74547 Untermünkheim Deutschland,74676 Niedernhall Deutschland,ae18d2fc99a6656b51ba4236d027216f98b4257e2db9f1b0c029425d6a41443b
1,3,00:00,24:00,84.5,1.323,2024-01-02,74676 Niedernhall Deutschland,97076 Würzburg Deutschland,ae18d2fc99a6656b51ba4236d027216f98b4257e2db9f1b0c029425d6a41443b
1,4,00:00,24:00,36.7,0.575,2024-01-02,74677 Dörzbach Deutschland,74749 Rosenberg Deutschland,ae18d2fc99a6656b51ba4236d027216f98b4257e2db9f1b0c029425d6a41443b
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_orbitope.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_spatialpri.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_lazycrew.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_qobj.py`
- Run `python`

```
$ python -c "
import pandas as pd
trips=pd.read_csv('results/trips.csv')
d2=trips[trips.day_ID==2]
print('day2 trips', len(d2), 'max km', d2.trip_distance_km.max(), 'median', d2.trip_distance_km.median())
fleet=pd.read_excel('data/fleet_dataset.xlsx')
print(fleet[['vehicle_id','vehicle_type','vehicle_energy_storage','vehicle_consumption']].to_string())
"
…
```
- Run `python`

```
$ python -c "
import pandas as pd
trips=pd.read_csv('results/trips.csv')
d2=trips[trips.day_ID==2]
print('day2 trips', len(d2), 'max km', d2.trip_distance_km.max(), 'median', d2.trip_distance_km.median())
fleet=pd.read_excel('data/fleet_dataset.xlsx')
print(fleet[['vehicle_id','vehicle_type','vehicle_energy_storage','vehicle_consumption']].to_string())
"
…
```
- Edit `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_partition.py`
- Edit `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_reach.py`
- Edit `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_partition.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\_validate_new.py`
- Run `python`

```
$ python benchmarks/_validate_new.py
OK  p_cuts0  apply()
OK  p_cuts1  apply()
OK  p_cuts3  apply()
OK  p_miqcp0  apply()
OK  p_miqcp1  apply()
OK  p_pricing1  apply()
OK  p_pricing2  apply()
…
```
- Write `C:\Users\go25pux\.grok\long-running-background-tasks\watch_bench_new.py`
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new.py`
- Run `python`

```
python -c "
from pathlib import Path
import sys, json, subprocess
HERE=Path('benchmarks')
names=['p_cuts0','m_nmdt','m_logbins','m_indicators','m_pvmin','m_chgmin','m_chainind','m_qobj','m_lazycrew','m_reach','m_sos1','m_partition','m_orbitope']
for name in names:
    proc=subprocess.run([sys.executable,'-u','-X','utf8',str(HERE/'bench_core.py'),name,'2','20','1'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    line=next((l for l in proc.stdout.splitlines() if l.startswith('@@JSON@@')), None)
…
```
- Run `python`

```
python -c "
from pathlib import Path
import sys, json, subprocess
HERE=Path('benchmarks')
names=['p_cuts0','m_nmdt','m_logbins','m_indicators','m_pvmin','m_chgmin','m_chainind','m_qobj','m_lazycrew','m_reach','m_sos1','m_partition','m_orbitope']
for name in names:
    proc=subprocess.run([sys.executable,'-u','-X','utf8',str(HERE/'bench_core.py'),name,'2','20','1'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    line=next((l for l in proc.stdout.splitlines() if l.startswith('@@JSON@@')), None)
…
```
- Run `python`

```
$ python -u -X utf8 benchmarks/bench_core.py p_cuts0 2 20 1
pv_profile_cache.json matches the current order and depot datasets (fingerprint 5faa17f7e5bd) - PVGIS skipped, 366 calendar days loaded.
Set parameter Seed to value 1
Set parameter Seed to value 1
@@JSON@@{"total": 21.23649199999636, "build": 0.2980460000107996, "warm": 0.5748566999682225, "solve": 20.044292000005953, "post": 0.02879630000097677, "vars": 7563, "cons": 16378, "ints": 4056, "nzs": 64519, "qnzs": 235, "status": 9, "gap": 0.18853760402590358, "objective": 1563.6054778252583, "bound": 1268.807047394306, "nodes": 4511.0, "notes": {"Cuts": 0, "seed": 1}, "error": null, "pre": 0.29050100001040846, "variant": "p_cuts0#1"}
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\_smoke_new.py`
- Run `python`

```
$ python -u benchmarks/_smoke_new.py
m_nmdt err= None total= 22.2 qnzs= 235 vars= 9678 status= 9
m_logbins err= None total= 41.0 qnzs= 0 vars= 11793 status= 9
m_indicators err= None total= 21.5 qnzs= 235 vars= 7563 status= 9
m_pvmin err= None total= 23.1 qnzs= 235 vars= 7563 status= 9
m_chgmin err= None total= 21.5 qnzs= 235 vars= 7798 status= 9
m_chainind err= None total= 21.6 qnzs= 235 vars= 7883 status= 9
m_qobj err= None total= 21.5 qnzs= 0 vars= 7798 status= 9
…
```
- ^    '

```
found 9 matches
```
- Run `if (Test-Path`

```
$ if (Test-Path benchmarks/results_new.jsonl) { Remove-Item benchmarks/results_new.jsonl }; python -u -X utf8 benchmarks/run_new.py 2 600 1
[1/43] pair: baseline vs p_cuts0, seed 1
```
- Run `Get-Process python`

```
$ Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, CPU, StartTime, ProcessName | Format-Table -AutoSize; if (Test-Path benchmarks/results_new.jsonl) { Write-Output 'jsonl exists'; Get-Content benchmarks/results_new.jsonl | Measure-Object -Line } else { Write-Output 'no jsonl' }; Get-ChildItem benchmarks/run_new.log, benchmarks/run_new.py -ErrorAction SilentlyContinue | Format-Table Name, Length, LastWriteTime
jsonl exists

[32;1mLines[0m[32;1m Words[0m[32;1m Characters[0m[32;1m Property[0m
[32;1m-----[0m [32;1m-----[0m [32;1m----------[0m [32;1m--------[0m
    0
…
```
- Run `Get-Process`

```
$ Get-Process | Where-Object { $_.ProcessName -match 'python|gurobi' } | Select-Object Id, ProcessName, CPU, StartTime | Format-Table -AutoSize; Write-Output '--- jsonl ---'; Get-Item benchmarks/results_new.jsonl | Format-List FullName, Length, LastWriteTime
--- jsonl ---

[32;1mFullName      : [0mC:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\results_new.jsonl
[32;1mLength        : [0m0
[32;1mLastWriteTime : [0m13.09.2026 14:33:37
```
- Run `python`

```
$ python -u -X utf8 benchmarks/run_new.py 2 600 1 1> benchmarks/run_new.log 2>&1
Background task t-15 started
```
- Run `Get-CimInstance Win32_Process`

```
$ Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId, CommandLine | Format-List; Write-Output '--- log tail ---'; if (Test-Path benchmarks/run_new.log) { Get-Content benchmarks/run_new.log -Tail 8 } else { Write-Output 'no log yet' }; Write-Output '--- jsonl ---'; if (Test-Path benchmarks/results_new.jsonl) { (Get-Content benchmarks/results_new.jsonl | Measure-Object -Line).Lines }

[32;1mProcessId   : [0m22732
[32;1mCommandLine : [0m"C:\Program Files\Python313\python.exe" -u -X utf8 benchmarks/run_new.py 2 600 1

[32;1mProcessId   : [0m516
[32;1mCommandLine : [0m"C:\Program Files\Python313\python.exe" -u -X utf8 
              C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\bench_core.py baseline 2 600.0 1
…
```
- Updating plan
- Run `python`

```
python -u C:\Users\go25pux\.grok\long-running-background-tasks\watch_bench_new.py 22732 86 C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\results_new.jsonl C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new.log
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\p_cuts0.py`

```
1→"""Cuts=0: generate no cutting planes.

This model's cost is the price of each node LP, not how many nodes it takes (DegenMoves=0 doubled node throughput by making the LP cheaper). Gurobi's own tuning slides name Cuts=0 as the first lever when node throughput is the bottleneck, with reported 5x wins on that shape. Cuts also enlarge every node LP, which is the one thing this model cannot afford.
"""
import bench_core


def apply(hdv):
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new.log`

```
1→[1/43] pair: baseline vs p_cuts0, seed 1
```
- Run `python`

```
$ python benchmarks/summarise_paired.py benchmarks/results_pdfpaired.jsonl benchmarks/results_bp.jsonl
option                n  speed-up (median)           range   node x  identical?    d obj
----------------------------------------------------------------------------------------
pdf6_startcuts        3              1.06x      0.86-1.18x     1.14     changed   -0.01%
n_aggregate2          3              1.00x      0.98-1.01x     1.00       NO-OP   +0.00%
pdf1_nonconvex2       3              1.00x      0.87-1.03x     0.73     changed   -0.03%
pdf7_ym_upper         3              0.95x      0.88-1.18x     0.61     changed   +0.08%
s_breakpoints17       3              0.92x      0.80-0.94x     0.82     changed   -0.16%
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new.log`

```
1→[1/43] pair: baseline vs p_cuts0, seed 1
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\summarise_new.py`
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new.log`

```
1→[1/43] pair: baseline vs p_cuts0, seed 1
    baseline                128s  nodes    5512  obj 1562.6016616599918    (1/86)
    p_cuts0                 129s  nodes   28231  obj 1560.7965938250882    (2/86)
[2/43] pair: baseline vs p_cuts1, seed 1
    baseline                 72s  nodes    5512  obj 1562.6016616599918    (3/86)
    p_cuts1                  67s  nodes    8945  obj 1571.3420220248804    (4/86)
[3/43] pair: baseline vs p_cuts3, seed 1
    baseline                 72s  nodes    5512  obj 1562.6016616599918    (5/86)
…
```
- python -u -X utf8 benchmarks/run_new.py 2 600 1 1> benchmarks/run_new.log 2>&1 (t-15)
- Run `python`

```
$ python benchmarks/summarise_new.py benchmarks/results_new.jsonl; Write-Output '--- count ---'; (Get-Content benchmarks/results_new.jsonl | Measure-Object -Line).Lines
option                n  speed-up           range   node x  identical?    d obj  hit cap?
--------------------------------------------------------------------------------------------------
m_pvmin               1     1.24x      1.24-1.24x     1.02     changed   +0.37%          
m_chainind            1     1.21x      1.21-1.21x     1.17     changed   +0.67%          
p_gomory0             1     1.19x      1.19-1.19x     0.65     changed   +1.00%          
p_method2             1     1.10x      1.10-1.10x     0.85     changed   +0.33%          
p_scale2              1     1.07x      1.07-1.07x     0.93     changed   +0.05%
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_pvmin.py`

```
1→"""State E_pv_charging = min(surplus, depot charging) with Gurobi's native min.

The current formulation introduces one binary per surplus step and two big-M rows, and
the M is the max of the two sides - on this fleet ~875 kWh against a surplus of tens of
kWh (PDF suggestion 4). addGenConstrMin lets Gurobi pick the formulation (indicator,
SOS, or a locally valid big-M) and does not need a shared M.

The equality is already what the model means; this only changes how it is written. The
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\m_chainind.py`

```
1→"""Replace the chain-timing big-M with a Gurobi indicator.

    start_g >= start_f + gap - M * (1 - link)

M is last_step plus the longest trip, around 60. A fractional link then lets the two
trips overlap in the LP by most of a route. The indicator "link = 1 => start_g >= start_f
+ gap" is the same integer statement with no M. start_f and start_g are weighted sums, so
they are copied onto auxiliary variables the indicator can take.
…
```
- Run `Get-CimInstance Win32_Process`

```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId, CommandLine | Format-List
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\variants\c_pvchain.py`
- Run `python`

```
$ python -c "import sys; sys.path.insert(0,'benchmarks'); sys.path.insert(0,'benchmarks/variants'); import bench_core, c_pvchain; bench_core.make_container('c_pvchain', c_pvchain.CONTAINER); print('container ok')"
pv_profile_cache.json matches the current order and depot datasets (fingerprint 5faa17f7e5bd) - PVGIS skipped, 366 calendar days loaded.
container ok
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new_replicate.py`
- Run `python`

```
$ python -u -X utf8 benchmarks/run_new_replicate.py 1> benchmarks/run_new_replicate.log 2>&1
Background task t-22 started
```
- Run `Get-CimInstance Win32_Process`

```
$ Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId, CommandLine | Format-List

[32;1mProcessId   : [0m14452
[32;1mCommandLine : [0m"C:\Program Files\Python313\python.exe" -u -X utf8 benchmarks/run_new_replicate.py

[32;1mProcessId   : [0m8888
[32;1mCommandLine : [0m"C:\Program Files\Python313\python.exe" -u -X utf8 
              C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\bench_core.py baseline 2 600.0 7
…
```
- Run `python`

```
python -u C:\Users\go25pux\.grok\long-running-background-tasks\watch_bench_new.py 14452 112 C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\results_new.jsonl C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new_replicate.log
```
- Run `python`

```
$ python -c "
import json, pathlib, statistics
from collections import defaultdict

# historical unpaired screen (MIPFocus=0, DegenMoves=auto) node-identity
rows=[json.loads(l) for l in pathlib.Path('benchmarks/results_screen.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
base=rows[0]
print('SCREEN seed1 baseline', round(base['total'],1), 'nodes', base['nodes'], 'obj', round(base['objective'],2))
…
```
- Run `python`

```
$ python -c "
import json, pathlib, statistics
from collections import defaultdict

# historical unpaired screen (MIPFocus=0, DegenMoves=auto) node-identity
rows=[json.loads(l) for l in pathlib.Path('benchmarks/results_screen.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
base=rows[0]
print('SCREEN seed1 baseline', round(base['total'],1), 'nodes', base['nodes'], 'obj', round(base['objective'],2))
…
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\_hist.py`
- Run `python`

```
$ python benchmarks/_hist.py
SCREEN seed1 baseline 119.4 nodes 8929.0 obj 1584.0
  n_obbt2                122.5s  0.97x  nodes 8929  NO-OP  obj 1584.00
  n_obbt3                129.5s  0.92x  nodes 6341  changed  obj 1570.28
  n_preqlin1             119.0s  1.00x  nodes 8929  NO-OP  obj 1584.00
  n_preqlin2             118.6s  1.01x  nodes 8929  NO-OP  obj 1584.00
  n_premiqcp             120.2s  0.99x  nodes 8929  NO-OP  obj 1584.00
  n_presparsify          160.1s  0.75x  nodes 20947  changed  obj 1567.14
…
```
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new_replicate.log`

```
1→[1/10] pair: baseline vs m_pvmin, seed 7
```
- [monitor] Watch new-option paired screening to completion (t-17)
- Read `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\run_new_replicate.log`

```
1→[1/10] pair: baseline vs m_pvmin, seed 7
    baseline                 88s  nodes    7694  obj 1571.8163326375939    (1/20)
    m_pvmin                  84s  nodes    7493  obj 1574.949777569188    (2/20)
[2/10] pair: baseline vs m_pvmin, seed 13
    baseline                 81s  nodes    7078  obj 1568.3731693889981    (3/20)
    m_pvmin                  75s  nodes    6858  obj 1556.8067999837594    (4/20)
[3/10] pair: baseline vs m_chainind, seed 7
    baseline                 89s  nodes    7694  obj 1571.8163326375939    (5/20)
…
```
- python -u -X utf8 benchmarks/run_new_replicate.py 1> benchmarks/run_new_replicate.log 2>&1 (t-22)
- Run `python`

```
$ python benchmarks/summarise_new.py benchmarks/results_new.jsonl
option                n  speed-up           range   node x  identical?    d obj  hit cap?
--------------------------------------------------------------------------------------------------
c_pvchain             3     1.57x      1.33-1.65x     0.79     changed   +0.28%          
m_chainind            3     1.42x      1.21-1.92x     1.32     changed   -0.13%          
p_cuts1               3     1.35x      1.07-1.50x     0.87     changed   -0.13%          
p_gomory0             3     1.19x      1.06-1.24x     0.74     changed   +0.19%          
p_method2             3     1.10x      0.99-1.29x     0.85     changed   +0.33%
…
```
- Write `C:\Users\go25pux\Desktop\Framework\02_Modell\benchmarks\_final_numbers.py`
- Run `python`

```
$ python benchmarks/_final_numbers.py
==== c_pvchain
  seed   1  base   73.9s n=5512 obj=1562.60  var   55.5s n=6934 obj=1569.13  1.333x  dobj +0.42%  gap 0.0992
  seed  13  base   82.3s n=7078 obj=1568.37  var   50.0s n=6248 obj=1571.81  1.647x  dobj +0.22%  gap 0.0977
  seed   7  base   89.0s n=7694 obj=1571.82  var   56.6s n=9792 obj=1576.29  1.572x  dobj +0.28%  gap 0.0878
  median 1.572x  range 1.333-1.647  saving 36.4%

==== m_chainind
…
```
- Updating plan
- Run `Remove-Item`

```
Remove-Item -ErrorAction SilentlyContinue benchmarks/_dump_results.py, benchmarks/_gen_new_variants.py, benchmarks/_validate_new.py, benchmarks/_smoke_new.py, benchmarks/_hist.py, benchmarks/_final_numbers.py
```
- [monitor] Watch winner replication until 112 records (t-24)

## User

So applying c_pvchain and m_chainind are the best solutions?
Attached: main.py

## Assistant

Not a sign-in issue — signing in again won't fix this. API error (status 402 Payment Required): Grok Build usage balance exhausted
