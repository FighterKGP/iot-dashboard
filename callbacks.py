"""
callbacks.py
============
Responsibilities:
    - Pull the latest devices snapshot from mqtt_client every second and
      push it into devices-store.
    - Rebuild map markers whenever devices-store changes.
    - Track which device is selected (via marker click) in
      selected-device-store.
    - Drive the detail panel (temperature, humidity, status badge,
      last-seen, RF spectrum graph) from whichever device is selected.

register_callbacks(app) is called once from app.py.
"""

import datetime

import plotly.graph_objects as go
from dash import Input, Output, State, ALL, ctx, no_update

import india_map
from mqtt_client import mqtt_manager, RF_CHANNEL_COUNT


def register_callbacks(app):

    # ------------------------------------------------------------------
    # 1. Poll MQTT manager every second -> devices-store
    # ------------------------------------------------------------------
    @app.callback(
        Output("devices-store", "data"),
        Input("refresh-interval", "n_intervals"),
    )
    def refresh_devices(_n):
        return mqtt_manager.get_snapshot()

    # ------------------------------------------------------------------
    # 2. devices-store changed -> rebuild markers on the map
    # ------------------------------------------------------------------
    @app.callback(
        Output("device-marker-layer", "children"),
        Input("devices-store", "data"),
        State("selected-device-store", "data"),
    )
    def update_markers(devices, selected_device):
        devices = devices or {}
        return india_map.build_markers(devices, selected_device)

    # ------------------------------------------------------------------
    # 3. Marker clicked -> update selected-device-store
    #    Uses pattern-matching so it works for any number of devices
    #    without touching this code.
    # ------------------------------------------------------------------
    @app.callback(
        Output("selected-device-store", "data"),
        Input({"type": "device-marker", "index": ALL}, "n_clicks"),
        State("devices-store", "data"),
        prevent_initial_call=True,
    )
    def select_device(_n_clicks_list, devices):
        triggered = ctx.triggered_id
        if not triggered or not isinstance(triggered, dict):
            return no_update

        # Pattern-matching callbacks like this one also fire when a new
        # marker is first added to the map (not just on an actual click).
        # In that case n_clicks is still None/0 - only proceed if the
        # triggering value is a real, positive click count.
        triggered_value = ctx.triggered[0]["value"] if ctx.triggered else None
        if not triggered_value:
            return no_update

        device_name = triggered.get("index")
        if devices and device_name in devices:
            return device_name
        return no_update

    # ------------------------------------------------------------------
    # 4. Selected device or its data changed -> update detail panel
    # ------------------------------------------------------------------
    @app.callback(
        Output("detail-content", "className"),
        Output("no-selection-hint", "className"),
        Output("selected-device-name", "children"),
        Output("selected-device-badge", "children"),
        Output("selected-device-badge", "className"),
        Output("rf-component-badge", "children"),
        Output("rf-component-badge", "className"),
        Output("temp-component-badge", "children"),
        Output("temp-component-badge", "className"),
        Output("metric-temp", "children"),
        Output("metric-humidity", "children"),
        Output("metric-last-seen", "children"),
        Output("rf-spectrum-graph", "figure"),
        Input("selected-device-store", "data"),
        Input("devices-store", "data"),
    )
    def update_detail_panel(selected_device, devices):
        devices = devices or {}

        if not selected_device or selected_device not in devices:
            return (
                "detail-content hidden",
                "empty-hint",
                "--", "", "status-badge",
                "", "status-badge",
                "", "status-badge",
                "--", "--", "--",
                _empty_rf_figure("No device selected"),
            )

        reading = devices[selected_device]
        is_online = reading.get("online", False)

        badge_text = "ONLINE" if is_online else "OFFLINE"
        badge_class = "status-badge online" if is_online else "status-badge offline"

        # Component-level status - independent of overall connectivity.
        # Rstatus / Tstatus come straight from the ESP32's JSON payload:
        # 1 = that specific sensor is working, 0 = it isn't.
        rf_ok = bool(reading.get("Rstatus", 0))
        temp_ok = bool(reading.get("Tstatus", 0))

        rf_badge_text = "RF OK" if rf_ok else "RF DISCONNECTED"
        rf_badge_class = "status-badge online" if rf_ok else "status-badge offline"

        temp_badge_text = "SENSOR OK" if temp_ok else "SENSOR DISCONNECTED"
        temp_badge_class = "status-badge online" if temp_ok else "status-badge offline"

        temp = reading.get("Temp")
        humidity = reading.get("Humidity")
        last_seen_ts = reading.get("last_seen")

        # If the temp/humidity sensor itself has flagged as disconnected,
        # show that explicitly rather than a stale last-good number.
        if temp_ok and isinstance(temp, (int, float)):
            temp_text = f"{temp:.1f}\u00b0C"
        else:
            temp_text = "Disconnected"

        if temp_ok and isinstance(humidity, (int, float)):
            humidity_text = f"{humidity:.0f}%"
        else:
            humidity_text = "Disconnected"

        last_seen_text = _format_last_seen(last_seen_ts)

        # Same idea for the RF graph: only plot real data if the RF module
        # itself is reporting healthy.
        if rf_ok:
            figure = _build_rf_figure(reading.get("RF", []))
        else:
            figure = _empty_rf_figure("RF module disconnected")

        return (
            "detail-content",
            "empty-hint hidden",
            selected_device,
            badge_text,
            badge_class,
            rf_badge_text,
            rf_badge_class,
            temp_badge_text,
            temp_badge_class,
            temp_text,
            humidity_text,
            last_seen_text,
            figure,
        )


def _format_last_seen(timestamp):
    if not timestamp:
        return "--"
    delta = datetime.datetime.now().timestamp() - timestamp
    if delta < 2:
        return "just now"
    return f"{int(delta)}s ago"


def _build_rf_figure(rf_values):
    channels = list(range(len(rf_values))) if rf_values else list(range(RF_CHANNEL_COUNT))
    values = rf_values if rf_values else [0] * RF_CHANNEL_COUNT

    fig = go.Figure(
        data=[
            go.Bar(
                x=channels,
                y=values,
                marker=dict(color="#f5a623"),
            )
        ]
    )
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=20, t=10, b=40),
        xaxis_title="Channel",
        yaxis_title="Signal",
        font=dict(family="JetBrains Mono, monospace", size=11, color="#c9d1d9"),
        bargap=0.15,
    )
    return fig


def _empty_rf_figure(message="No data"):
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=20, t=10, b=40),
        annotations=[dict(
            text=message,
            xref="paper", yref="paper",
            showarrow=False,
            font=dict(size=13, color="#6e7681"),
        )],
    )
    return fig
