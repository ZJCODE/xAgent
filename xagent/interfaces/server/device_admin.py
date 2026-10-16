"""Device pairing uses the existing trusted administration boundary."""

from __future__ import annotations

from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, HTTPException, Response, Request
from starlette.responses import JSONResponse

from ...core.runtime.client import RuntimeClient, RuntimeUnavailable
from ...core.runtime.ownership import runtime_is_active
from ...integrations.devices.credentials import DeviceCredentials
from ...integrations.devices.config import DeviceId
from ...integrations.devices.profiles import DeviceProfiles, ProfilePatch


def register_device_admin_routes(app: FastAPI, resolve_admin) -> None:
    @app.middleware("http")
    async def device_admin_boundary(request: Request, call_next):
        if not request.url.path.startswith("/api/devices"):
            return await call_next(request)
        origin = request.headers.get("origin")
        try:
            source = urlsplit(origin) if origin else None
            trusted_origin = source is None or (source.scheme in {"http", "https"} and source.hostname == request.url.hostname)
        except ValueError:
            trusted_origin = False
        if not trusted_origin:
            return JSONResponse({"detail": "Device administration requires the trusted management origin"},
                                status_code=403, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def status(server):
        service = server.device_service
        paired = service.credentials if service is not None else DeviceCredentials(server.config_dir / ".devices.json")
        online = service.devices if service is not None else {}
        voice_sessions = service.voice_sessions if service is not None else {}
        profiles = service.profiles if service is not None else DeviceProfiles(server.config_dir / ".device_profiles.json")
        api = (server.config.get("channels") or {}).get("api") or {}
        host = api.get("host", "127.0.0.1")
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return {
            "runtime_available": service is not None,
            "api_available": service is not None and service.accepting,
            "api_url": f"ws://{host}:{api.get('port', 8010)}",
            "devices": [{
                "device_id": device_id,
                "online": device_id in online or device_id in voice_sessions,
                **({"voice_online": device_id in voice_sessions} if service is not None else {}),
                **(online[device_id].registration.model_dump(exclude={"type", "device_id"}) if device_id in online else {}),
                **profiles.fields(device_id, paired.digests[device_id]),
            } for device_id in sorted(paired.digests)],
        }

    def live_service(server):
        if server.device_service is None:
            raise HTTPException(503, "Start the Agent runtime to manage devices")
        return server.device_service

    @app.get("/api/devices", tags=["Devices"])
    async def devices_status(response: Response):
        response.headers["Cache-Control"] = "no-store"
        server = resolve_admin()
        if server.device_service is None and runtime_is_active(server.config_dir):
            try:
                upstream = await RuntimeClient(server.config_dir).request("GET", "/api/devices", timeout=1.5)
                return upstream.json()
            except (RuntimeUnavailable, httpx.HTTPError) as exc:
                raise HTTPException(503, "Agent runtime is temporarily unavailable") from exc
        return status(server)

    @app.patch("/api/devices/{device_id}", tags=["Devices"])
    async def update_device(device_id: DeviceId, patch: ProfilePatch):
        server = resolve_admin()
        service = server.device_service
        paired = service.credentials if service is not None else DeviceCredentials(server.config_dir / ".devices.json")
        if device_id not in paired.digests:
            raise HTTPException(404, "Device is not paired with this Agent")
        profiles = service.profiles if service is not None else DeviceProfiles(server.config_dir / ".device_profiles.json")
        try:
            profiles.update(device_id, paired.digests[device_id], patch)
        except OSError as exc:
            raise HTTPException(500, "Device details could not be saved. Please retry.") from exc
        return {"status": "ok", "device_id": device_id, **profiles.fields(device_id, paired.digests[device_id])}

    @app.post("/api/devices/pairing", tags=["Devices"])
    async def devices_pairing(response: Response):
        response.headers["Cache-Control"] = "no-store"
        service = live_service(resolve_admin())
        try:
            return service.create_pairing()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/devices/pairing", tags=["Devices"])
    async def cancel_pairing():
        live_service(resolve_admin()).cancel_pairing()
        return {"status": "ok"}

    @app.delete("/api/devices/{device_id}", tags=["Devices"])
    async def remove_device(device_id: str):
        service = live_service(resolve_admin())
        if device_id not in service.credentials.digests:
            raise HTTPException(404, "Device is not paired with this Agent")
        await service.revoke(device_id)
        return {"status": "ok"}
