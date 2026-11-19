# Spider Sense

Spider Sense is a responsive, installable web app for **Android, iOS, and desktop browsers**. It tracks the speed reported by your device's GPS while the app is open and can deliver nearby, short-lived community hazard alerts.

## Run locally

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Geolocation works on localhost for development. On a phone or deployed site, browsers require HTTPS for location and service-worker features. Install the site from the browser menu to use it like an app. Location tracking and alerts require the app to remain open; background tracking is not guaranteed by mobile browsers.

## Features

- Live GPS speed display, configurable speed-limit indicator, and km/h or mph units.
- Nearby accident, road hazard, and obstruction reports with temporary alerts.
- Optional browser notifications and an installable PWA shell.
- Location is requested only after the user taps **Start tracking**. Stop tracking at any time.

## Trusted alert ingestion

Optional trusted systems can submit alerts to `POST /api/alerts`. Configure a secret first:

```bash
export INGEST_TOKEN="use-a-long-random-secret"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Send `X-Ingest-Token` and a JSON body containing `kind`, `latitude`, `longitude`, `message`, and an `expires_at` timestamp. The default alert radius is 5 km and can be changed with `ALERT_RADIUS_KM`. Expiry must be within 10 minutes.

## Important safety and privacy notes

This is a prototype, not an emergency service or a certified speedometer. GPS speed may be inaccurate, unavailable, or delayed. Community reports are unverified and can be wrong or stale. Always obey traffic laws, keep your attention on the road, and do not interact with the app while driving. Pull over safely before using it. In an emergency, contact local emergency services.

Location is sent to this server while tracking so nearby alerts can be matched. The prototype keeps live connections, reports, and rate-limit data in memory and does not persist location history; production deployment needs an explicit privacy policy, stronger abuse controls, durable alert storage, and a secure HTTPS deployment. Do not expose the development server directly to the public internet.
