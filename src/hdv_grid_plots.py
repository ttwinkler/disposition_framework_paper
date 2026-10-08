"""The grid view: one row per unit, one cell per 30-minute step, coloured by value.

A second way of drawing the same day. The Gantt figures the model already writes answer
"what is this truck doing" - they are about *activity*, and a bar is the right shape for
that. They cannot answer "how much", because a bar has no magnitude: a truck charging at
50 kW and one at 600 kW draw the same green.

These do the opposite. Every cell is one vehicle-half-hour with its number printed in it
and its colour taken from the same number, so a whole day of a whole fleet is one image
and the eye finds the extremes without reading any of it. What they give up is the
categorical detail - which is exactly what the Gantt keeps - so the two are written side
by side rather than one replacing the other.

Conventions shared by all three, because a set of figures that agree is worth more than
three that are each locally optimal:

  - rows are units (a vehicle, a driver), columns are the 48 half hours of the day
  - the x axis is labelled in hours, 0 to 24, however many steps that is
  - a cell with nothing to say is blank with a dash, never a zero - "not charging" and
    "charging 0 kW" are different statements and only one of them is usually true
  - what the colour means is explained along the bottom, as a bar for a quantity and as
    a legend for a set of states
  - one colour scheme per figure, so a glance at the colours says which figure it is

Two kinds of grid, because there are two kinds of cell value. A *quantity* - a state of
charge - is a heatmap: the colour is the number and a continuous bar reads it back. A
*state* - parking, charging, on trip 7 - is not on a scale at all, and colouring it as if
it were invents an order between parking and charging that does not exist. Those get a
fixed colour per state and a legend. In both the cell text carries the detail the colour
cannot: the exact value, or which V2G channel a discharge settled in.

A heatmap can also carry two quantities at once, by giving the colour to one and the
printed number to the other (grid_heatmap's text_values). That is worth doing only where
the pair is read together and the eye wants them in the same place - the state of charge
against the power that moves it, where the question is always "how full, and how hard is
it being pushed to get there". The cost is a colour bar for one of them and nothing but
the digits for the other, so the quantity whose *shape over the day* matters takes the
colour and the one read cell by cell takes the text.
"""

# 1 SETUP
import math

import numpy as np


# 1.1 house style. Kept here rather than in each caller so the three figures cannot drift
FIGURE_DPI = 150
BLANK_TEXT = '-'
BLANK_COLOUR = '#FFFFFF'
GRID_COLOUR = '#BFBFBF'


def soc_colour_map():
    """A traffic light: red at empty, yellow at half, green at full.

    A state of charge is a quantity, but it is not a neutral one: the question asked of
    this figure is whether a battery got low, and "low" is the answer that needs to be
    visible from across the room. A single-hue ramp makes an empty battery merely pale,
    which is the same thing an unremarkable one looks like. A traffic light says which end
    is the bad one to anyone who has ever crossed a road, without a key and without
    reading the bar.

    Three stops, not five. Interpolating red to yellow to green passes through amber and
    yellow-green on its own, which is exactly the sequence wanted, and naming only the
    three keeps the figure honest about what it is.

    Every stop is light enough for black text (relative luminance 0.59 to 0.83), which is
    what stops the printed power numbers flipping between black and white down a row.
    That is why these are the softer traffic-light tones rather than signal red and signal
    green: at full saturation the red end drops below the ink threshold and the grid stops
    reading as one surface. The yellow midpoint also keeps the middle off grey, which in
    this figure is already spoken for - an uncoloured cell is the blank fill and its dash
    is grey, so a grey half-full battery would read as a missing reading.

    The cost is worth stating plainly: red against green is the one pair that deuteranopia
    and protanopia cannot separate, and nothing else in this figure encodes the level - the
    digits are the power. On a red-green-blind reading the ramp still runs light to dark
    and low to high, which is what carries it, but the *hue* is not doing any work. Where
    that matters, 'GnBu' is the lightness-only ramp this replaced.
    """
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list('soc_traffic_light', [
        '#E8776B',      # 0 %   - stop
        '#F5DA63',      # 50 %  - caution
        '#72C472',      # 100 % - go
    ])


def _annotation_size(rows, columns, width, height, characters=3):
    """Largest font that still fits `characters` inside one cell, both ways.

    Worked out from the cell in points rather than from a constant over the row and column
    counts, because the text only has to fit the cell and how many characters it is doing
    that with matters as much as how many cells there are. A two-character state code has
    room for half again the size a three-digit number does, and guessing one constant for
    both left everything at the floor and unreadable.
    """
    cell_width_pt = 72.0 * max(width - 1.8, 1.0) / max(columns, 1)      # less the labels
    cell_height_pt = 72.0 * max(height - 1.6, 1.0) / max(rows, 1)       # less title, axis
    by_width = 0.85 * cell_width_pt / (0.62 * max(characters, 1))       # 0.62 em a glyph
    by_height = 0.70 * cell_height_pt
    return max(3.0, min(11.0, by_width, by_height))


def _text_colour(value, vmin, vmax, cmap):
    """Black on a light cell, white on a dark one - decided from the cell's own colour."""
    if vmax - vmin <= 0:
        return 'black'
    red, green, blue, _ = cmap((float(value) - vmin) / (vmax - vmin))
    # perceived luminance; the threshold is where mid-tones stop reading as light
    return 'white' if (0.299 * red + 0.587 * green + 0.114 * blue) < 0.55 else 'black'


def _ink(colour, plt):
    """Black or white, whichever is legible on a cell of this colour."""
    from matplotlib.colors import to_rgb
    red, green, blue = to_rgb(colour)
    return 'white' if (0.299 * red + 0.587 * green + 0.114 * blue) < 0.55 else 'black'


def _frame(plt, rows, columns, row_labels, step_hours, title, row_axis_label):
    """The parts both grids share: figure size, hour ticks, row labels, cell hairlines."""
    width = max(11.0, min(26.0, 0.34 * columns))
    height = max(3.5, min(26.0, 0.34 * rows + 2.2))
    figure, axis = plt.subplots(figsize=(width, height))

    # clock labels every two hours, the same grid every other time axis in the project
    # uses. A bare hour number reads as an elapsed count - "8" could be the ninth hour of
    # the figure or eight hours into a shift - where 08:00 can only be a time of day.
    from hdv_figure_style import HOUR_TICK_STRIDE, TIME_AXIS_LABEL, hhmm

    per_hour = max(1, int(round(1.0 / step_hours)))
    hours = columns / per_hour
    tick_hours = range(0, int(hours) + 1, HOUR_TICK_STRIDE)
    axis.set_xticks([h * per_hour - 0.5 for h in tick_hours])
    axis.set_xticklabels([hhmm(h) for h in tick_hours], fontsize=8)
    axis.set_xlabel(TIME_AXIS_LABEL, fontsize=11)

    axis.set_yticks(range(rows))
    axis.set_yticklabels(row_labels, fontsize=max(6.0, min(10.0, 150.0 / max(rows, 1))))
    if row_axis_label:
        axis.set_ylabel(row_axis_label, fontsize=11)

    # a hairline between cells, on the minor grid so it sits between them and not through
    # their centres
    axis.set_xticks(np.arange(-0.5, columns, 1), minor=True)
    axis.set_yticks(np.arange(-0.5, rows, 1), minor=True)
    axis.grid(which='minor', color=GRID_COLOUR, linewidth=0.3)
    axis.tick_params(which='minor', length=0)
    for spine in axis.spines.values():
        spine.set_edgecolor('#4D4D4D')
    if title:
        axis.set_title(title, fontsize=12, pad=10)
    return figure, axis, height


# 2 THE FIGURE
# 2.0 a figure is closed once it is written, unless somebody is going to look at it
#
# keep_open is how a run that SHOWS its figures (disposition model 1.4b1) holds on to
# them: matplotlib can only show a figure it still has, and every one of these is closed
# the moment it is saved. Closing stays the default, because the caller that does not ask
# is a sweep or a design run drawing hundreds of them and wanting none of them in memory.
def _release(plt, figure, keep_open):
    """Close a saved figure - or leave it for whoever asked to see it."""
    if not keep_open:
        plt.close(figure)


def grid_heatmap(values, row_labels, output_path, *, value_label, colour_map,
                 step_hours=0.5, value_format='{:.0f}', vmin=None, vmax=None,
                 symmetric=False, title=None, row_axis_label=None,
                 blank_colour=BLANK_COLOUR, text_values=None, text_format=None,
                 plt=None, keep_open=False):
    """Draw one grid figure and write it to output_path.

    values      2D array-like, one row per unit and one column per time step. Use NaN for
                "nothing to report", which is drawn blank with a dash rather than as a
                zero.
    symmetric   centre the colour scale on zero and make it run equally far either way.
                For a signed quantity - power in and out, say - anything else puts the
                neutral point at an arbitrary colour and makes the sign hard to read.
    blank_colour
                what an empty cell is filled with. White by default, which reads as
                "nothing" on a one-sided scale. On a diverging one it does not: white is
                the *middle* there, so an idle cell and a cell at zero would be the same
                colour and only the dash would tell them apart. Pass a grey instead.
    text_values a second quantity of the same shape, printed in the cells in place of
                `values`. The colour still comes from `values` and the bar still reads
                that one back, so this is how one figure carries two numbers per cell.
                Its NaNs are read as "no number here" and drawn as a dash on a cell that
                keeps its colour - which is a statement rather than an absence: a battery
                at 62 % with a dash is at 62 % and not plugged in.
    text_format how to format those, defaulting to value_format. Separate because the two
                quantities need not be alike - a percentage and a signed kW figure do not
                agree on decimals or on width.
    """
    if plt is None:                       # the caller normally passes its own pyplot
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from hdv_figure_style import use_figure_style
        use_figure_style(plt)

    grid = np.array(values, dtype=float)
    if grid.ndim != 2:
        raise ValueError(f"grid_heatmap needs a 2D array, got shape {grid.shape}")
    rows, columns = grid.shape
    if rows == 0 or columns == 0:
        return None
    if len(row_labels) != rows:
        raise ValueError(f"{len(row_labels)} row labels for {rows} rows")

    finite = grid[np.isfinite(grid)]
    if finite.size == 0:
        return None                       # nothing happened; a blank grid says nothing

    # the printed quantity, where it is not the coloured one. Same shape is required
    # rather than broadcast: these are two readings of the same cell, and a silent
    # stretch of one over the other would put a number in a cell it does not belong to.
    printed = grid
    if text_values is not None:
        printed = np.array(text_values, dtype=float)
        if printed.shape != grid.shape:
            raise ValueError(f"text_values has shape {printed.shape}, but the values it "
                             f"annotates have {grid.shape}")
    text_format = text_format or value_format

    if symmetric:
        reach = max(abs(float(finite.min())), abs(float(finite.max()))) or 1.0
        low, high = -reach, reach
    else:
        low = float(finite.min()) if vmin is None else float(vmin)
        high = float(finite.max()) if vmax is None else float(vmax)
    if high <= low:
        high = low + 1.0

    # a name or a ready-made colormap: the house ramps (soc_colour_map) are built here
    # rather than registered globally, so the caller hands the object over directly
    cmap = (colour_map if hasattr(colour_map, 'set_bad')
            else plt.get_cmap(colour_map)).copy()
    cmap.set_bad(blank_colour)
    masked = np.ma.masked_invalid(grid)

    figure, axis, height = _frame(plt, rows, columns, row_labels, step_hours, title,
                                  row_axis_label)
    image = axis.imshow(masked, aspect='auto', cmap=cmap, vmin=low, vmax=high,
                        interpolation='nearest')

    # sized on what is actually printed, which is not always what is coloured: a signed
    # kW figure is a character wider than the percentage behind it and would be shrunk to
    # fit a cell measured for the wrong string
    widest = max((len(text_format.format(v)) for v in printed[np.isfinite(printed)]),
                 default=1)
    annotation = _annotation_size(rows, columns, figure.get_figwidth(), height, widest)
    blank_ink = _ink(blank_colour, plt)    # one fill, so one answer, not one per cell
    for r in range(rows):
        for c in range(columns):
            cell = grid[r, c]
            # ink from the cell's own colour, which is `values` even when the digits on
            # top of it are not - a dark cell needs light text whatever the text says.
            # An uncoloured cell is the blank fill, so the ink follows that instead.
            ink = (_text_colour(cell, low, high, cmap) if np.isfinite(cell)
                   else blank_ink)
            number = printed[r, c]
            if not np.isfinite(number):
                # grey only on a cell that is blank anyway; on a coloured one it would be
                # the least legible shade available and the dash is carrying meaning here
                axis.text(c, r, BLANK_TEXT, ha='center', va='center', fontsize=annotation,
                          color=ink if np.isfinite(cell) else '#9A9A9A')
            else:
                axis.text(c, r, text_format.format(number), ha='center', va='center',
                          fontsize=annotation, color=ink)

    # colorbar padding is a fraction of the axes height, so a fixed value collides with
    # the x-axis label on a short figure (four drivers) and floats away on a tall one.
    # Worked back from the gap actually wanted, about three quarters of an inch.
    pad = max(0.14, min(0.55, 0.75 / max(1.0, height - 1.8)))
    bar = figure.colorbar(image, ax=axis, orientation='horizontal',
                          fraction=0.05, pad=pad, aspect=44)
    bar.set_label(value_label, fontsize=11)
    bar.ax.tick_params(labelsize=8)

    figure.tight_layout()
    figure.savefig(output_path, dpi=FIGURE_DPI, bbox_inches='tight')
    _release(plt, figure, keep_open)
    return output_path


# 3 THE STATE GRID
def grid_categories(states, labels, row_labels, output_path, *, palette,
                    step_hours=0.5, title=None, row_axis_label=None,
                    legend_columns=None, plt=None, keep_open=False):
    """The same grid for a *state* rather than a quantity: fixed colours and a legend.

    A state is not on a scale. Parking is not less than charging, and a continuous colour
    map would put them in an order and invite the eye to read a magnitude out of a
    difference that has none. So each state keeps the colour it already has in the Gantt
    figure - the two views of the same day agree - and the bottom of the figure explains
    them instead of measuring them.

    states   2D array-like of keys into palette, one per unit and time step. None, or any
             key the palette does not know, is an empty cell: blank, with a dash.
    labels   2D array-like of the text for each cell. This is where the *detail* goes,
             two characters at most: the trip number, or which V2G channel a discharge
             settled in, which the colour deliberately does not distinguish.
    palette  {key: (colour, legend text)}, in the order the legend should read.
    """
    if plt is None:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from hdv_figure_style import use_figure_style
        use_figure_style(plt)
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    rows = len(states)
    if rows == 0:
        return None
    columns = len(states[0])
    if columns == 0:
        return None
    if len(row_labels) != rows:
        raise ValueError(f"{len(row_labels)} row labels for {rows} rows")

    keys = list(palette)
    index_of = {key: i for i, key in enumerate(keys)}
    grid = np.full((rows, columns), np.nan)
    for r in range(rows):
        for c in range(columns):
            grid[r, c] = index_of.get(states[r][c], np.nan)
    if not np.isfinite(grid).any():
        return None                       # nothing to show; an empty grid says nothing

    cmap = ListedColormap([palette[key][0] for key in keys])
    cmap.set_bad(BLANK_COLOUR)

    figure, axis, height = _frame(plt, rows, columns, row_labels, step_hours, title,
                                  row_axis_label)
    axis.imshow(np.ma.masked_invalid(grid), aspect='auto', cmap=cmap,
                vmin=-0.5, vmax=len(keys) - 0.5, interpolation='nearest')

    widest = max((len(str(t)) for row in (labels or []) for t in row if t), default=1)
    annotation = _annotation_size(rows, columns, figure.get_figwidth(), height, widest)
    for r in range(rows):
        for c in range(columns):
            cell = grid[r, c]
            if not np.isfinite(cell):
                axis.text(c, r, BLANK_TEXT, ha='center', va='center',
                          fontsize=annotation, color='#9A9A9A')
                continue
            text = '' if labels is None else (labels[r][c] or '')
            if text:
                axis.text(c, r, str(text), ha='center', va='center', fontsize=annotation,
                          color=_ink(palette[keys[int(cell)]][0], plt))

    # the legend sits where the colour bar sits on the heatmap grids, for the same reason:
    # it is the same piece of information and moving it would break the family resemblance
    handles = [Patch(facecolor=palette[key][0], edgecolor='#4D4D4D', linewidth=0.4,
                     label=palette[key][1]) for key in keys]
    if legend_columns is None:
        legend_columns = min(5, max(1, len(handles)))
    figure.tight_layout()
    # in axes-height units, like the colour bar's pad, so a short figure is not crowded
    drop = -max(0.14, min(0.55, 0.62 / max(1.0, height - 1.8)))
    axis.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, drop),
                ncol=legend_columns, fontsize=9, frameon=False)

    figure.savefig(output_path, dpi=FIGURE_DPI, bbox_inches='tight')
    _release(plt, figure, keep_open)
    return output_path
