"""
india_map.py
============
Responsibilities:
    - Hold the (editable) mapping of device name -> physical location.
    - Assign a deterministic fallback location (scattered around a default
      campus point) to any device that isn't explicitly mapped yet, so new
      ESP32s show up on the map immediately without code changes.
    - Build the list of dash-leaflet Marker components from the current
      devices snapshot. This is the ONLY function callbacks.py should call
      to turn device data into map markers.
"""

import hashlib

import dash_leaflet as dl

# --------------------------------------------------------------------------
# Known device locations. Add real coordinates here as you deploy ESP32s.
# Anything not listed here gets an automatic fallback position (see below).
# --------------------------------------------------------------------------
DEVICE_LOCATIONS = {
    "Muin": (26.5123, 80.2329)   # IIT Kharagpur main campus, example
    # "Lab1": (22.3193, 87.2916),
    # "Lab2": (22.3105, 87.3080),
}

# Default center used both for the map's initial view and as the anchor
# for auto-scattered fallback markers - set to IIT Kharagpur.
DEFAULT_CENTER = (22.3149, 87.3105)
DEFAULT_ZOOM = 15

# India-wide fallback view, used when zero devices have known locations yet
INDIA_CENTER = (22.9734, 78.6569)
INDIA_ZOOM = 5

# How far (in degrees, roughly) auto-scattered markers spread from the
# default center. ~0.01 deg is on the order of ~1km.
_SCATTER_RADIUS_DEG = 0.01

STATUS_COLORS = {
    "online": "#3ddc84",   # signal green
    "offline": "#ff5c5c",  # alert red
}


def _fallback_location(device_name: str) -> tuple:
    """
    Deterministically derive a lat/lon near DEFAULT_CENTER from the device
    name, so the same unmapped device always lands in the same spot (and
    different devices don't stack on top of each other).
    """
    digest = hashlib.md5(device_name.encode("utf-8")).hexdigest()
    # Turn two chunks of the hash into signed offsets in [-1, 1)
    offset_x = (int(digest[0:8], 16) / 0xFFFFFFFF) * 2 - 1
    offset_y = (int(digest[8:16], 16) / 0xFFFFFFFF) * 2 - 1

    lat = DEFAULT_CENTER[0] + offset_y * _SCATTER_RADIUS_DEG
    lon = DEFAULT_CENTER[1] + offset_x * _SCATTER_RADIUS_DEG
    return (lat, lon)


def get_device_location(device_name: str) -> tuple:
    """Return (lat, lon) for a device, using an explicit mapping if present."""
    if device_name in DEVICE_LOCATIONS:
        return DEVICE_LOCATIONS[device_name]
    return _fallback_location(device_name)


def build_markers(devices: dict, selected_device: str = None) -> list:
    """
    Build a list of dl.Marker components from a devices snapshot
    (as returned by mqtt_client.mqtt_manager.get_snapshot()).

    Each marker's id is a pattern-matching dict id:
        {"type": "device-marker", "index": <device_name>}
    so callbacks.py can listen to clicks on ALL of them with a single
    callback, regardless of how many devices exist.
    """
    markers = []

    for name, reading in devices.items():
        lat, lon = get_device_location(name)
        is_online = reading.get("online", False)
        color = STATUS_COLORS["online"] if is_online else STATUS_COLORS["offline"]
        is_selected = (name == selected_device)

        marker = dl.CircleMarker(
            center=(lat, lon),
            id={"type": "device-marker", "index": name},
            radius=12 if is_selected else 9,
            color="#f5a623" if is_selected else color,
            fill=True,
            fillColor=color,
            fillOpacity=0.9,
            weight=3 if is_selected else 2,
            children=[
                dl.Tooltip(name),
                dl.Popup(_popup_content(name, reading)),
            ],
        )
        markers.append(marker)

    return markers


def _popup_content(name: str, reading: dict) -> str:
    status_text = "Online" if reading.get("online") else "Offline"
    return (
        f"{name} | Temp: {reading.get('Temp', '--')}\u00b0C | "
        f"Humidity: {reading.get('Humidity', '--')}% | {status_text}"
    )


def default_map_center(devices: dict):
    """Center/zoom the map on the known devices, or fall back to all-India."""
    if not devices:
        return INDIA_CENTER, INDIA_ZOOM
    return DEFAULT_CENTER, DEFAULT_ZOOM
