# Runtime optimization assessment review
## User

read the runtime optimization assessment attached for the code in the current folder. So applying c_pvchain and m_chainind are the best solutions?
Attached: ../01_Material/Laufzeitoptimierung/Untitled-1.md

## Assistant

I'll read the assessment document first.Let me verify the actual variant code in the repo, since the answer hinges on what `c_pvchain` actually contains.No — and the premise needs one correction before the ranking question makes sense.

## c_pvchain already *is* m_chainind

`benchmarks/variants/c_pvchain.py:10`:

```python
CONTAINER = list(m_pvmin.CONTAINER) + list(m_chainind.CONTAINER)   # + GomoryPasses=0
```

`c_pvchain` is the combination container: native PV `min` + **chain-timing indicators** + `GomoryPasses=0`. Applying "both" is just applying `c_pvchain`. They aren't two levers that stack.

## And on the same seeds, the combination isn't measurably better than m_chainind alone

The 36% vs 29% in the table is a *median* of three, and the seed rank-order differs between the two rows. The per-seed pairs (`benchmarks/results_new.jsonl`):

| seed | c_pvchain | m_chainind | winner |
| --- | --- | --- | --- |
| 1 | 1.333× | 1.206× | c_pvchain |
| 7 | 1.572× | **1.921×** | m_chainind |
| 13 | **1.647×** | 1.417× | c_pvchain |
| **mean** | **1.517×** | **1.515×** | tie |

Geometric means 1.511× vs 1.486×. On n=3 with ranges that overlap almost completely (1.33–1.65 vs 1.21–1.92), the two extra components buy nothing you can measure. That tracks with their standalone numbers: `m_pvmin` is 1.07× (range 1.04–1.24, i.e. noise) and `p_gomory0` is 1.19×, yet stacking all three gives 1.52× — less than `m_chainind` × `p_gomory0` would predict. They interfere rather than add.

## The objective column separates them

- `c_pvchain`: +0.42%, +0.28%, +0.22% — **positive on all three seeds**. A systematically dearer incumbent, ~+0.3%.
- `m_chainind`: +0.67%, −0.13%, −0.53% — mean ≈ 0. Noise, not a trend.

So the combination pays a consistent ~0.3% cost premium for a speed-up that is not distinguishable from the single change.

## What I'd actually conclude

**`m_chainind` is the one real finding here** — and it's still not ready to apply.

It's the only option with a clean mechanism: chain timing is written `start_g >= start_f + gap - M*(1-link)` with M ≈ 60, so a fractional `link` lets two trips overlap by most of a route in the LP. The indicator removes M entirely. One change, objective-neutral, and it carries essentially the whole gain. `c_pvchain` wraps it in two changes that don't pay and makes attribution impossible if day 95 goes badly.

But by the study's own veto rule, neither goes into `src/` yet. `results_new.jsonl` is **112 records, all day 2** — day 95 has never been run for either. `ImproveStartGap` is the precedent: best day-2 combination in its round, 0.89× on the hard day.

The missing measurement is ~1.5 h: `m_chainind` and `c_pvchain` paired against baseline on day 95, seeds 1/7/13 (day-95 baseline is ~320 s, so 12 runs). Want me to start that run? If `m_chainind` holds up there, applying it alone is the defensible change.

- Read C:\Users\go25pux\Desktop\Framework\01_Material\Laufzeitoptimierung\Untitled-1.md

```
1	# Constraint Model Runtime Optimization Assessment
2	## User
3	
4	Do a comprehensive runtime optimization assessment of the model without loosing any features. Follow these steps:
5	1. do a deep web and code research on possible options and theories to speed up the runtime for example rewriting the constraints or different optimization and solver parameters but also every other method you can find
6	2. implement und run all found options in separate containers so the original code wont be affected
7	3. show me a summary table of all assessed options with the following results for each: runtime saving, downsides of the implementation
8
…
```
- Read C:\Users\go25pux\Desktop\Framework\01_Material\Laufzeitoptimierung\Untitled-1.md (990 - 1309)

````
990	names=['p_cuts0','m_nmdt','m_logbins','m_indicators','m_pvmin','m_chgmin','m_chainind','m_qobj','m_lazycrew','m_reach','m_sos1','m_partition','m_orbitope']
991	for name in names:
992	    proc=subprocess.run([sys.executable,'-u','-X','utf8',str(HERE/'bench_core.py'),name,'2','20','1'], capture_output=True, text=True, encoding='utf-8', errors='replace')
993	    line=next((l for l in proc.stdout.splitlines() if l.startswith('@@JSON@@')), None)
994	…
995	```
996	- Run `python`
997
…
````
- Find `benchmarks/variants/{c_pvchain,m_chainind,m_pvmin,p_gomory0,p_cuts1}.py`

```
benchmarks\variants\m_pvmin.py
benchmarks\variants\p_cuts1.py
benchmarks\variants\p_gomory0.py
benchmarks\variants\m_chainind.py
benchmarks\variants\c_pvchain.py
```
- Run `ls`

```
$ ls -la "C:/Users/go25pux/Desktop/Framework/02_Modell/benchmarks/containers/" 2>/dev/null
total 24
drwxr-xr-x 1 go25pux 1049089 0 Sep 13 16:36 .
drwxr-xr-x 1 go25pux 1049089 0 Sep 13 17:12 ..
drwxr-xr-x 1 go25pux 1049089 0 Sep 13 16:36 c_pvchain
drwxr-xr-x 1 go25pux 1049089 0 Sep 13 14:20 m_chainind
drwxr-xr-x 1 go25pux 1049089 0 Sep 13 14:20 m_chgmin
drwxr-xr-x 1 go25pux 1049089 0 Sep 13 14:20 m_indicators
…
```
- Read benchmarks\variants\c_pvchain.py

```
1	"""The three screening winners together: native PV min, chain-timing indicators, no Gomory cuts.
2	
3	Each keeps every feature and each looked like a 1.2x on seed 1. Whether they add is the
4	measurement - extra general constraints can easily undo a cheaper cut setting.
5	"""
6	import bench_core
7	import m_pvmin
8	import m_chainind
…
```
- Read benchmarks\variants\m_chainind.py

```
1	"""Replace the chain-timing big-M with a Gurobi indicator.
2	
3	    start_g >= start_f + gap - M * (1 - link)
4	
5	M is last_step plus the longest trip, around 60. A fractional link then lets the two
6	trips overlap in the LP by most of a route. The indicator "link = 1 => start_g >= start_f
7	+ gap" is the same integer statement with no M. start_f and start_g are weighted sums, so
8	they are copied onto auxiliary variables the indicator can take.
…
```
- Read benchmarks\variants\m_pvmin.py

```
1	"""State E_pv_charging = min(surplus, depot charging) with Gurobi's native min.
2	
3	The current formulation introduces one binary per surplus step and two big-M rows, and
4	the M is the max of the two sides - on this fleet ~875 kWh against a surplus of tens of
5	kWh (PDF suggestion 4). addGenConstrMin lets Gurobi pick the formulation (indicator,
6	SOS, or a locally valid big-M) and does not need a shared M.
7	
8	The equality is already what the model means; this only changes how it is written. The
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python benchmarks/summarise_new.py benchmarks/results_new.jsonl 2>&1 | head -30
option                n  speed-up           range   node x  identical?    d obj  hit cap?
--------------------------------------------------------------------------------------------------
c_pvchain             3     1.57x      1.33-1.65x     0.79     changed   +0.28%          
m_chainind            3     1.42x      1.21-1.92x     1.32     changed   -0.13%          
p_cuts1               3     1.35x      1.07-1.50x     0.87     changed   -0.13%          
p_gomory0             3     1.19x      1.06-1.24x     0.74     changed   +0.19%          
p_method2             3     1.10x      0.99-1.29x     0.85     changed   +0.33%
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python -c "
import json, pathlib
from collections import Counter
for f in sorted(pathlib.Path('benchmarks').glob('results_*.jsonl')):
    rows=[json.loads(l) for l in f.read_text(encoding='utf-8').splitlines() if l.strip()]
    if not rows: continue
    days=Counter(r.get('day') for r in rows)
    vs=sorted({r.get('variant','').split('#')[0] for r in rows})
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python -c "
import json, pathlib, statistics
rows=[json.loads(l) for l in pathlib.Path('benchmarks/results_new.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
by={}
for r in rows:
    by.setdefault(r.get('pair'), []).append(r)
for name in ['c_pvchain','m_chainind','m_pvmin','p_gomory0','p_cuts1']:
    print('==',name)
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python -c "
import json, pathlib
rows=[json.loads(l) for l in pathlib.Path('benchmarks/results_day95.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
for r in rows:
    print(f\"{r['variant']:28} tot={r['total']:7.1f} gap={r['gap']:.4f} status={r['status']}\")
"
baseline#1                   tot=  329.4 gap=0.0996 status=2
baseline#7                   tot=  330.5 gap=0.0979 status=2
…
```

## User

start the run
Attached: main.py

## Assistant

Let me check the harness handles day 95 for these containers before committing to a multi-hour run.Containers are source patches and day-independent, so day 95 is fine structurally. Smoke-testing both at a short cap first so a crash doesn't burn two hours:Both build cleanly on day 95 (`qnzs=235` — bilinear objective intact, so no feature is being dropped). Starting the real run:Running. First pair is underway.

**What's running:** `benchmarks/run_paired.py day95new 95 1200 1,7,13 m_chainind c_pvchain`

- 6 pairs (2 variants × seeds 1, 7, 13), each pair is `[baseline, variant]` back-to-back on the same seed — 12 solves total.
- Day 95, 1200 s cap per solve (day-95 baseline ran ~320 s previously, so the cap is headroom, not a target).
- Writes to `benchmarks/results_day95new.jsonl`, log at `benchmarks/run_day95new.log`. Fresh file — day-2 data in `results_new.jsonl` is untouched.
- Nothing in `src/` is modified; both variants run as patched private copies under `benchmarks/containers/`.

Estimate ~1–2 h. The machine slows under sustained Gurobi load, which is exactly why the pairing is adjacent — only the within-pair ratio is the measurement, and absolute times across pairs shouldn't be compared.

I'll report when it finishes. The things I'll be reading out of it: whether `m_chainind` holds a speed-up on day 95 at all (the `ImproveStartGap` failure mode), whether `c_pvchain` separates from it here in a way it didn't on day 2, and whether node counts and objectives confirm the search actually changed rather than the clock drifting.

- Read benchmarks\run_paired.py

```
1	"""Run each variant immediately next to its own baseline, on the same seed.
2	
3	    python benchmarks/run_paired.py <tag> <day> <cap-s> <seeds csv> <variant...>
4	
5	**Use this, not run_matrix.py, for anything that will be quoted.** run_matrix runs all seeds
6	of one variant, then all seeds of the next, which puts hours between a variant and the
7	baseline it is compared against. On this machine that is fatal: sustained multi-threaded
8	Gurobi load slows it by roughly a factor of two, and the drift is larger than almost every
…
```
- Read benchmarks\run_new_replicate.py

```
1	"""Replicate the screening shortlist across seeds 7 and 13, plus the combination."""
2	import run_new
3	import sys
4	
5	# seeds 7,13 for the five that looked faster than noise on seed 1
6	sys.argv = ['run_new.py', '2', '600', '7,13',
7	            'm_pvmin,m_chainind,p_gomory0,p_method2,p_cuts1']
8	run_new.main()
…
```
- Read benchmarks\run_new.py

```
1	"""Paired screening of the new options against the current baseline.
2	
3	    python benchmarks/run_new.py [day] [cap-s] [seeds csv]
4	
5	One seed by default (screening). Re-run with 1,7,13 for anything that is not a no-op
6	and not a clear regression. Pairing is mandatory on this machine: see run_paired.py.
7	"""
8	import sys
…
```
- Read benchmarks\bench_core.py

```
1	"""Runtime assessment harness. Nothing here is imported by the model.
2	
3	Every option under test is a *patch applied at run time* to a freshly imported copy of
4	src/hdv_disposition_optimization.py. The file on disk is never edited, and each variant
5	runs in its own process, so an option that corrupts module state cannot reach the next
6	one or the production code.
7	
8	What is measured, per run:
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && for v in m_chainind c_pvchain; do python -u -X utf8 benchmarks/bench_core.py $v 95 25 1 2>&1 | python -c "
import sys, json
line = next((l for l in sys.stdin if l.startswith('@@JSON@@')), None)
if line is None: print('NO OUTPUT'); sys.exit(1)
r = json.loads(line.replace('@@JSON@@',''))
print(f\"{r['variant']:14} err={r['error']} total={r['total']:.1f} vars={r['vars']} cons={r['cons']} qnzs={r['qnzs']} status={r['status']}\")
"; done
m_chainind#1   err=None total=29.0 vars=8643 cons=17377 qnzs=235 status=9
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python -u -X utf8 benchmarks/run_paired.py day95new 95 1200 1,7,13 m_chainind c_pvchain > benchmarks/run_day95new.log 2>&1
Command running in background with ID: b7wg6o54t. Output is being written to: C:\Users\go25pux\AppData\Local\Temp\claude\C--Users-go25pux-Desktop-Framework-02-Modell\d3f9e9d0-dd89-413e-be8a-ceb384ceb066\tasks\b7wg6o54t.output. You will be notified when it completes. To check interim output, use Read on that file path.
Session cwd remains C:\Users\go25pux\Desktop\Framework\02_Modell; directory changes made by the backgrounded command do not apply to subsequent commands.
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python -c "
import subprocess, pathlib
out = subprocess.run(['powershell','-NoProfile','-Command',
  \"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\" | Select-Object -ExpandProperty CommandLine\"],
  capture_output=True, text=True).stdout
for l in out.splitlines():
    if 'bench' in l or 'run_paired' in l: print('RUNNING:', l.strip()[:130])
p = pathlib.Path('benchmarks/run_day95new.log')
…
```

## User

additionally check all claims in the chats attached and give me a full comprehensive answer of what to do the sped up the runtime of the model
Attached: main.py
Attached: ../../fahre fort.pdf
Attached: ../../I need to improve the runtime of the attached MIP 2.pdf
Attached: ../../I need to improve the runtime of the attached MIQC 1.pdf
Attached: ../../what about the already approximated by 9 piecewise 3.pdf

## Assistant

Let me locate and read the attached PDFs while the benchmark runs.Now let me verify the PDFs' factual claims against the actual code. Starting with the biggest one — PDF 2 calls an unconditional `model.tune()` "very likely your single largest lever":`model.tune()` is commented out (lines 3390–3393). Let me check the remaining structural claims and the build/solve time split:Day 95 has already produced a significant result. Let me finish the code verification while it runs:I have everything I need for the audit. Here it is — the day-95 run is still going, and its first data point already matters.

---

# 1. The four PDFs: two are built on a false premise

**PDF 1 (`MIQC 1`)** opens with: *"despite the 'MIQCP' label, the attached model is actually a pure MILP… zero addQConstr/quadratic objective terms."*
**PDF 4 (`fahre fort`)** repeats it: *"which is actually a MILP — the quadratic degradation term was already linearized via tangent approximation."*

Both are wrong, and `hdv_disposition_optimization.py:2944` is the refutation:

```python
weighted_thp = throughput * soc_w      # throughput = E_neg/eta_dis (variable)
                                       # soc_w      = soc_w_var    (variable)
```

Every benchmark run in this repo records `NumQNZs = 235`. It is a non-convex MIQCP. **PDF 3 (`what about the already approximated by 9 piecewise`) is the one that gets this right** — the 9 tangents linearize `w(x)`, they do not linearize `E_neg · w`. PDF 3 also correctly notes that `NumQConstrs` is zero *because the bilinearity is in the objective*, which is exactly the trap PDFs 1 and 4 fell into: they grepped for `addQConstr`, found none, and concluded MILP.

This matters because PDF 1 then builds its whole strategy on it — *"That's exactly the right strategy, and it's the template for everything else below."* The template is a misreading.

# 2. PDF 2's "single largest lever" is commented-out code

PDF 2's headline claim:

> *"In solve_model, every single model.optimize() call is immediately followed by model.write(...tuningmodel.lp), model.tune(), model.getTuneResult(0)… this can multiply total runtime by several times on every single scenario/day/iteration… **This is very likely your single largest lever.**"*

`hdv_disposition_optimization.py:3389-3393`:

```python
    # 4.4 model tuning
    #model.write(str(project_path('results', 'fleet_disposition_tuning_model.lp')))
    #model.tune()
    #model.getTuneResult(0)
    #model.write(str(project_path('results', 'fleet_disposition_tuning_model_tuneresults.prm')))
```

All four lines are commented out. There is no tuning overhead. The largest claimed win in the four documents is worth exactly zero seconds.

Worse, the tuner *was* run offline by the benchmark study: 1200 s, `TuneTrials=3`, and it returned baseline mean 114.04 s (sd 2.99) vs "improved" 110.08 s (sd 13.94) — the recommendation's own variance is 5× the gain it claims. So PDF 1's *"un-comment model.tune() once and store the .prm"* and PDF 4's *"often finds 2–5× speedups"* are also dead ends, measured.

# 3. PDF 4's parameter block, scored against the measurements

PDF 4 supplies a drop-in `solve_model` replacement and estimates *"3–10× speedup… with zero functional changes."* Every line has now been measured:

| PDF 4 recommends | measured in this repo |
| --- | --- |
| `MIPFocus=1` | **~2× slower** on day 2; day 95 went 0/3 → 3/3 solves when it was set to 0. The study changed it *away* from 1. |
| `Cuts=2` | not re-won vs current baseline. `Cuts=3` **38% slower**. (`Cuts=1` *is* a 1.35× win — the opposite direction.) |
| `Presolve=2` | **0.75×** |
| `Symmetry=2` | NO-OP (bit-identical search) |
| `PreMIQCPForm=1` | NO-OP |
| `PreDual=1` | NO-OP |
| `VarBranch=1` strong branching | **0.49×**; pseudo-cost 55% slower (34k nodes vs 5.5k) |
| `NodeMethod` barrier | **0.13×**, usually hits the cap |
| `Method=2` barrier root | 1.10× median but **one seed is a loss** |
| `Heuristics=0.5`, `RINS=25` | `Heuristics=0` → 0.61×, so heuristics matter; but `NoRelHeurTime` 0.89× and the tuner's `RINS=0` was noise. No win found. |
| `NumericFocus=1` | 1.04×, single seed, unreplicated |
| `ObjectiveScale` | **23% slower** |
| `DegenMoves=0` | ✅ **correct — and already applied** (34% day 2, 20% day 95) |

One of ~15 recommendations is right, and it was already in the code before the PDF was written.

# 4. PDF 4 guessed at a constraint and guessed wrong

> *"site_peak_kW uses max_ over steps — **Not shown but likely** — Replace with epigraph: site_peak_kW >= site_import[t] ∀t. Avoids non-linear max_ if present."*

`hdv_disposition_optimization.py:2767` already is that epigraph. There is no `max_()` or `addGenConstrMax` anywhere in the model. The fix was written before the diagnosis.

# 5. Where the PDFs are genuinely right

All three of PDF 1, 2, 4 independently flag the **chain-timing big-M** (`big_m_time = last_step + max(duration) + 2`, ≈ 50–60 for a gap of a few steps). That is the correct structural diagnosis, and it is the only one they agree on.

The study implemented the *strong* version of it — `m_chainind`, replacing the big-M with `addGenConstrIndicator` — and it was the best single day-2 result at **1.42×**.

**But the day-95 run started this session has just contradicted it.** Seed 1:

| | baseline | m_chainind |
| --- | --- | --- |
| time | 407 s | **617 s** |
| nodes | 25,503 | **57,508** |

0.66× — a **34% slowdown**, with 2.25× the nodes. This is the `ImproveStartGap` failure mode repeating exactly: best day-2 idea, reverses on the hard day. One seed of three; two more are running.

# 6. Ideas the PDFs propose that were already tested and lost

| PDF proposal | container | result |
| --- | --- | --- |
| Prune `z` by reachability/range (PDF 1 §2, PDF 4 §1) | `m_reach` | **NO-OP** — presolve already proves `SoC ≤ cap < trip energy` infeasible |
| Symmetry-break identical vehicles (all four) | `m_symlex`, `s_symlex2`, `m_orbitope` | 0.62×, re-tested, 11% slower |
| Discretize SoC×discharge to reach a true MILP (**PDF 3's prescription**) | `m_logbins`, `m_lindegrad`, `m_nmdt` | **61% slower** (+2.1% obj), 0.88×, 25% slower (+1.15%) |
| Indicator for SoC-at-departure (PDF 1 §2) | `m_indicators` | ~10% slower |
| `addGenConstrPWL`/native min for charging derate (PDF 1, PDF 4 §5b) | `m_chgmin` | 18% slower |
| Branching priorities (PDF 1 §4) | `m_branchpri`, `m_spatialpri` | 1.05× single seed; NO-OP |
| Warm start everywhere (PDF 1 §4, PDF 4 §4) | already implemented; `a_nowarm` | dropping it is 0.97× → keep it |

PDF 3 deserves credit for correct diagnosis and a fair caveat (*"not an exact representation… a controlled MILP approximation"*), but its cure is the single worst-performing family in the study.

# 7. Proposals sold as "zero functional change" that are not

- **"Aggregate time steps where possible, merge 2–4 steps"** (PDF 4) — changes the 30-min resolution, changes which schedules are feasible. Feature loss.
- **"Ensure `soc_weight_tangents` has minimal breakpoints (4–5 is usually enough)"** (PDF 4) — the tangent envelope's error is 0.78% at 9 breakpoints and scales as 1/(n−1)²; at 4 it's ~5%. That changes the objective, not the runtime.
- **Rolling horizon / fix-and-dive / local branching** (PDF 4 §4) — heuristics. They discard the optimality certificate. The study's `r_montecarlo3` is this class: ~5× faster, infeasible on some days, *"not a legal speed-up."*
- **Type-level vehicle aggregation** (PDF 1 §3) — drops per-truck EFC tracking, which PDF 1 itself flags.
- **"MIPFocus=1 for parameter studies, 0 for publication runs"** (PDF 1 §4) — two different answers from the same model, and the faster of the two is 0 anyway.

# 8. Build time is 0.4%, not 30%

PDF 1 (*"build-time is also worth attacking… model construction can exceed the solve time"*), PDF 2 (*"repeated getVarByName calls in loops"*), PDF 4 (*"if build > 30% of total, optimize data structures"*) all target model construction. Measured, day-2 baseline:

```
total=71.9s   pre=0.30   build=0.30   warm=0.80   solve=70.4   post=0.03
```

Build is 0.4% of wall clock. Even eliminating it entirely is unmeasurable. (The caveat is fair for the *multi-day design* run, which is a different code path and was never benchmarked — `getVarByName` genuinely is used in loops there, at `:3161` and in postprocessing.)

---

# What to actually do

**Ranked, with the evidence state marked honestly.**

### Already applied — keep
`MIPFocus=0` (~50% on day 2; day 95 went 0/3 → 3/3) and `DegenMoves=0` (34% day 2, 20% day 95). These are the two real wins and they are in `src/` already. Everything else in this study is looking for a third.

### Apply nothing else until the day-95 run finishes
`m_chainind` is at 0.66× on day 95 seed 1. If seeds 7 and 13 confirm it, the entire day-2 shortlist — `m_chainind`, `c_pvchain`, and by association `p_cuts1` and `p_gomory0`, which were never run on day 95 either — has to be treated as day-2 overfitting rather than as findings. I'll report when it lands.

### The strongest untested lever is not in any of the PDFs
**Gurobi 13.** The vendor claims **2.68× on hard non-convex MIQCP** vs 12.0, which is precisely this model's class. The environment is Gurobi 12.0.2. That is a larger claimed effect than anything in 43 measured containers, it requires no formulation change, and it cannot overfit to day 2. None of the four documents mentions it.

### Three untested formulation candidates worth a container each
1. **Per-pair chain big-M** — PDF 1/2's *literal* suggestion, `bigM[f,g]` computed from the sampled start lists, which is **not** what `m_chainind` did. `m_chainind` removed M entirely via indicators and is now looking bad on day 95; the milder version tightens the same relaxation without 2 auxiliary variables per chain or the indicator machinery. Given the day-95 result, this is now the more interesting of the two.
2. **Relax `break_active` and `penalized_external` to continuous [0,1]** — 480 binaries (~12% of the 4,056 in the day-2 model). Valid without changing the feasible set: the penalty pushes `penalized_external` down onto `x_ext ∧ ¬break`, and the relief pushes `break_active` up onto `∨z`, so both land on their integer values at any integral `z`/`x`. Neither PDF proposes this; PDF 2 comes closest but claims both are *"fully determined via equalities"* — wrong for `penalized_external`, which is an AND (a product), stated with the standard three-inequality linearization at `:3006-3008`. The study's own `s_atdepot_binary` result (declaring a determined variable binary cost 12%) points the same way.
3. **Eliminate `E_public`** via `E_private + E_public == E_pos` (`:2617`) — PDF 1's one clean structural idea. Likely already handled by `Aggregate=1` presolve, but it has never been built, and it's cheap to check.

### Do not revisit
Everything in §6 and §7. That's ~35 measured dead ends, and re-deriving them from the PDFs would cost days.

---

**Status:** day-95 pairing is on pair 2 of 6 (`benchmarks/run_day95new.log`, writing `results_day95new.jsonl`). Nothing in `src/` has been touched. I'll come back with the full six pairs.

- Run `ls`

```
$ ls -la "C:/Users/go25pux/Desktop/"*.pdf 2>/dev/null
-rw-r--r-- 1 go25pux 1049089 363601 Sep 13 14:06 C:/Users/go25pux/Desktop/I need to improve the runtime of the attached MIP 2.pdf
-rw-r--r-- 1 go25pux 1049089 443821 Sep 13 14:06 C:/Users/go25pux/Desktop/I need to improve the runtime of the attached MIQC 1.pdf
-rw-r--r-- 1 go25pux 1049089 569194 Sep 14 00:36 C:/Users/go25pux/Desktop/fahre fort.pdf
-rw-r--r-- 1 go25pux 1049089 624098 Sep 13 14:06 C:/Users/go25pux/Desktop/what about the already approximated by 9 piecewise 3.pdf
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop" && python -c "
import sys
try:
    import pypdf; print('pypdf', pypdf.__version__)
except ImportError:
    try:
        import PyPDF2; print('PyPDF2', PyPDF2.__version__)
    except ImportError: print('none')
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop" && python -c "
import pypdf, pathlib
for f in sorted(pathlib.Path('.').glob('*.pdf')):
    r = pypdf.PdfReader(str(f))
    txt = '\n'.join((p.extract_text() or '') for p in r.pages)
    out = pathlib.Path('/tmp/pdftxt') / (f.stem + '.txt')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(txt, encoding='utf-8')
…
```
- Read C:\tmp\pdftxt\I need to improve the runtime of the attached MIQC 1.txt

```
1	Read through the whole builder. One important ﬁnding ﬁrst: despite the "MIQCP" label, the
2	attached model is actually a pure MILP in its current form — there are no quadratic constraints
3	le  in the code (addLConstr everywhere, zero addQConstr/quadratic objective terms). The
4	quadratic part was already eliminated once: the SoC-dependent aging weight
5	 is approximated by 9 piecewise-linear tangent lines precisely
6	because "carrying the square itself made the model quadratically constrained and cost far more
7	solver time than the shape is worth". That's exactly the right strategy, and it's the template for
8	everything else below.
…
```
- Read C:\tmp\pdftxt\I need to improve the runtime of the attached MIP 2.txt

```
1	This model is a large-scale Gurobi MILP for daily (and multi-day "design") electric heavy-duty
2	ﬂeet disposition. It combines a set-partitioning/assignment core (z[m,f,s] binaries covering
3	every trip exactly once) with vehicle-routing ﬂow-balance (routestart/routeend/chain), an
4	intraday battery/SoC and V2G energy-scheduling layer, crew/driving-time tracking via
5	cumulative big-M counters, and (in the design variant) a ﬂeet-sizing binary layer. Below is the
6	problem classiﬁcation and concrete, functionality-preserving speedups, ordered roughly by
7	expected impact.
8	Structurally this is a combined Electric Vehicle Scheduling Problem (E-VSP) with time
…
```
- Read C:\tmp\pdftxt\what about the already approximated by 9 piecewise 3.txt

```
1	No. The nine piecewise-linear tangents make the aging-weight function  itself
2	linearizable, but your implemented degradation cost still multiplies that variable weight by a
3	continuous discharge-energy variable. That remaining product means the model is not a full
4	MILP when advanced_degradation_status == 'on' and soc_weight_factor > 0.
5	You approximate the convex aging weight
6	using tangent inequalities such as
7	That part is entirely linear. In the code, this is the section that creates soc_w_var and adds the
8	tangent constraints:
…
```
- Read C:\tmp\pdftxt\fahre fort.txt

```
1	Now I have a thorough understanding of the model structure. Let me provide a comprehensive
2	answer on runtime optimization techniques.
3	Based on my analysis of your hdv_disposition_optimization.py (which is actually a MILP — the
4	quadratic degradation term was already linearized via tangent approximation), here are
5	concrete techniques to improve solve time without changing functionality:
6	Technique Where in Your Model Why It Helps
7	Tighten
8	event_possible_starts /
…
```
- grep -n -C 3 "tune\(\)|getTuneResult|tuningmodel|model\.write\(|TuneCriterion|\.prm" C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py

```
3387-    model.optimize()
3388-
3389-    # 4.4 model tuning
3390:    #model.write(str(project_path('results', 'fleet_disposition_tuning_model.lp')))
3391:    #model.tune()
3392:    #model.getTuneResult(0)
3393:    #model.write(str(project_path('results', 'fleet_disposition_tuning_model_tuneresults.prm')))
3394-
…
```
- grep -n -A 30 "def solve_model" C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py

```
3359:def solve_model(model, optimization_MIPGap, gurobi_threads):
3360-    # 4.2 set model parameters
3361-    model.setParam('OutputFlag', 0)
3362-    model.setParam('LogToConsole', 0)
3363-    model.setParam('MIPGap', optimization_MIPGap)
3364-    model.setParam('Threads', gurobi_threads)
3365-    # No wall-clock cap by default: the solver runs until it meets optimization_MIPGap.
3366-    # Set explicitly rather than left to Gurobi's default so the intent is on the page -
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && ls benchmarks/variants/*.py | sed 's#.*/##;s#\.py##' | tr '\n' ' '; echo; echo "--- build vs solve split (day2 baseline, seed1) ---"; python -c "
import json, pathlib
rows=[json.loads(l) for l in pathlib.Path('benchmarks/results_new.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
b=[r for r in rows if r['variant'].startswith('baseline')][:3]
for r in b:
    print(f\"total={r['total']:6.1f}  pre={r['pre']:5.2f}  build={r['build']:5.2f}  warm={r['warm']:6.2f}  solve={r['solve']:6.1f}  post={r['post']:4.2f}\")
"
a_nowarm a_nowarm2 c_all3 c_all3_nowarm c_best c_degen_bounds c_degen_improve c_lin_focus c_pvchain m_bounds m_branchpri m_chainind m_chgmin m_indicators m_lazycrew m_lindegrad m_logbins m_nmdt m_orbitope m_partition m_pvmin m_qobj m_reach m_sos1 m_spatialpri m_symlex n_aggregate2 n_cutpasses n_degenmoves n_disconnected n_heur0 n_improvestart n_mipfocus2 n_mipfocus3 n_nodemethod2 n_norelheur5 n_obbt2 n_obbt3 n_premiqcp n_preqlin1 n_preqlin2 n_presparsify n_varbranch3 p_aggregate0 p_branchdir1 p_clique2 p_combo p_concurrent2 p_concurrent4 p_cuts0 p_cuts1 p_cuts2 p_cuts3 p_flowcover2 p_focus_cuts p_gomory0 p_heurskip p_implied2 p_integrality1 p_lpwarm2 p_method1 p_method2 p_mipfocus0 p_mipfocus2 p_mipfocus3 p_miqcp0 p_miqcp1 p_network2 p_nlheur0 p_nonconvex_cuts p_norelheur p_numeric1 p_objscale p_predual2 p_presolve2 p_pricing1 p_pricing2 p_quad1 p_scale2 p_sifting2 p_symmetry0 p_symmetry2 p_threads2 p_threads4 p_threads8 p_varbranch1 p_zerohalf2 pdf1_nonconvex2 pdf4_pv_bigm pdf6_startcuts pdf7_ym_upper r_degen_auto r_montecarlo3 r_socweight_off s_atdepot_binary s_bilinear_bounds s_bounds_all s_breakpoints17 s_symlex2 t_tuned
…
```
- grep -n | head -40 "max_\(|addGenConstrMax|site_peak_kW|E_pos\[m, t\] ==|E_private\[m, t\] \+ E_public" C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py

```
2617:            model.addLConstr(E_private[m, t] + E_public[m, t] == E_pos[m, t])
2757:    site_peak_kW = model.addVar(lb=0, ub=site_peak_limit_kW, name=f"site_peak_kW{tag}")
2767:        model.addLConstr(site_peak_kW >= site_import[t])
2962:    peak_shaving_cost = (peak_power_price_eur_per_kW / 365.0) * (site_peak_kW - baseline_peak_kW)
3919:        # Recomputed from the primitives rather than read from the site_peak_kW variable:
3921:        site_peak_kW_value = 0.0
3928:            site_peak_kW_value = max(site_peak_kW_value, grid_draw_kW)
3934:        bev_peak_increment_kW = site_peak_kW_value - baseline_peak_kW_value
…
```
- grep -n | head -40 "E_public|E_pos" C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py

```
729:# Everything the model *buys, sells and meters* (E_private, E_public, E_neg) is grid-side
1988:    E_pos           = model.addVars(bev_vehicles, time_steps, lb=0, name=f"E_pos{tag}") #bev-only: E_posg (kWh) = max(0, E)
1991:    E_public        = model.addVars(bev_vehicles, time_steps, lb=0, name=f"E_public{tag}")
2418:    # v43: drive consumption only from real trips (as before). V2G discharge (to grid) and CHG (from grid) affect via x_m_t_E (E_neg/E_pos).
2479:    # Gating of E_neg / E_pos / x_E moved into the per-t power limits block below using v2g_z / chg_z.
2557:            # the cap is a station power, so it bounds the *metered* draw E_pos. What
2563:            # E_pos <= same caps (for earnings/peak calcs)
2564:            model.addLConstr(E_pos[m, t] <= charge_e_cap)
…
```
- grep -n -C 8 | head -80 "event_possible_starts\s*=|possible_start_times\s*=|def .*possible_start|fleet_pool_copies_for" C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py

```
350-#
351-#      'synthetic_fleet' lists vehicle *types*, one per row, with no vehicle_id: these are
352-#      the trucks that could be bought, not trucks that exist. The design model may take as
353-#      many of each as it needs, so the pool below instantiates every type `copies` times
354-#      and hands the optimizer one ownership binary per copy.
355-#
356-#      The copy count is a cap on the answer, so it has to be generous enough not to be the
357-#      binding constraint and small enough to solve. It is derived from the work by default
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && cat benchmarks/run_day95new.log
[1/6] pair: baseline vs m_chainind, seed 1
    baseline                407s  nodes   25503  obj 2941.598036055132
    m_chainind              617s  nodes   57508  obj 2945.1953027455947
[2/6] pair: baseline vs m_chainind, seed 7
    baseline                253s  nodes   19139  obj 2941.0673421971023
```
- Read src\hdv_disposition_optimization.py (1629 - 1688)

```
1629	        trip_duration_h = row['trip_duration_h']
1630	        drive_steps = int(round(trip_duration_h * 2))
1631	        # 2.6b the Lenkzeitpause a long trip carries inside it.
1632	        #
1633	        # The router returns pure driving time, and a driver may not drive more than
1634	        # driving_time_before_break_minutes without stopping for
1635	        # driving_break_duration_minutes. A 6 h run is therefore a 6 h 45 min job, and
1636	        # booking it as 6 h would let the fleet schedule driving no driver may legally do.
…
```
- grep -n | head -30 "soc_weight_tangents\s*=|break_active|penalized_external" C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py

```
2890:        soc_weight_tangents = soc_aging_weight_tangents()
2965:        break_active = model.addVars(bev_vehicles, time_steps, vtype=gp.GRB.BINARY, name=f"break_active{tag}")
2966:        penalized_external = model.addVars(bev_vehicles, time_steps, vtype=gp.GRB.BINARY, name=f"penalized_external{tag}")
2977:        # sparse precompute: only add break_active links for valid (m,t,f,s) tuples
2990:        # break_active has to be pinned from both sides. It only ever *relieves* a penalty
2998:                model.addLConstr(break_active[m, t] >= z_var)
2999:            model.addLConstr(break_active[m, t] <= gp.quicksum(z_vars))
3003:                    model.addLConstr(break_active[m, t] == 0)
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && cat benchmarks/run_day95new.log
[1/6] pair: baseline vs m_chainind, seed 1
    baseline                407s  nodes   25503  obj 2941.598036055132
    m_chainind              617s  nodes   57508  obj 2945.1953027455947
[2/6] pair: baseline vs m_chainind, seed 7
    baseline                253s  nodes   19139  obj 2941.0673421971023
```
- grep -n "weighted_thp|throughput \* soc_w|soc_w_var" C:\Users\go25pux\Desktop\Framework\02_Modell\src\hdv_disposition_optimization.py

```
2928:                # the coefficient on soc_w_var is efc_base * E_neg / (2*cap) >= 0 the
2938:                    soc_w_var = model.addVar(lb=1.0, ub=1.0 + soc_weight_factor,
2942:                            soc_w_var >= w_i + slope_i * (x_m_SoC[m, t-1] / cap - x_i))
2943:                    soc_w = soc_w_var
2944:                weighted_thp = throughput * soc_w
2945:                degrad_contrib = efc_base * weighted_thp / (2.0 * cap)
```

## Assistant

Result is unambiguous. Let me get exact numbers and confirm nothing hit the cap:The day-95 run finished. It answers your original question decisively — and in the opposite direction.

# Result: apply neither

All 12 solves converged to the 10% target (`status=2`, gaps 0.096–0.100). No cap hits, so these are clean like-for-like comparisons.

| variant | seed 1 | seed 7 | seed 13 | median | vs day 2 |
| --- | --- | --- | --- | --- | --- |
| `m_chainind` | 0.661× | 0.338× | 0.579× | **0.58× (73% longer)** | was 1.42× |
| `c_pvchain` | 0.491× | 0.440× | 0.502× | **0.49× (104% longer)** | was 1.57× |

**6 out of 6 losses.** Not one seed came within 0.67× of break-even. `c_pvchain`'s range is 0.440–0.502 — it is almost exactly 2× slower, every time.

The mechanism is visible in the node counts: baseline 19k–42k, `m_chainind` 57k–58k, `c_pvchain` 75k–81k. The indicator reformulation doubles-to-quadruples the tree on this instance. Objective deltas are noise (±0.3%), so nothing is being bought with the time — it is pure loss.

Note also that `c_pvchain` is *worse* than `m_chainind` alone here (0.49× vs 0.58×). The two extra components — `m_pvmin` and `GomoryPasses=0` — that contributed nothing measurable on day 2 actively hurt on day 95. That's consistent across both days: they were never doing anything good.

## Why day 2 misled

Day 95 is not a bigger instance. It's **15 trips vs 13, with *less* total driving** (29.8 h vs 33.6 h). Yet its baseline takes ~280 s against day 2's ~75 s. It's combinatorially harder at essentially the same size.

So this isn't "the optimization doesn't scale." It's that instance identity — not instance size — decides whether LP tightening pays, and a 13-trip day tells you nothing about a 15-trip day. That makes the study's screening protocol (screen on day 2, replicate on more day-2 seeds) structurally unable to catch this. Day 95 needs to be *in* the screen, not the veto at the end.

## Where that leaves the runtime work

**Validated and already in `src/`:** `MIPFocus=0` and `DegenMoves=0`. These are the only two changes ever measured as wins on *both* days. They remain the entire result of the study.

**Now falsified:** `c_pvchain`, `m_chainind`. Nothing was written to `src/` — the containers were never applied, so there's nothing to revert.

**Now suspect by association:** `p_cuts1` (1.35×) and `p_gomory0` (1.19×) were also day-2-only. `p_gomory0` sits inside `c_pvchain`, which lost by more than `m_chainind` alone — weak evidence it contributes to the day-95 damage. Neither should be quoted as a saving until paired on day 95. That's a ~40-minute run if you want it.

**Still the largest untested lever, and in none of the four PDFs:** **Gurobi 13** — vendor-claimed 2.68× on hard non-convex MIQCP versus 12.0, which is exactly this model's class. This environment is 12.0.2. It requires no formulation change and therefore cannot overfit to an instance, which is the failure mode that just destroyed the day-2 shortlist. Given today's result, I'd put this well ahead of any further reformulation work.

**The three untested formulation candidates** from my audit (per-pair chain big-M, relaxing `break_active`/`penalized_external` to continuous, eliminating `E_public`) are still coherent ideas — but the honest prior on them just got much worse. Two of the three are LP-tightening, which is the exact family that has now failed on day 95 twice. If you pursue them, screen them on day 95 first and treat day 2 as the confirmation, inverting the old protocol.

Files: `benchmarks/results_day95new.jsonl`, `benchmarks/run_day95new.log`. I've also noted the day-2/day-95 divergence in project memory so it doesn't get re-derived.

- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && cat benchmarks/run_day95new.log
[1/6] pair: baseline vs m_chainind, seed 1
    baseline                407s  nodes   25503  obj 2941.598036055132
    m_chainind              617s  nodes   57508  obj 2945.1953027455947
[2/6] pair: baseline vs m_chainind, seed 7
    baseline                253s  nodes   19139  obj 2941.0673421971023
    m_chainind              748s  nodes   58496  obj 2934.328573804665
[3/6] pair: baseline vs m_chainind, seed 13
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python -c "
import json, pathlib, statistics
rows=[json.loads(l) for l in pathlib.Path('benchmarks/results_day95new.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
by={}
for r in rows: by.setdefault(r['pair'],[]).append(r)
for name in ['m_chainind','c_pvchain']:
    rs=[]
    print('==',name)
…
```
- Run `cd`

```
$ cd "C:/Users/go25pux/Desktop/Framework/02_Modell" && python -c "
import pandas as pd
t=pd.read_csv('results/trips.csv')
for d in (2,95):
    x=t[t.day_ID==d]
    print(f'day {d:3}: trips={len(x):3}  km total={x.trip_distance_km.sum():7.1f}  max={x.trip_distance_km.max():6.1f}  drive_h={x.trip_duration_h.sum():5.2f}')
"
day   2: trips= 13  km total= 2486.8  max= 430.7  drive_h=33.56
…
```
- Run `ls`

```
$ ls -la "C:/Users/go25pux/.claude/projects/C--Users-go25pux-Desktop-Framework-02-Modell/memory/" 2>&1 | head -20
total 4
drwxr-xr-x 1 go25pux 1049089 0 Aug 28 17:50 .
drwxr-xr-x 1 go25pux 1049089 0 Sep 14 00:31 ..
```
- Write C:\Users\go25pux\.claude\projects\C--Users-go25pux-Desktop-Framework-02-Modell\memory\day2-benchmark-wins-do-not-transfer.md

```
File created successfully at: C:\Users\go25pux\.claude\projects\C--Users-go25pux-Desktop-Framework-02-Modell\memory\day2-benchmark-wins-do-not-transfer.md
```
- Write C:\Users\go25pux\.claude\projects\C--Users-go25pux-Desktop-Framework-02-Modell\memory\MEMORY.md

```
File created successfully at: C:\Users\go25pux\.claude\projects\C--Users-go25pux-Desktop-Framework-02-Modell\memory\MEMORY.md (file state is current in your context — no need to Read it back)
```
