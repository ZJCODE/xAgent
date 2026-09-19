#!/usr/bin/env python3
"""Smoke: many humans + agents in one world (requires a running hub)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

from agents_world.client import WorldClient


def http(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


async def main() -> int:
    parser = argparse.ArgumentParser(description="Join N humans + M agents and speak once each.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7182)
    parser.add_argument("--world", default="stress-smoke")
    parser.add_argument("--humans", type=int, default=5)
    parser.add_argument("--agents", type=int, default=4)
    args = parser.parse_args()
    base = f"http://{args.host}:{args.port}"
    ws = f"ws://{args.host}:{args.port}/ws/{args.world}"

    try:
        http(base, f"/worlds/create?name={args.world}")
    except Exception:
        pass

    humans = [(f"h{i}", f"H{i}") for i in range(args.humans)]
    agents = [(f"a{i}", f"A{i}") for i in range(args.agents)]
    members = [(mid, name, "human") for mid, name in humans] + [
        (mid, name, "agent") for mid, name in agents
    ]

    clients: list[WorldClient] = []
    for mid, name, kind in members:
        c = WorldClient(ws, member_id=mid, display_name=name, kind=kind)
        await c.connect()
        await c.join()
        await c.wait_for(lambda m: m.get("type") == "snapshot" and not m.get("sync"), timeout=15)
        clients.append(c)

    summary = next(w for w in http(base, "/worlds")["worlds"] if w["id"] == args.world)
    expected = len(members)
    if summary.get("present_count") != expected:
        print(f"FAIL present_count={summary.get('present_count')} expected {expected}", file=sys.stderr)
        return 1

    for c in clients:
        await c.speak(f"ok-{c.member_id}")
    await asyncio.sleep(0.3)

    for c in clients:
        await c.leave()
        await c.close()

    print(
        f"OK world={args.world} present={expected} "
        f"by_kind={summary.get('present_by_kind')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
