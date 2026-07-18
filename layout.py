"""
layout.py
=========
Responsibilities:
    - Define the entire visual structure of the dashboard:
        * Header
        * Left: interactive Leaflet map of India with device markers
        * Right: selected-device info cards + live RF spectrum graph
    - Define the dcc.Store components that hold app state:
        * devices-store           -> latest snapshot of all devices
        * selected-device-store   -> name of the currently selected device
    - Define the dcc.Interval that drives the 1-second live refresh.

No callback logic lives here - only structure. callbacks.py wires
everything together.
"""

from dash import dcc, html
import dash_leaflet as dl

REFRESH_INTERVAL_MS = 1000


def build_layout():
    return html.Div(
        className="app-shell",
        children=[
            # ---- State ----------------------------------------------------
            dcc.Store(id="devices-store", data={}),
            dcc.Store(id="selected-device-store", data=None),
            dcc.Interval(id="refresh-interval", interval=REFRESH_INTERVAL_MS, n_intervals=0),

            # ---- Header -----------------------------------------------------
            html.Header(
                className="app-header",
                children=[
                    html.Div(className="header-brand", children=[
                        html.Span("IITKGP", className="brand-mark"),
                        html.Span("Environmental Monitoring", className="brand-title"),
                    ]),
                    html.Div(id="header-status", className="header-status"),
                ],
            ),

            # ---- Main body: map (left) + detail panel (right) -------------
            html.Main(
                className="dashboard-grid",
                children=[
                    html.Section(
                        className="map-panel",
                        children=[
                            dl.Map(
                                id="india-map",
                                center=(22.9734, 78.6569),
                                zoom=5,
                                style={"width": "100%", "height": "100%"},
                                children=[
                                    dl.TileLayer(
                                        url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
                                        attribution='&copy; OpenStreetMap contributors &copy; CARTO',
                                    ),
                                    dl.LayerGroup(id="device-marker-layer", children=[]),
                                ],
                            ),
                        ],
                    ),
                    html.Section(
                        className="detail-panel",
                        children=[
                            html.Div(id="no-selection-hint", className="empty-hint", children=[
                                html.P("Click a device marker on the map to view its live readings."),
                            ]),
                            html.Div(
                                id="detail-content",
                                className="detail-content hidden",
                                children=[
                                    html.Div(className="detail-title-row", children=[
                                        html.H2(id="selected-device-name", children="--"),
                                        html.Div(className="badge-group", children=[
                                            html.Span(id="selected-device-badge", className="status-badge"),
                                            html.Span(id="rf-component-badge", className="status-badge"),
                                            html.Span(id="temp-component-badge", className="status-badge"),
                                        ]),
                                    ]),
                                    html.Div(
                                        className="metric-row",
                                        children=[
                                            html.Div(className="metric-card", children=[
                                                html.Span("Temperature", className="metric-label"),
                                                html.Span(id="metric-temp", className="metric-value"),
                                            ]),
                                            html.Div(className="metric-card", children=[
                                                html.Span("Humidity", className="metric-label"),
                                                html.Span(id="metric-humidity", className="metric-value"),
                                            ]),
                                            html.Div(className="metric-card", children=[
                                                html.Span("Last update", className="metric-label"),
                                                html.Span(id="metric-last-seen", className="metric-value"),
                                            ]),
                                        ],
                                    ),
                                    html.Div(
                                        className="graph-card",
                                        children=[
                                            html.Div(className="graph-card-header", children=[
                                                html.Span("RF Spectrum (Channels 0-125)"),
                                            ]),
                                            dcc.Graph(
                                                id="rf-spectrum-graph",
                                                config={"displayModeBar": False},
                                                style={"height": "360px"},
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
