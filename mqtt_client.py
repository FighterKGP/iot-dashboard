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
import threading
import time

import paho.mqtt.client as mqtt
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --------------------------------------------------------------------------
# Broker / topic configuration
# --------------------------------------------------------------------------
# Private HiveMQ Cloud cluster (TLS required on port 8883).
# Set these three as environment variables - on Render: Environment tab.
# Locally: set them in your shell, or hardcode temporarily for a quick test.
BROKER = os.environ.get("MQTT_BROKER", "REPLACE_WITH_YOUR_CLUSTER_URL.hivemq.cloud")
PORT = int(os.environ.get("MQTT_PORT", 8883))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
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


def decrypt_payload(ciphertext_b64: str) -> dict:
    """Base64-decode -> AES-CBC decrypt -> unpad -> JSON-parse a payload."""
    ciphertext = base64.b64decode(ciphertext_b64)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return json.loads(plaintext.decode("utf-8"))


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

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Private broker requires TLS + credentials (the old public
        # broker.hivemq.com needed neither, since it had no auth at all).
        self._client.tls_set()  # uses system CA certs to verify the broker
        if MQTT_USERNAME:
            self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        # paho's built-in exponential backoff reconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

        self._started = False

    # -- MQTT callbacks ----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            print("[MQTT] Connected to broker.")
            client.subscribe(self.topic)
            print(f"[MQTT] Subscribed to: {self.topic}")
        else:
            print(f"[MQTT] Connection failed, reason code: {reason_code}")

    def _on_disconnect(self, client, userdata, reason_code, properties=None, *args):
        print(f"[MQTT] Disconnected (reason code: {reason_code}). "
              f"paho will attempt to reconnect automatically.")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8", errors="ignore")

            data = decrypt_payload(payload)

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

        except Exception as exc:
            print(f"[MQTT] Error handling message on '{msg.topic}': {exc}")

    # -- Public API ----------------------------------------------------------

    def start(self):
        """Connect and start the network loop in a background thread."""
        if self._started:
            return
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
