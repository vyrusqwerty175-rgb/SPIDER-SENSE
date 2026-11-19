"""
Road-safety alert prototype: single-file app.

Install:
    pip install fastapi "uvicorn[standard]" ultralytics opencv-python requests

Run the web app only:
    export INGEST_TOKEN="replace-with-a-long-random-secret"
    uvicorn app:app --host 127.0.0.1 --port 8000

Run the web app and camera detector:
    export INGEST_TOKEN="replace-with-a-long-random-secret"
    export ENABLE_DETECTOR=1
    export CAMERA_LATITUDE="37.7749"
    export CAMERA_LONGITUDE="-122.4194"
    export GATE_DISTANCE_METERS="20"
    export SPEED_LIMIT_KMH="50"
    uvicorn app:app --host 127.0.0.1 --port 8000

Set VIDEO_SOURCE to a video file or camera number, for example:
    export VIDEO_SOURCE="0"

Prototype only. Camera estimates and alerts may be inaccurate or delayed.
"""

import asyncio
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import cv2
import requests
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Road Safety Alert Prototype")

# ----------------------------
# Configuration
# ----------------------------

INGEST_TOKEN = os.getenv("INGEST_TOKEN", "change-me")
ALERT_RADIUS_KM = float(os.getenv("ALERT_RADIUS_KM", "2"))
ALERT_TTL_SECONDS = int(os.getenv("ALERT_TTL_SECONDS", "120"))

ENABLE_DETECTOR = os.getenv("ENABLE_DETECTOR", "0").lower() in {"1", "true", "yes"}
VIDEO_SOURCE = os.getenv("VIDEO_SOURCE", "0")
MODEL_PATH = os.getenv("MODEL_PATH", "yolo11n.pt")

GATE_1_Y = int(os.getenv("GATE_1_Y", "220"))
GATE_2_Y = int(os.getenv("GATE_2_Y", "360"))
GATE_DISTANCE_METERS = float(os.getenv("GATE_DISTANCE_METERS", "20"))

SPEED_LIMIT_KMH = float(os.getenv("SPEED_LIMIT_KMH", "50"))
MIN_VALID_SPEED_KMH = 5.0
MAX_VALID_SPEED_KMH = 180.0
ALERT_COOLDOWN_SECONDS = 30

CAMERA_LATITUDE = os.getenv("CAMERA_LATITUDE")
CAMERA_LONGITUDE = os.getenv("CAMERA_LONGITUDE")

# Connected driver browsers and their latest locations.
drivers: dict[WebSocket, tuple[float, float]] = {}
drivers_lock = asyncio.Lock()


# ----------------------------
# Alert data and utilities
# ----------------------------

class Alert(BaseModel):
    kind: Literal["speeding", "hazard"]
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    message: str = Field(min_length=1, max_length=240)
    speed_kmh: Optional[float] = Field(default=None, ge=0, le=400)
    expires_at: datetime


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the approximate great-circle distance between two points."""
    earth_radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


# ----------------------------
# Driver web page
# ----------------------------

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Road Safety Alerts</title>
  <style>
    body {
      font: 16px system-ui, sans-serif;
      max-width: 720px;
      margin: 2rem auto;
      padding: 0 1rem;
    }
    .status {
      padding: .8rem;
      background: #eee;
      border-radius: 8px;
    }
    .alert {
      padding: 1rem;
      margin: .8rem 0;
      border-left: 6px solid #d97706;
      background: #fff7ed;
    }
    .hazard {
      border-color: #b91c1c;
      background: #fef2f2;
    }
    button {
      padding: .7rem 1rem;
      font-size: 1rem;
    }
  </style>
</head>
<body>
  <h1>Road Safety Alerts</h1>
  <p>
    Prototype only. Alerts may be inaccurate or delayed.
    Do not interact with this page while driving.
  </p>

  <button id="start">Share location and connect</button>
  <p id="status" class="status">Not connected.</p>
  <section id="alerts" aria-live="polite"></section>

  <script>
    const statusBox = document.getElementById("status");
    const alertBox = document.getElementById("alerts");
    let socket;
    let watchId;

    function showStatus(message) {
      statusBox.textContent = message;
    }

    function connect() {
      if (!navigator.geolocation) {
        showStatus("This browser does not support geolocation.");
        return;
      }

      watchId = navigator.geolocation.watchPosition(
        (position) => {
          const { latitude, longitude } = position.coords;

          if (!socket || socket.readyState === WebSocket.CLOSED) {
            const scheme = location.protocol === "https:" ? "wss:" : "ws:";
            socket = new WebSocket(`${scheme}//${location.host}/ws/driver`);

            socket.onopen = () => {
              showStatus("Connected. Location is being shared with this prototype.");
              socket.send(JSON.stringify({ latitude, longitude }));
            };

            socket.onmessage = (event) => {
              const alert = JSON.parse(event.data);
              const card = document.createElement("article");
              card.className =
                "alert " + (alert.kind === "hazard" ? "hazard" : "");

              const title = document.createElement("strong");
              title.textContent =
                alert.kind === "hazard"
                  ? "Hazard reported nearby"
                  : "Speeding vehicle detected nearby";

              const message = document.createElement("p");
              message.textContent = alert.message;

              card.append(title, message);
              alertBox.prepend(card);
            };

            socket.onclose = () => showStatus("Connection closed.");
            socket.onerror = () => showStatus("Connection error.");
          } else if (socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ latitude, longitude }));
          }
        },
        (error) => showStatus("Location unavailable: " + error.message),
        {
          enableHighAccuracy: true,
          maximumAge: 5000,
          timeout: 10000
        }
      );
    }

    document.getElementById("start").addEventListener("click", connect);

    window.addEventListener("beforeunload", () => {
      if (watchId !== undefined) {
        navigator.geolocation.clearWatch(watchId);
      }
      if (socket) {
        socket.close();
      }
    });
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(PAGE)


@app.post("/api/alerts")
async def receive_alert(
    alert: Alert,
    x_ingest_token: str = Header(default=""),
):
    """Receive a camera or trusted-source alert and notify nearby connected users."""
    if x_ingest_token != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid ingest token")

    now = datetime.now(timezone.utc)
    expires_at = alert.expires_at

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at <= now or expires_at > now + timedelta(minutes=10):
        raise HTTPException(status_code=400, detail="Invalid alert expiry")

    payload = alert.model_dump(mode="json")
    payload["expires_at"] = expires_at.isoformat()
    payload["created_at"] = now.isoformat()

    async with drivers_lock:
        recipients = [
            websocket
            for websocket, (latitude, longitude) in drivers.items()
            if distance_km(
                latitude,
                longitude,
                alert.latitude,
                alert.longitude,
            ) <= ALERT_RADIUS_KM
        ]

    dead_connections = []

    for websocket in recipients:
        try:
            await websocket.send_json(payload)
        except Exception:
            dead_connections.append(websocket)

    if dead_connections:
        async with drivers_lock:
            for websocket in dead_connections:
                drivers.pop(websocket, None)

    return {
        "accepted": True,
        "recipients_notified": len(recipients) - len(dead_connections),
    }


@app.websocket("/ws/driver")
async def driver_socket(websocket: WebSocket):
    """Receive location updates from a driver's browser."""
    await websocket.accept()

    async with drivers_lock:
        # The location is replaced as soon as the browser sends its first update.
        drivers[websocket] = (0.0, 0.0)

    try:
        while True:
            position = await websocket.receive_json()

            try:
                latitude = float(position["latitude"])
                longitude = float(position["longitude"])
            except (KeyError, TypeError, ValueError):
                continue

            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue

            async with drivers_lock:
                drivers[websocket] = (latitude, longitude)

    except WebSocketDisconnect:
        pass
    finally:
        async with drivers_lock:
            drivers.pop(websocket, None)


# ----------------------------
# Camera vehicle-speed detector
# ----------------------------

def post_speed_alert(speed_kmh: float, track_id: int):
    """Send a speed alert to this app's HTTP endpoint."""
    if CAMERA_LATITUDE is None or CAMERA_LONGITUDE is None:
        print("Set CAMERA_LATITUDE and CAMERA_LONGITUDE to send alerts.")
        return

    try:
        latitude = float(CAMERA_LATITUDE)
        longitude = float(CAMERA_LONGITUDE)
    except ValueError:
        print("Camera coordinates must be valid numbers.")
        return

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=ALERT_TTL_SECONDS
    )

    payload = {
        "kind": "speeding",
        "latitude": latitude,
        "longitude": longitude,
        "speed_kmh": round(speed_kmh, 1),
        "message": (
            f"Unverified camera estimate: vehicle #{track_id} "
            f"was traveling at {speed_kmh:.1f} km/h."
        ),
        "expires_at": expires_at.isoformat(),
    }

    try:
        response = requests.post(
            "http://127.0.0.1:8000/api/alerts",
            json=payload,
            headers={"X-Ingest-Token": INGEST_TOKEN},
            timeout=3,
        )
        response.raise_for_status()
        print("Alert accepted:", response.json())
    except requests.RequestException as error:
        print("Could not send alert:", error)


def open_video_source(source: str):
    """Open a camera number or a video file path."""
    if source.isdigit():
        return cv2.VideoCapture(int(source))
    return cv2.VideoCapture(source)


def detector_loop():
    """
    Run object detection and estimate speed using two image crossing lines.

    This simple timing-based estimate assumes the camera is fixed and traffic
    crosses both lines in the expected direction. It is not a calibrated
    enforcement-grade speed measurement.
    """
    if GATE_2_Y <= GATE_1_Y:
        print("Detector error: GATE_2_Y must be below GATE_1_Y.")
        return

    if GATE_DISTANCE_METERS <= 0:
        print("Detector error: GATE_DISTANCE_METERS must be positive.")
        return

    try:
        from ultralytics import YOLO
    except ImportError:
        print("Install the detector dependency: pip install ultralytics")
        return

    try:
        model = YOLO(MODEL_PATH)
    except Exception as error:
        print(f"Could not load detection model {MODEL_PATH!r}: {error}")
        return

    capture = open_video_source(VIDEO_SOURCE)

    if not capture.isOpened():
        print(f"Could not open video source: {VIDEO_SOURCE}")
        return

    # COCO class IDs: car=2, motorcycle=3, bus=5, truck=7.
    vehicle_class_ids = {2, 3, 5, 7}
    gate1_crossing_time = {}
    previous_y = {}
    last_alert_time = {}

    print("Camera detector started. Press q in the video window to stop it.")

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                print("Video source ended or a frame could not be read.")
                break

            current_time = __import__("time").monotonic()

            try:
                result = model.track(
                    frame,
                    persist=True,
                    classes=sorted(vehicle_class_ids),
                    conf=0.35,
                    verbose=False,
                )[0]
            except Exception as error:
                print("Detection error:", error)
                continue

            cv2.line(
                frame, (0, GATE_1_Y), (frame.shape[1], GATE_1_Y),
                (0, 255, 255), 2
            )
            cv2.line(
                frame, (0, GATE_2_Y), (frame.shape[1], GATE_2_Y),
                (0, 0, 255), 2
            )

            boxes = result.boxes

            if boxes is not None and boxes.id is not None:
                coordinates = boxes.xyxy.cpu().tolist()
                track_ids = boxes.id.int().cpu().tolist()
                class_ids = boxes.cls.int().cpu().tolist()

                for box, track_id, class_id in zip(
                    coordinates, track_ids, class_ids
                ):
                    if class_id not in vehicle_class_ids:
                        continue

                    x1, y1, x2, y2 = map(int, box)
                    foot_y = y2
                    old_y = previous_y.get(track_id)
                    previous_y[track_id] = foot_y

                    label = f"ID {track_id}"

                    # This example supports vehicles moving down through
                    # the frame: Gate 1 first, then Gate 2.
                    if old_y is not None and old_y < GATE_1_Y <= foot_y:
                        gate1_crossing_time[track_id] = current_time

                    if old_y is not None and old_y < GATE_2_Y <= foot_y:
                        start_time = gate1_crossing_time.pop(track_id, None)

                        if start_time is not None:
                            elapsed_seconds = current_time - start_time

                            if elapsed_seconds > 0:
                                speed_kmh = (
                                    GATE_DISTANCE_METERS / elapsed_seconds
                                ) * 3.6

                                if (
                                    MIN_VALID_SPEED_KMH
                                    <= speed_kmh
                                    <= MAX_VALID_SPEED_KMH
                                ):
                                    label = f"ID {track_id}: {speed_kmh:.1f} km/h"

                                    if speed_kmh > SPEED_LIMIT_KMH:
                                        label += " OVER LIMIT"
                                        last_alert = last_alert_time.get(
                                            track_id, 0
                                        )

                                        if (
                                            current_time - last_alert
                                            >= ALERT_COOLDOWN_SECONDS
                                        ):
                                            post_speed_alert(
                                                speed_kmh,
                                                track_id,
                                            )
                                            last_alert_time[track_id] = (
                                                current_time
                                            )
                                else:
                                    label = f"ID {track_id}: invalid estimate"
                            else:
                                label = f"ID {track_id}: invalid timing"

                    cv2.rectangle(
                        frame, (x1, y1), (x2, y2), (50, 200, 50), 2
                    )
                    cv2.circle(
                        frame, ((x1 + x2) // 2, foot_y), 4, (255, 0, 0), -1
                    )
                    cv2.putText(
                        frame,
                        label,
                        (x1, max(25, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 0, 255),
                        2,
                    )

            cv2.imshow("Speed prototype - press q to quit", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    finally:
        capture.release()
        cv2.destroyAllWindows()
        print("Camera detector stopped.")


@app.on_event("startup")
async def start_detector():
    """Start the optional camera detector in a background thread."""
    if ENABLE_DETECTOR:
        thread = threading.Thread(target=detector_loop, daemon=True)
        thread.start()


@app.get("/health")
async def health():
    return {"ok": True, "detector_enabled": ENABLE_DETECTOR}