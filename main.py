# 5 MAIN
"""Entry point for the HDV disposition model.

The model is driven exclusively by the four Excel dataset inputs in inputs/:

    inputs/costs_dataset.xlsx -> data/cost_parameter_yearly.csv (sheet 'energy_yearly')
                                 data/cost_parameter_hourly.csv  (sheet 'energy_daily')
    inputs/depot_dataset.xlsx   -> data/depot_load_profile.csv      (sheet 'consumption')
                                 data/depot_pv_parameters.csv     (sheet 'generation')
                                 data/depot_charging_stations.csv (sheet 'charging')
    inputs/order_dataset.xlsx -> data/order_trips.csv   (geocoding + truck routing)
    inputs/depot_dataset.xlsx
    + data/order_trips.csv -> data/cache_pv_profile.json (PVGIS year for every trip day)
    inputs/fleet_dataset.xlsx   -> read directly by the disposition model

Nothing is ever written back into inputs/.

The web interface is the standard operating environment and starts by default:

    python main.py                 # start the web interface (http://localhost:8501)
    python main.py --port 8600     # ... on a different port
    python main.py --prepare       # command line only: regenerate the derived inputs
    python main.py --optimize      # command line only: prepare, then run the MIQCP solve
    python main.py --no-interface  # the same run, named for the case it exists for:
                                   # a remote server or any machine with no browser
    python main.py --force-routing # with --prepare/--optimize: re-route, ignoring order_trips.csv

What --optimize runs is whatever parameter.py asks for. One entry in each of the
variation lists (scenario, scenario_year, v2g_status, order_data_days) is a single day
planned once - a DISPOSITION run. It writes its figures into results/ and, being a
terminal run, opens them when it is done. More than one combination is a SWEEP: its
solves share one set of output filenames, so none of them is written and the summary
CSV is the output. The two are also priced from different sheets of
costs_dataset.xlsx. None of this is a switch - see run_kind and energy_price_basis,
sections 1.4b1 and 1.4b2 of the model.

Preparing the derived inputs and running a single scenario are both available inside
the web interface, so the flags above are only needed for headless work. The run itself
is configured in parameter.py.
"""

import sys
import argparse
import subprocess
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / 'src'
sys.path.insert(0, str(SRC_DIR))


def start_web_interface(port=8501):
    """Start the Streamlit interface and block until the user closes it."""
    from hdv_web_interface import launch
    return launch(port=port)


def prepare_inputs(force_routing=False, make_plots=True):
    """Regenerate every derived input from the Excel datasets in inputs/.

    The interface has to do exactly this when its rebuild button is pressed, and it used to
    do it with its own copy of the sequence - two lists of the same four generators, each
    free to gain a step the other never got, and only one of them defaulting to drawing the
    diagnostic plots. There is one now, in the interface module, and this is its
    command-line face: the order the generators run in, and what counts as "prepared", is
    decided in one place.
    """
    from hdv_web_interface import prepare_inputs as prepare
    info = prepare(force_routing=force_routing, make_plots=make_plots)
    print(f'derived inputs ready: {info["trips"]} trips over {info["days"]} days, '
          f'years {info["years"][0]}-{info["years"][1]}.')
    return info


def run_optimization():
    """Run the MIQCP solve in a fresh interpreter (it manages its own multiprocessing).

    One combination in parameter.py's variation lists or many decides whether that is a
    disposition run or a sweep, and with it whether figures are written - see the module
    docstring above and section 6.0 of the model.
    """
    script = SRC_DIR / 'hdv_disposition_optimization.py'
    print(f'\nstarting optimization: {script}')
    return subprocess.run([sys.executable, str(script)], check=False).returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--prepare', action='store_true',
                        help='regenerate the derived inputs on the command line instead '
                             'of starting the web interface')
    # Two spellings of one flag rather than two flags: --optimize says what the run does,
    # --no-interface says what it avoids, and on a machine with no browser the second is
    # what anyone reading --help will be looking for. argparse takes the dest from the
    # first long option, so both land on arguments.optimize and there is no second branch
    # to keep in step with this one.
    parser.add_argument('--optimize', '--no-interface', action='store_true',
                        help='prepare the derived inputs, then run the optimization on '
                             'the command line, without starting the web interface - for '
                             'a remote server or any machine with no browser. One '
                             'combination in parameter.py is a disposition run and writes '
                             'its figures; several are a sweep and write the summary CSV')
    parser.add_argument('--force-routing', action='store_true',
                        help='re-route the order dataset even if data/order_trips.csv is up to date')
    parser.add_argument('--no-plots', action='store_true',
                        help='skip the diagnostic plots of the preparation steps')
    parser.add_argument('--port', type=int, default=8501,
                        help='port of the web interface (default: 8501)')
    arguments = parser.parse_args()

    # the web interface is the default environment; the flags below opt out of it
    if not (arguments.prepare or arguments.optimize):
        sys.exit(start_web_interface(port=arguments.port))

    prepare_inputs(force_routing=arguments.force_routing, make_plots=not arguments.no_plots)

    if arguments.optimize:
        sys.exit(run_optimization())
    else:
        print('\nRun "python main.py" to start the web interface, or '
              '"python main.py --optimize" for the disposition sweep.')
