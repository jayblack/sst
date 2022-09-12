#!/usr/bin/env python

from bokeh.models.widgets.markups import Div
import msgpack
import numpy as np

from bokeh.events import DoubleTap, SelectionGeometry
from bokeh.io import curdoc
from bokeh.layouts import column, layout
from bokeh.palettes import Spectral11
from pathlib import Path

from extremes import topouts, combined_topouts
from extremes import intervals_mask, filter_airtimes, filter_idlings
from extremes import add_airtime_labels, add_idling_marks
from fft import fft_figure, update_fft
from leverage import shock_wheel_figure, leverage_ratio_figure
from psst import Telemetry, dataclass_from_dict
from travel import travel_figure, travel_histogram_figure, update_travel_histogram
from velocity import velocity_histogram_figure, velocity_band_stats_figure
from velocity import update_velocity_band_stats, update_velocity_histogram


args = curdoc().session_context.request.arguments
p = Path(args.get('psst')[0].decode('utf-8')).name
psst_file = Path('data').joinpath(p)
if not psst_file.exists():
    curdoc().add_root(Div(text=f"File not found in data directory: {p}"))
    raise Exception("No such file")
d = msgpack.unpackb(open(psst_file, 'rb').read())

# XXX for compatibility with older PSST files...
if 'Present' not in d['Front'].keys():
    d['Front']['Present'] = True
    d['Rear']['Present'] = True
telemetry = dataclass_from_dict(Telemetry, d)

# lod - Level of Detail for travel graph (downsample ratio)
try:
    lod = int(args.get('lod')[0])
except:
    lod = 5

# hst - High Speed Threshold for velocity graphs/statistics in mm/s
try:
    hst = int(args.get('hst')[0])
except:
    hst = 100

tick = 1.0 / telemetry.SampleRate # time step length in seconds

front_travel, rear_travel = [], []
front_velocity, rear_velocity = [], []
front_topouts, rear_topouts = [], []
front_color, rear_color = Spectral11[1], Spectral11[2]
front_record_num, rear_record_num, record_num = 0, 0, 0

'''
Topouts are intervals where suspension is at zero extension for an extended period of time. It allows us to filter
out e.g. the beginning and the end of the ride, where the bike is at rest, or intervals where we stop mid-ride.
Filtering these out is important, because they can skew travel and velocity statistics. They are handled
individually for front and rear suspension.
'''
if telemetry.Front.Present:
    front_travel = np.array(telemetry.Front.Travel)
    front_record_num = len(front_travel)
    front_velocity = np.array(telemetry.Front.Velocity)
    front_topouts = topouts(front_travel, telemetry.Front.Calibration.MaxStroke, telemetry.SampleRate)
    front_topouts_mask = intervals_mask(front_topouts, front_record_num)

    if np.count_nonzero(front_topouts_mask):
        p_front_travel_hist = travel_histogram_figure(telemetry.Front.DigitizedTravel, front_travel, front_topouts_mask,
            front_color, "Travel histogram (front)")
        p_front_vel_hist = velocity_histogram_figure(telemetry.Front.DigitizedTravel, telemetry.Front.DigitizedVelocity,
            front_velocity, front_topouts_mask, hst, "Speed histogram (front)")
        p_front_vel_stats = velocity_band_stats_figure(front_velocity[front_topouts_mask], hst)
        p_front_fft = fft_figure(front_travel[front_topouts_mask], tick, front_color, "Frequencies (front)")
    else:
        telemetry.Front.Present = False

if telemetry.Rear.Present:
    rear_travel = np.array(telemetry.Rear.Travel)
    rear_record_num = len(rear_travel)
    rear_velocity = np.array(telemetry.Rear.Velocity)
    rear_topouts = topouts(rear_travel, telemetry.Frame.MaxRearTravel, telemetry.SampleRate)
    rear_topouts_mask = intervals_mask(rear_topouts, rear_record_num)

    if np.count_nonzero(rear_topouts_mask):
        p_rear_travel_hist = travel_histogram_figure(telemetry.Rear.DigitizedTravel, rear_travel, rear_topouts_mask,
            rear_color, "Travel histogram (rear)")
        p_rear_vel_hist = velocity_histogram_figure(telemetry.Rear.DigitizedTravel, telemetry.Rear.DigitizedVelocity,
            rear_velocity, rear_topouts_mask, hst, "Speed histogram (rear)")
        p_rear_vel_stats = velocity_band_stats_figure(rear_velocity[rear_topouts_mask], hst)
        p_rear_fft = fft_figure(rear_travel[rear_topouts_mask], tick, rear_color, "Frequencies (rear)")
    else:
        telemetry.Rear.Present = False

if not (front_record_num == 0 or rear_record_num == 0) and front_record_num != rear_record_num:
    curdoc().add_root(Div(text=f"SST file is corrupt"))
    raise Exception("Corrupt dataset")

record_num = front_record_num if front_record_num else rear_record_num

'''
Event handlers for travel graph. We update histograms, statistics and FFTs when a selection is made with the Box Select
tool, and when the selection is cancelled with a double tap.
'''
def on_selectiongeometry(event):
    start = int(event.geometry['x0'] * telemetry.SampleRate)
    end = int(event.geometry['x1'] * telemetry.SampleRate)
    mask = np.full(record_num, False)
    mask[start:end] = True

    if telemetry.Front.Present:
        f_mask = front_topouts_mask & mask
        if np.count_nonzero(f_mask):
            update_travel_histogram(p_front_travel_hist, front_travel, telemetry.Front.DigitizedTravel, f_mask)
            update_fft(p_front_fft, front_travel[f_mask], tick)
            update_velocity_histogram(p_front_vel_hist, telemetry.Front.DigitizedTravel, telemetry.Front.DigitizedVelocity,
                front_velocity, f_mask)
            update_velocity_band_stats(p_front_vel_stats, front_velocity[f_mask], hst)

    if telemetry.Rear.Present:
        r_mask = rear_topouts_mask & mask
        if np.count_nonzero(r_mask):
            update_travel_histogram(p_rear_travel_hist, rear_travel, telemetry.Rear.DigitizedTravel, r_mask)
            update_fft(p_rear_fft, rear_travel[r_mask], tick)
            update_velocity_histogram(p_rear_vel_hist, telemetry.Rear.DigitizedTravel, telemetry.Rear.DigitizedVelocity,
                rear_velocity, r_mask)
            update_velocity_band_stats(p_rear_vel_stats, rear_velocity[r_mask], hst)

def on_doubletap():
    if telemetry.Front.Present:
        update_travel_histogram(p_front_travel_hist, front_travel, telemetry.Front.DigitizedTravel, front_topouts_mask)
        update_fft(p_front_fft, front_travel[front_topouts_mask], tick)
        update_velocity_histogram(p_front_vel_hist, telemetry.Front.DigitizedTravel, telemetry.Front.DigitizedVelocity,
            front_velocity, front_topouts_mask)
        update_velocity_band_stats(p_front_vel_stats, front_velocity[front_topouts_mask], hst)

    if telemetry.Rear.Present:
        update_travel_histogram(p_rear_travel_hist, rear_travel, telemetry.Rear.DigitizedTravel, rear_topouts_mask)
        update_fft(p_rear_fft, rear_travel[rear_topouts_mask], tick)
        update_velocity_histogram(p_rear_vel_hist, telemetry.Rear.DigitizedTravel, telemetry.Rear.DigitizedVelocity,
            rear_velocity, rear_topouts_mask)
        update_velocity_band_stats(p_rear_vel_stats, rear_velocity[rear_topouts_mask], hst)

p_travel = travel_figure(telemetry, lod, front_color, rear_color)
p_travel.on_event(SelectionGeometry, on_selectiongeometry)
p_travel.on_event(DoubleTap, on_doubletap)

'''
We use both suspensions to find airtimes. Basically, everything is considered airtime if both suspensions are close
to zero travel, and suspension velocity at the end of the interval reaches a threshold. A few remarks:
    - Originally, I used a velocity threshold at the beginning too of a candidate interval, but there were a lot of
    false negatives usually with drops.
    - We use the mean of front and rear travel to determine closeness to zero. This is based on the empirical
    observation that sometimes one of the suspensions (usually my fork) oscillates outside the set threshold during
    airtime (usually during drops). I expect this to become a problem if anybody else starts using this program, but
    could not come up with better heuristics so far.
'''
comb_topouts = combined_topouts(front_travel if telemetry.Front.Present else np.full(record_num, 0),
    telemetry.Front.Calibration.MaxStroke,
    rear_travel if telemetry.Rear.Present else np.full(record_num, 0),
    telemetry.Frame.MaxRearTravel,
    telemetry.SampleRate)
airtimes = filter_airtimes(comb_topouts,
    front_velocity if telemetry.Front.Present else np.full(record_num, 0),
    rear_velocity if telemetry.Rear.Present else np.full(record_num, 0),
    telemetry.SampleRate)
airtimes_mask = intervals_mask(np.array(airtimes), record_num, False)
add_airtime_labels(airtimes, tick, p_travel)

'''
Mask out intervals on the travel graph that are ignored in statistics.
'''
if telemetry.Front.Present:
    front_idlings = filter_idlings(front_topouts, airtimes_mask)
    add_idling_marks(front_idlings, tick, p_travel)

if telemetry.Rear.Present:
    rear_idlings = filter_idlings(rear_topouts, airtimes_mask)
    add_idling_marks(rear_idlings, tick, p_travel)

'''
Leverage-related graphs. These are input data, not measured by this project.
'''
p_lr = leverage_ratio_figure(np.array(telemetry.Frame.WheelLeverageRatio), Spectral11[5])
p_sw = shock_wheel_figure(telemetry.Frame.CoeffsShockWheel, telemetry.Rear.Calibration.MaxStroke, Spectral11[5])

'''
Construct the layout.
'''
travel_hists = column()
row2 = [travel_hists]
row3 = []
if telemetry.Front.Present:
    travel_hists.children.append(p_front_travel_hist)
    row2.append(p_front_vel_hist)
    row2.append(p_front_vel_stats)
    row3.append(p_front_fft)
if telemetry.Rear.Present:
    travel_hists.children.append(p_rear_travel_hist)
    row2.append(p_rear_vel_hist)
    row2.append(p_rear_vel_stats)
    row3.append(p_rear_fft)
if len(row2[0].children) == 1:
    row2[0].children[0].height = 500

# add graphs to layout
l = layout(
    children=[
        [p_travel, p_lr, p_sw],
        row2,
        row3,
    ],
    sizing_mode='stretch_width')

curdoc().theme = 'dark_minimal'
curdoc().title = f"Sufni Suspension Telemetry Dashboard ({p})"
curdoc().add_root(l)