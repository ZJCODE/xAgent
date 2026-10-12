"""A software device implementing the same protocol as a hardware executor.

Run from the repository with:
    XAGENT_PAIRING_CODE=code-from-Agent-page python examples/device_executor.py --device-id desk
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus


def save_credential(path: Path, api_url: str, device_id: str, credential: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".device-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"api_url": api_url, "device_id": device_id, "credential": credential}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def run(args):
    args.api_url = args.api_url.rstrip("/")
    url = urlsplit(args.api_url)
    if url.scheme not in {"ws", "wss"} or not url.hostname:
        raise ValueError("api-url must use ws or wss")
    if url.scheme == "ws" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote api-url must use wss")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("api-url must not contain credentials, a query or a fragment")
    key = hashlib.sha256((args.api_url + "\0" + args.device_id).encode()).hexdigest()
    credential_path = Path(args.credentials_file).expanduser() if args.credentials_file else Path.home() / ".local/share/xagent/device-credentials" / f"{key}.json"
    token = None
    if not token and credential_path.exists():
        saved = json.loads(credential_path.read_text())
        if saved.get("api_url") != args.api_url or saved.get("device_id") != args.device_id or not isinstance(saved.get("credential"), str):
            raise ValueError("Credential file does not belong to this Agent address and device ID")
        token = saved["credential"]
    if not token and not args.pairing_code:
        raise ValueError("Add a device on the Agent page and set XAGENT_PAIRING_CODE for the first connection")
    state = {"value": 0}
    # websockets 15+ otherwise picks up ambient HTTP proxy settings.
    connection_options = {"proxy": None} if "proxy" in inspect.signature(websockets.connect).parameters else {}
    registration = {
        "type": "register", "device_id": args.device_id,
        "name": args.name, "location": args.location,
        "metadata": {"implementation": "Python demo"},
        "capabilities": [
            {"name": "counter.read", "description": "Read the current counter", "parameters": {"type": "object", "additionalProperties": False}},
            {"name": "counter.set", "description": "Set the counter to an integer", "parameters": {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": ["value"], "additionalProperties": False,
            }},
        ],
    }
    while True:
        try:
            async with websockets.connect(
                args.api_url.rstrip("/") + "/ws/devices/" + quote(args.device_id, safe=""),
                additional_headers={"Authorization": "Bearer " + (token or args.pairing_code)}, max_size=64 * 1024,
                **connection_options,
            ) as socket:
                await socket.send(json.dumps(registration))
                print(f"Connected device {args.device_id}", flush=True)
                async for raw in socket:
                    command = json.loads(raw)
                    if command.get("type") == "paired":
                        if token or command.get("device_id") != args.device_id or not isinstance(command.get("credential"), str):
                            raise ValueError("Invalid pairing response")
                        save_credential(credential_path, args.api_url, args.device_id, command["credential"])
                        token = command["credential"]
                        print(f"Paired device {args.device_id}; credential saved", flush=True)
                        continue
                    result = {"type": "result", "command_id": command["command_id"]}
                    params = command.get("params", {})
                    try:
                        if command["capability"] == "counter.read":
                            if params:
                                raise ValueError("counter.read takes no parameters")
                        elif command["capability"] == "counter.set":
                            if set(params) != {"value"} or type(params["value"]) is not int:
                                raise ValueError("counter.set requires exactly one integer value")
                            state["value"] = params["value"]
                        else:
                            raise ValueError("Unsupported capability")
                        result.update(ok=True, data=dict(state))
                    except ValueError as exc:
                        result.update(ok=False, error=str(exc))
                    await socket.send(json.dumps(result))
            # A normal 1000/1001 close ends the iterator without an exception.
            # Back off before reconnecting during API drain or Runtime restart.
            print("Disconnected; reconnecting", flush=True)
            await asyncio.sleep(1)
        except InvalidStatus as exc:
            if exc.response.status_code in {401, 403}:
                raise ValueError("Device authentication rejected. Create a fresh pairing code; for a revoked device, remove its saved credential first") from None
            await asyncio.sleep(1)
        except (ConnectionClosed, OSError):
            print("Disconnected; reconnecting", flush=True)
            await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="ws://127.0.0.1:8010", help="Agent API WebSocket base URL")
    parser.add_argument("--device-id", default="desk")
    parser.add_argument("--name", default="Demo counter")
    parser.add_argument("--location", default="office")
    parser.add_argument("--pairing-code", default=os.environ.get("XAGENT_PAIRING_CODE"), help="One-time code for initial pairing")
    parser.add_argument("--credentials-file", help="Local credential file; defaults to a file per Agent address and device ID")
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments))
    except KeyboardInterrupt:
        pass
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
