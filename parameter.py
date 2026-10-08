"""Run parameters - edit, save, run. This is the only file a run needs you to open.

Every name below is a parameter of src/hdv_disposition_optimization.py and replaces the
default it carries there. Delete a line to fall back to that default. A name that is not
a model parameter raises on import instead of being silently ignored, so a typo here is
loud. The long form of any of these, with the reasoning, is in that file's sections
1.3-1.5 and in the README.

The web interface does not read this file: its sidebar is its own set of controls.
"""

# 1.3 variation parameters ---------------------------------------------------------
order_data_days = [3, 3] # first, last day of order_trips.csv to plan (1-127)
scenario                            = ['best case']  # 'best case' (bev low, ice high) | 'worst case'
scenario_year                       = [2025]      # 2025...2045
v2g_status                          = ['on']      # 'on' | 'off'
work_hours_start                    = '06:00'     # earliest a trip or its empty leg may start
work_hours_end                      = '18:00'     # latest a trip or its empty leg may end
date_disposition                    = '07.11.2025'  # DD.MM.YYYY; curve base date only - the day is order_data_days
home_depot_location                 = '74635 Kupferzell Deutschland'
route_chaining_status               = 'on'        # 'off' = no geography: any trip after any other
location_tolerance_share            = 0.10        # "same place" radius, share of median trip distance
route_nearest_link_candidates       = 1           # nearest trips a finishing trip may chain to

# 1.3 runtime parameters -----------------------------------------------------------
optimization_MIPGap                 = 0.01        # 0...1
optimization_time_limit_s           = None        # [s] per solve; None = run to the gap
parallel_worker_limit               = 4           # concurrent Gurobi sessions in a sweep
optimization_MIPFocus               = 0           # 0 balanced | 1 incumbents | 2 prove bound | 3 bound only
optimization_DegenMoves             = 0           # 0 = Gurobi default
optimization_presolve               = -1          # -1 auto | 0 off | 1 conservative | 2 aggressive
plot_degradation_weight_curve       = False       # diagnostic only
plot_run_cost_parameters            = False       # diagnostic only
auto_sizing                         = 'off'       # 'on' = design a fleet instead of dispatching one
design_max_fleet_evals              = None        # None = no cap
design_max_search_seconds           = 7200        # [s] whole design search
design_search_day_limit_s           = 300         # [s] per day while searching
design_final_day_limit_s            = None        # [s] per day on the final re-solve
design_bound_limit_s                = 300         # [s] per floor solve
design_parallel_days                = 'auto'      # 'auto' | int

# 1.4 feature parameters -----------------------------------------------------------
fleet_operation_mode                = 'crewed'    # 'crewed' | 'autonomous'
battery_price_share                 = 0.40        # share of vehicle price that is battery
external_charging_status            = 'on'        # allow public charging, priced and time-penalised
driver_hourly_rate_eur              = 20.0        # reported only; the objective carries no wage
driver_max_driving_hours            = 9.0         # Lenkzeit [h], driving only
driver_max_working_hours            = 9.0         # Arbeitszeit [h], whole duty
driving_time_before_break_minutes   = 270         # [min] driving before a Lenkzeitpause is due
driver_mandatory_break_hours        = 0.75        # [h] length of that break
driver_shift_limit                  = 'hard'      # 'hard' = constraint | 'priced' = slack
penalty_crew_rule_breach            = 500.0       # EUR per half hour over a limit, 'priced' only
penalty_driver_use                  = 20.0        # EUR per driver on the day's peak roster
driver_roster_feedback              = 'on'        # re-solve once if the roster needs more heads
driver_warm_start                   = 'off'       # 'on' solves the relaxed day first
driver_warm_start_MIPGap            = 0.30        # gap of that relaxed solve
driver_warm_start_max_seconds       = 15          # [s] budget for it
site_peak_limit_kW                  = 2000        # hard cap on depot grid import [kW]
peak_power_price_eur_per_kW         = 17          # DSO demand charge [EUR/kW/year]
grid_energy_overhead_eur_per_kWh    = 0.15        # fees, levies, taxes, margin on top of spot when buying
energy_selling_overhead_eur_per_kWh = 0.00        # deducted from spot when selling (PV and V2G alike)
# which sheet of costs_dataset.xlsx this run is priced from. All four prices follow it.
# 'daily'  = 'energy_daily' alone, as written. The two curves keep their own shape AND
#            level; the diesel and public charging prices are that sheet's. The scenario
#            year and band reach nothing - that sheet has neither axis.
# 'yearly' = 'energy_yearly' at the scenario year and band, with the shape of
#            'energy_daily' rescaled onto it so arbitrage still has a spread to trade.
# 'auto'   = 'daily' for a disposition run, 'yearly' for a sizing run or a sweep. The
#            default, and the one to leave alone: it follows the kind of run, which the
#            entry point already knows. One combination of the variation lists above is a
#            disposition run; several are a sweep.
# The diesel price is flat either way; the basis only decides which sheet states it.
energy_price_basis                  = 'auto'      # 'auto' | 'daily' | 'yearly'
diesel_truck_toll_eur_per_km        = 0.183       # ice HDV toll
bev_truck_toll_eur_per_km           = 0.0         # bev HDV toll, often reduced
pv_allow_synthetic_profile          = 'off'       # 'on' accepts a bell curve if PVGIS is unreachable
pv_profile_retries                  = 3           # PVGIS attempts before giving up
pv_profile_retry_backoff_s          = 5.0         # [s] linear backoff between them
v2v_status                          = 'on'        # truck-to-truck energy, never crossing the meter
v2g_price_mode                      = 'both'      # 'arbitrage' | 'flexibility' | 'both' (the better of the two)
v2g_arbitrage_price_file            = None        # optional CSV: time_step, price_EUR/MWh
v2g_flexibility_price_file          = None        # optional CSV, same format
v2g_min_discharge_kWh               = 1.0         # floor on one V2G slot
block_arbitrage_extreme_slots       = 'on'        # keep trips out of the best/worst arbitrage windows
charging_curve_status               = 'on'        # derate charging power above 80 % SoC
charging_power_modulation           = 'on'        # 'off' = dumb-charger rule
charging_min_energy_kWh             = 20.0        # floor on one depot charging visit
charging_efficiency                 = 0.97        # meter -> battery
discharging_efficiency              = 0.93        # battery -> meter
monte_carlo_samples_per_trip        = 5           # candidate start times kept per trip; 0 = all
monte_carlo_seed                    = 20251107    # fixes that draw; changing it changes the model
advanced_degradation_status         = 'on'        # per-vehicle EFC, SoC weighting, aging spread
degradation_distribution_penalty    = 50.0        # EUR per kWh-equiv on the worst-aged vehicle
soc_weight_factor                   = 0.5         # extra aging weight at either SoC extreme
soc_weight_breakpoints              = 9           # tangents approximating that curve

# 1.5 other parameters -------------------------------------------------------------
initial_soc_fraction                = 0.5         # SoC at 00:00 and the target at 24:00
penalty_vehicle_use                 = 10          # EUR per used vehicle
penalty_charging_use                = 1           # EUR per occupied 30-min charger or V2G slot
penalty_charging_block              = 5           # EUR per visit to a depot charger; 0 = off
penalty_charger_use                 = 20.0        # EUR per charger at peak, asset sizing only; 0 = off
penalty_charging_external_time      = 10          # EUR per minute of public charging
slack_notification_status           = 'off'       # 'on' posts to Slack when a run finishes
slack_notification_channel          = 'python-updates'  # token comes from $SLACK_BOT_TOKEN, never from here
