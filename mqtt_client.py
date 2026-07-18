"""
mqtt_client.py
==============
Responsibilities:
    - Connect to the MQTT broker and subscribe to all ESP32 topics.
    - Reconnect automatically on connection loss.
    - Decrypt (AES-256-CBC) and parse each incoming payload.
    - Maintain a thread-safe in-memory store of the latest reading per device.
    - Run entirely in a background thread so the Dash app's main thread
      (and its own event loop / callbacks) is never blocked.

This module exposes a single object, `mqtt_manager`, which the rest of the
app (layout.py, callbacks.py, india_map.py) imports and reads from. Nothing
outside this file should touch the MQTT client or the raw devices dict
directly - always go through `mqtt_manager.get_snapshot()`.
"""

import base64
import copy
import json
import os
import re
import ssl
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --------------------------------------------------------------------------
# Broker / topic configuration
# --------------------------------------------------------------------------
# Read from environment variables so credentials never get committed to a
# public GitHub repo. Set these in Render's dashboard under
# Environment (not in the code, not in git).
#
# HiveMQ Cloud requires TLS + username/password on every connection -
# there is no plaintext/unauthenticated option, unlike the public test
# broker.hivemq.com:1883 this project originally used.
BROKER = os.environ.get("MQTT_BROKER", "broker.hivemq.com")
PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")  # required for HiveMQ Cloud
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")  # required for HiveMQ Cloud
MQTT_USE_TLS = os.environ.get("MQTT_USE_TLS", "false").lower() == "true"

TOPIC = "IITKGP/esp32/#"
KEEPALIVE = 60

# --------------------------------------------------------------------------
# AES-256-CBC configuration (must match the ESP32 firmware exactly)
# --------------------------------------------------------------------------
AES_KEY = b"52147896321478596317846257895842"  # 32 bytes -> AES-256
AES_IV = b"7412365789321459"                    # 16 bytes -> CBC block size

# How many seconds without a new message before a device is considered stale
STALE_AFTER_SECONDS = 20

# How many additional seconds AFTER going stale/offline before the device
# is removed entirely (marker + data disappear from the dashboard, instead
# of sitting there showing "OFFLINE" forever).
OFFLINE_GRACE_SECONDS = 10
REMOVE_AFTER_SECONDS = STALE_AFTER_SECONDS + OFFLINE_GRACE_SECONDS

# Number of RF channels the ESP32 scans (0-125 -> 126 values)
RF_CHANNEL_COUNT = 126


def decrypt_to_text(ciphertext_b64: str) -> str:
    """Base64-decode -> AES-CBC decrypt -> unpad -> return the raw UTF-8 text."""
    ciphertext = base64.b64decode(ciphertext_b64)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return plaintext.decode("utf-8")


# Arduino's String(float) conversion prints a failed DHT11 read as the bare
# word 'nan' (or '-nan'/'inf') with no quotes - which is not valid JSON and
# would otherwise make json.loads() reject the ENTIRE packet (RF data and
# all) just because one field failed to read. Replace those tokens with
# `null` before parsing so the rest of the packet still comes through; a
# null Temp/Humidity is then treated as "component disconnected", exactly
# like the Tstatus/Rstatus flags already do.
_INVALID_NUMERIC_TOKEN_RE = re.compile(r":\s*-?(?:nan|inf(?:inity)?)\b", re.IGNORECASE)


def _sanitize_json_text(text: str) -> str:
    return _INVALID_NUMERIC_TOKEN_RE.sub(": null", text)


def decrypt_payload(ciphertext_b64: str) -> dict:
    """Base64-decode -> AES-CBC decrypt -> unpad -> JSON-parse a payload."""
    text = decrypt_to_text(ciphertext_b64)
    return json.loads(_sanitize_json_text(text))


def _safe_callback(func):
    """
    Decorator for paho callbacks. An uncaught exception inside any paho
    callback silently kills the ENTIRE background network thread - no
    crash is shown in the app, no automatic reconnect happens, and the
    dashboard just quietly stops receiving messages forever (looking
    exactly like a broken connection, when the connection was actually
    fine). Wrapping every callback like this means a bug in one callback
    gets logged instead of taking down the whole MQTT pipeline.
    """
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            print(f"[MQTT] Exception inside {func.__name__} (caught, thread survives): {exc!r}")
    return wrapper


class MQTTDeviceManager:
    """
    Owns the MQTT connection and the shared `devices` state.

    devices = {
        "Muin": {
            "Temp": 34.2,
            "Humidity": 71,
            "Rstatus": 1,
            "RF": [126 floats],
            "last_seen": 1731412345.12,   # epoch seconds, added by us
        },
        ...
    }
    """

    def __init__(self, broker=BROKER, port=PORT, topic=TOPIC):
        self.broker = broker
        self.port = port
        self.topic = topic

        self._devices = {}
        self._lock = threading.Lock()

        client_id = f"dashboard-{uuid.uuid4().hex[:8]}"
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe

        if MQTT_USERNAME:
            self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        if MQTT_USE_TLS:
            # HiveMQ Cloud (and most managed brokers) require TLS on their
            # secure port (typically 8883). This uses the system's default
            # trusted CA certificates to verify the broker's certificate.
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)

        # paho's built-in exponential backoff reconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

        self._started = False

    # -- MQTT callbacks ----------------------------------------------------

    @_safe_callback
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            print("[MQTT] Connected to broker.")
            client.subscribe(self.topic)
            print(f"[MQTT] Subscribed to: {self.topic}")
        else:
            print(f"[MQTT] Connection failed, reason code: {reason_code}")
            if reason_code in (4, 5) or "not authorised" in str(reason_code).lower():
                print("[MQTT] This usually means bad/missing username or password.")

    @_safe_callback
    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        print(f"[MQTT] Disconnected (reason code: {reason_code}). "
              f"paho will attempt to reconnect automatically.")

    @_safe_callback
    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        # reason_codes >= 128 mean the broker REFUSED the subscription
        # (e.g. this credential set has no permission on this topic) -
        # a successful subscribe() call does NOT guarantee the broker
        # actually granted it, so this is the real confirmation.
        # NOTE: paho 2.x wraps each code in a ReasonCode object which
        # supports direct comparison (>=) but not int() conversion.
        codes = [reason_codes] if not isinstance(reason_codes, list) else reason_codes
        try:
            denied = [c for c in codes if c >= 128]
        except TypeError:
            denied = [c for c in codes if getattr(c, "value", 0) >= 128]
        if denied:
            print(f"[MQTT] Subscription DENIED by broker, reason code(s): {denied}. "
                  f"Check this credential's topic permissions in HiveMQ Cloud.")
        else:
            print(f"[MQTT] Subscription granted by broker, QoS/code(s): {codes}")

    def _on_message(self, client, userdata, msg):
        raw_text = None
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8", errors="ignore")

            raw_text = decrypt_to_text(payload)
            data = json.loads(_sanitize_json_text(raw_text))

            # IMPORTANT: never reject the whole packet just because one
            # sensor's field is missing/null (e.g. the DHT11 got
            # unplugged). Doing that would drop the ENTIRE message -
            # including perfectly good RF data - and make the whole
            # device look disconnected over a single failed component.
            # Instead, accept whatever arrived and default what's absent;
            # Rstatus/Tstatus (or a missing Temp/Humidity value) are what
            # signal a specific component's failure, not the packet itself.
            data.setdefault("Temp", None)
            data.setdefault("Humidity", None)
            data.setdefault("Rstatus", 0)
            data.setdefault("Tstatus", 0)
            data.setdefault("RF", [0] * RF_CHANNEL_COUNT)
            data["last_seen"] = time.time()

            device_name = topic.split("/")[-1]

            with self._lock:
                self._devices[device_name] = data

            print(f"[MQTT] Received + stored update for device '{device_name}' "
                  f"(topic '{topic}')")

        except Exception as exc:
            print(f"[MQTT] Error handling message on '{msg.topic}': {exc}")
            if raw_text is not None:
                print(f"[MQTT] Raw decrypted text was: {raw_text!r}")

    # -- Public API ----------------------------------------------------------

    def start(self):
        """Connect and start the network loop in a background thread."""
        if self._started:
            return
        print(f"[MQTT] Connecting as client ID: {self._client._client_id.decode()}")
        self._client.connect_timeout = 10  # fail fast instead of hanging
        try:
            self._client.connect(self.broker, self.port, KEEPALIVE)
        except Exception as exc:
            print(f"[MQTT] connect() raised: {exc!r}")
            # Still start the loop - paho's built-in reconnect logic will
            # keep retrying in the background even after a failed first
            # attempt, and each attempt's outcome is logged via
            # _on_connect / _on_disconnect.
        self._client.loop_start()
        self._started = True
        print("[MQTT] Background loop started.")

    def stop(self):
        if self._started:
            self._client.loop_stop()
            self._client.disconnect()
            self._started = False

    def get_snapshot(self) -> dict:
        """
        Return a deep copy of the current devices dict, annotated with a
        derived 'online' flag based on STALE_AFTER_SECONDS. Devices that
        have been silent longer than REMOVE_AFTER_SECONDS are dropped
        entirely (both from the returned snapshot and from internal
        storage) so they disappear from the map instead of sitting there
        marked OFFLINE forever. Safe to call from any thread (e.g. a Dash
        callback).
        """
        now = time.time()

        with self._lock:
            expired = [
                name for name, reading in self._devices.items()
                if (now - reading.get("last_seen", 0)) > REMOVE_AFTER_SECONDS
            ]
            for name in expired:
                del self._devices[name]

            snapshot = copy.deepcopy(self._devices)

        for name, reading in snapshot.items():
            last_seen = reading.get("last_seen", 0)
            reading["online"] = (now - last_seen) <= STALE_AFTER_SECONDS

        return snapshot


# Single shared instance used across the whole app
mqtt_manager = MQTTDeviceManager()


# Allow `python mqtt_client.py` standalone for debugging/testing the
# MQTT pipeline without spinning up the dashboard.
if __name__ == "__main__":
    mqtt_manager.start()
    try:
        while True:
            time.sleep(2)
            snap = mqtt_manager.get_snapshot()
            print(f"[DEBUG] {len(snap)} device(s) known: {list(snap.keys())}")
    except KeyboardInterrupt:
        mqtt_manager.stop()
