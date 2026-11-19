"""Spider Sense: cross-platform, browser-based road safety prototype.

Run with: uvicorn app:app --host 127.0.0.1 --port 8000
For trusted alert ingestion, set INGEST_TOKEN to a long random secret.
"""

import asyncio
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Spider Sense", description="Advisory speed and road hazard alerts")
ROOT = Path(__file__).parent
ALERT_RADIUS_KM = float(os.getenv("ALERT_RADIUS_KM", "5"))
ALERT_TTL_SECONDS = int(os.getenv("ALERT_TTL_SECONDS", "300"))
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")

# In-memory prototype state; use a database and a broker for production deployments.
drivers: dict[WebSocket, tuple[float, float]] = {}
drivers_lock = asyncio.Lock()
recent_alerts: list[dict] = []
report_times: dict[str, float] = {}


class Alert(BaseModel):
    kind: Literal["hazard", "speeding"]
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    message: str = Field(min_length=1, max_length=240)
    speed_kmh: Optional[float] = Field(default=None, ge=0, le=400)
    expires_at: datetime


class HazardReport(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    category: Literal["accident", "road_hazard", "obstruction"]


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two coordinates."""
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


async def publish_alert(alert: Alert) -> int:
    now = datetime.now(timezone.utc)
    expires = alert.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now or expires > now + timedelta(minutes=10):
        raise HTTPException(status_code=400, detail="Invalid alert expiry")

    payload = alert.model_dump(mode="json")
    payload["expires_at"] = expires.isoformat()
    payload["created_at"] = now.isoformat()
    recent_alerts.append(payload)
    recent_alerts[:] = [item for item in recent_alerts if datetime.fromisoformat(item["expires_at"]) > now]

    async with drivers_lock:
        recipients = [
            socket for socket, (lat, lon) in drivers.items()
            if distance_km(lat, lon, alert.latitude, alert.longitude) <= ALERT_RADIUS_KM
        ]
    dead = []
    for socket in recipients:
        try:
            await socket.send_json(payload)
        except Exception:
            dead.append(socket)
    if dead:
        async with drivers_lock:
            for socket in dead:
                drivers.pop(socket, None)
    return len(recipients) - len(dead)


@app.get("/", response_class=FileResponse)
async def home():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(ROOT / "web" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/service-worker.js")
async def service_worker():
    return FileResponse(ROOT / "web" / "service-worker.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/health")
async def health():
    return {"ok": True, "connected_drivers": len(drivers)}


@app.post("/api/alerts")
async def receive_alert(alert: Alert, x_ingest_token: str = Header(default="")):
    """Accept alerts from a trusted source; the ingest token must be configured."""
    if not INGEST_TOKEN or x_ingest_token != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or unconfigured ingest token")
    notified = await publish_alert(alert)
    return {"accepted": True, "recipients_notified": notified}


@app.post("/api/report")
async def report_hazard(report: HazardReport, request: Request):
    """Create a short-lived, explicitly unverified community hazard report."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    if now - report_times.get(client_ip, 0) < 60:
        raise HTTPException(status_code=429, detail="Please wait before submitting another report")
    report_times[client_ip] = now
    descriptions = {
        "accident": "Community report: possible accident nearby. Unverified—use caution.",
        "road_hazard": "Community report: road hazard nearby. Unverified—use caution.",
        "obstruction": "Community report: road obstruction nearby. Unverified—use caution.",
    }
    alert = Alert(
        kind="hazard",
        latitude=report.latitude,
        longitude=report.longitude,
        message=descriptions[report.category],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ALERT_TTL_SECONDS),
    )
    notified = await publish_alert(alert)
    return {"accepted": True, "recipients_notified": notified, "expires_in_seconds": ALERT_TTL_SECONDS}


@app.websocket("/ws/driver")
async def driver_socket(websocket: WebSocket):
    await websocket.accept()
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
                nearby = [
                    alert for alert in recent_alerts
                    if datetime.fromisoformat(alert["expires_at"]) > datetime.now(timezone.utc)
                    and distance_km(latitude, longitude, alert["latitude"], alert["longitude"]) <= ALERT_RADIUS_KM
                ]
            for alert in nearby:
                await websocket.send_json(alert)
    except WebSocketDisconnect:
        pass
    finally:
        async with drivers_lock:
            drivers.pop(websocket, None)
