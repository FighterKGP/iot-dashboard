"""
app.py
======
Responsibilities:
    - Initialize the Dash app.
    - Wire in the layout (layout.py) and callbacks (callbacks.py).
    - Start the MQTT background thread (mqtt_client.py).
    - Run the dev server locally, or expose `server` for a WSGI host
      (e.g. `gunicorn app:server` on Render).

No Flask app is created manually - Dash's own app.server (a Flask
instance Dash manages internally) is exposed for deployment purposes only.
"""

import os

import dash

from layout import build_layout
from callbacks import register_callbacks
from mqtt_client import mqtt_manager

app = dash.Dash(
    __name__,
    title="IITKGP Environmental Monitoring",
    update_title=None,
)

app.layout = build_layout()
register_callbacks(app)

# Exposed for production WSGI servers, e.g.:
#   gunicorn app:server --bind 0.0.0.0:$PORT
server = app.server

# Start the MQTT background thread once, at import time, so it runs both
# under `python app.py` locally and under gunicorn in production.
mqtt_manager.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    app.run(host="0.0.0.0", port=port, debug=False)
