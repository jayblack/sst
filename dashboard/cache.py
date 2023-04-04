#!/usr/bin/env python3

import argparse
import logging
import msgpack
import numpy as np
import struct
import sys
import zmq

from bokeh.document import Document
from bokeh.events import DocumentReady, MouseMove
from bokeh.embed import components
from bokeh.layouts import row
from bokeh.models.callbacks import CustomJS
from bokeh.models.widgets.markups import Div
from bokeh.palettes import Spectral11
from bokeh.plotting import figure
from bokeh.themes import built_in_themes, DARK_MINIMAL
from sqlalchemy import create_engine
from sqlalchemy.engine.base import Connection, Engine

from balance import balance_figure
from database import stmt_session, stmt_setup, stmt_cache_insert
from description import description_figure
from fft import fft_figure
from leverage import leverage_ratio_figure, shock_wheel_figure
from map import map_figure
from psst import Telemetry, dataclass_from_dict
from travel import travel_figure, travel_histogram_figure
from velocity import velocity_figure
from velocity import velocity_histogram_figure, velocity_band_stats_figure


def _setup_figure(conn: Connection, session_id: int) -> figure:
    res = conn.execute(stmt_setup(session_id))
    setup = res.fetchone()
    if not setup:
        raise Exception("Missing setup data")

    calibration_table = '''
    <table style="width: 100%;">
    <tbody>
    <tr>
    <th>Max. stroke</th>
    <td>{} mm</td>
    </tr>
    <tr>
    <th>Max. distance</th>
    <td>{} mm</td>
    </tr>
    <tr>
    <th>Arm length</th>
    <td>{} mm</td>
    </tr>
    </tbody>
    </table>
    '''

    return Div(
        name='setup',
        sizing_mode='stretch_width',
        height=300,
        stylesheets=['''
            div {
              width: 100%;
              height: 100%;
              padding: 15px;
              background: #15191c;
              font-size: 14px;
              color: #d0d0d0;
            }
            hr {
              border-top:1px dashed #d0d0d0;
              background-color: transparent;
              border-style: none none dashed;
            }
            table, th, td {
              border: 1px dashed #d0d0d0;
              border-collapse: collapse;
              text-align: center;
            }'''],
        text=f'''
            <b>Setup:</b>{setup[0]}<br />
            <b>Linkage:</b> {setup[1]}<hr />
            <b>Front calibration:</b>{setup[3]}<br />
            {calibration_table.format(setup[6], setup[5], setup[4])}
            <br /><b>Rear calibration:</b>{setup[9]}<br />
            {calibration_table.format(setup[12], setup[11], setup[10])}
            ''')


def create_cache(engine: Engine, session_id: int, lod: int, hst: int):
    front_color, rear_color = Spectral11[1], Spectral11[2]

    conn = engine.connect()

    res = conn.execute(stmt_session(session_id))
    session_data = res.fetchone()
    if not session_data:
        raise Exception("No such session")
    session_name = session_data[0]
    description = session_data[1]
    d = msgpack.unpackb(session_data[2])
    telemetry = dataclass_from_dict(Telemetry, d)

    tick = 1.0 / telemetry.SampleRate  # time step length in seconds

    p_setup = _setup_figure(conn, session_id)

    if telemetry.Front.Present:
        p_front_travel_hist = travel_histogram_figure(
            telemetry.Front.Strokes,
            telemetry.Front.TravelBins,
            front_color,
            "Travel histogram (front)")
        p_front_vel_hist = velocity_histogram_figure(
            telemetry.Front.Strokes,
            telemetry.Front.Velocity,
            telemetry.Front.TravelBins,
            telemetry.Front.VelocityBins,
            hst,
            "Speed histogram (front)")
        p_front_vel_stats = velocity_band_stats_figure(
            telemetry.Front.Strokes,
            telemetry.Front.Velocity,
            hst)
        p_front_fft = fft_figure(
            telemetry.Front.Strokes,
            telemetry.Front.Travel,
            tick,
            front_color,
            "Frequencies (front)")

    if telemetry.Rear.Present:
        p_rear_travel_hist = travel_histogram_figure(
            telemetry.Rear.Strokes,
            telemetry.Rear.TravelBins,
            rear_color,
            "Travel histogram (rear)")
        p_rear_vel_hist = velocity_histogram_figure(
            telemetry.Rear.Strokes,
            telemetry.Rear.Velocity,
            telemetry.Rear.TravelBins,
            telemetry.Rear.VelocityBins,
            hst,
            "Speed histogram (rear)")
        p_rear_vel_stats = velocity_band_stats_figure(
            telemetry.Rear.Strokes,
            telemetry.Rear.Velocity,
            hst)
        p_rear_fft = fft_figure(
            telemetry.Rear.Strokes,
            telemetry.Rear.Travel,
            tick,
            rear_color,
            "Frequencies (rear)")

    p_travel = travel_figure(telemetry, lod, front_color, rear_color)
    p_velocity = velocity_figure(telemetry, lod, front_color, rear_color)
    p_travel.x_range.js_link('start', p_velocity.x_range, 'start')
    p_travel.x_range.js_link('end', p_velocity.x_range, 'end')
    p_velocity.x_range.js_link('start', p_travel.x_range, 'start')
    p_velocity.x_range.js_link('end', p_travel.x_range, 'end')

    '''
    Leverage-related graphs. These are input data, not something measured.
    '''
    p_lr = leverage_ratio_figure(
        np.array(telemetry.Linkage.LeverageRatio), Spectral11[5])
    p_sw = shock_wheel_figure(telemetry.Linkage.ShockWheelCoeffs,
                              telemetry.Rear.Calibration.MaxStroke,
                              Spectral11[5])

    '''
    Compression and rebound velocity balance
    '''
    if telemetry.Front.Present and telemetry.Rear.Present:
        p_balance_compression = balance_figure(
            telemetry.Front.Strokes.Compressions,
            telemetry.Rear.Strokes.Compressions,
            telemetry.Linkage.MaxFrontTravel,
            telemetry.Linkage.MaxRearTravel,
            False,
            front_color,
            rear_color,
            'balance_compression',
            "Compression velocity balance")
        p_balance_rebound = balance_figure(
            telemetry.Front.Strokes.Rebounds,
            telemetry.Rear.Strokes.Rebounds,
            telemetry.Linkage.MaxFrontTravel,
            telemetry.Linkage.MaxRearTravel,
            True,
            front_color,
            rear_color,
            'balance_rebound',
            "Rebound velocity balance")

    p_desc = description_figure(session_id, session_name, description)
    p_map, on_mousemove = map_figure()
    p_travel.js_on_event(MouseMove, on_mousemove)

    '''
    Construct the layout.
    '''
    suspension_count = 0
    if telemetry.Front.Present:
        suspension_count += 1
    if telemetry.Rear.Present:
        suspension_count += 1

    dark_minimal_theme = built_in_themes[DARK_MINIMAL]
    document = Document()

    document.add_root(p_travel)
    document.add_root(p_velocity)
    document.add_root(p_map)
    document.add_root(p_lr)
    document.add_root(p_sw)
    document.add_root(p_setup)
    document.add_root(p_desc)
    columns = ['session_id', 'script', 'travel', 'velocity', 'map', 'lr', 'sw',
               'setup', 'desc']

    if telemetry.Front.Present:
        prefix = 'front_' if suspension_count == 2 else ''
        p_front_travel_hist.name = f'{prefix}travel_hist'
        p_front_fft.name = f'{prefix}fft'
        p_front_velocity = row(
            name=f'{prefix}velocity_hist',
            sizing_mode='stretch_width',
            children=[
                p_front_vel_hist,
                p_front_vel_stats])
        document.add_root(p_front_travel_hist)
        document.add_root(p_front_fft)
        document.add_root(p_front_velocity)
        columns.extend(['f_thist', 'f_fft', 'f_vhist'])
    if telemetry.Rear.Present:
        prefix = 'rear_' if suspension_count == 2 else ''
        p_rear_travel_hist.name = f'{prefix}travel_hist'
        p_rear_fft.name = f'{prefix}fft'
        p_rear_velocity = row(
            name=f'{prefix}velocity_hist',
            sizing_mode='stretch_width',
            children=[
                p_rear_vel_hist,
                p_rear_vel_stats])
        document.add_root(p_rear_travel_hist)
        document.add_root(p_rear_fft)
        document.add_root(p_rear_velocity)
        columns.extend(['r_thist', 'r_fft', 'r_vhist'])
    if suspension_count == 2:
        document.add_root(p_balance_compression)
        document.add_root(p_balance_rebound)
        columns.extend(['cbalance', 'rbalance'])

    # Some Bokeh models (like the description box or the map) need to be
    # dynamically initialized based on values in a particular Flask session.
    document.js_on_event(DocumentReady, CustomJS(
        args=dict(), code='init_models();'))

    script, divs = components(document.roots, theme=dark_minimal_theme)

    components_data = dict(zip(columns, [session_id, script] + list(divs)))
    conn.execute(stmt_cache_insert(), [components_data])
    conn.commit()
    conn.close()


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        nargs='?',
        choices=['serve', 'process'],
        default='process',
        help="Listen for session ids on ZMQ, or process one session")
    parser.add_argument(
        "-d", "--database",
        required=True,
        help="SQLite database path")
    parser.add_argument(
        "-a", "--address",
        default='0.0.0.0',
        help="ZMQ host")
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=5555,
        help="ZMQ port")
    parser.add_argument(
        "-s", "--session",
        type=int,
        help="Session ID")
    parser.add_argument(
        "-l", "--lod",
        type=int,
        default=5,
        help="Level of detail for graphs")
    parser.add_argument(
        "-t", "--hst",
        type=int,
        default=350,
        help="High speed threshold")
    cmd_args = parser.parse_args()
    engine = create_engine(f'sqlite:///{cmd_args.database}')

    if cmd_args.action == 'serve':
        context = zmq.Context()
        socket = context.socket(zmq.PULL)
        socket.bind(f'tcp://{cmd_args.address}:{cmd_args.port}')

        while True:
            try:
                message = socket.recv()
            except KeyboardInterrupt:
                sys.exit(0)
            session_id = struct.unpack('<i', message)[0]
            logging.info(f"generating cache for session {session_id}")
            create_cache(engine, session_id, cmd_args.lod, cmd_args.hst)
            logging.info(f"finished generating cache for session {session_id}")
    else:
        if not cmd_args.session:
            print(parser.format_help())
        else:
            create_cache(engine, cmd_args.session, cmd_args.lod, cmd_args.hst)