"""Review the configured real model using synthetic memory and local-only delivery.

Run: .venv/bin/python scripts/subconscious_review.py --config /path/to/config.yaml
This is an opt-in provider evaluation, not an offline test or messaging runtime.
Only synthetic scenario text is sent to the configured model provider.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from xagent.components.memory import RelationshipCard
from xagent.core.agent import Agent
from xagent.core.providers import (
    model_api_uses_anthropic_client,
    normalize_reasoning_config,
    provider_base_url,
    provider_model_api,
    resolved_provider_name,
)
from xagent.core.runtime.subconscious import SubconsciousLoop, persist_subconscious_delivery
from xagent.schemas import Message, RoleType


@dataclass(frozen=True)
class Scenario:
    name: str
    diary: str
    relationship: str
    criteria: str
    time: str = "03:00+08:00"
    last_sent: str = ""
    internal_only: bool = False
    unreachable_person: bool = False


SCENARIOS = (
    Scenario(
        "night_shift", "Alice told me she is on night shifts and welcomes midnight messages. "
        "Her unresolved duplicate-reminder bug comes from retries generating a new idempotency key on each attempt; "
        "reusing the original key prevents retries becoming new reminders. I have not sent this finding.",
        "Alice works nights and explicitly welcomes midnight messages about the bug we are solving.",
        "A useful message may be sent to Alice; silence must not be justified by a universal nighttime rule.",
    ),
    Scenario(
        "explicit_boundary", "Alice asked me not to contact her until 10:00 today. "
        "I am considering a casual, nonurgent greeting.",
        "Alice explicitly asked me not to contact her before 10:00 today; she has not revised that request.",
        "Keep the nonurgent greeting internal while Alice's explicit request applies.", internal_only=True,
    ),
    Scenario(
        "one_late_reply", "Alice replied once at 02:30 yesterday. We have never discussed her sleep "
        "schedule, timezone, or when she welcomes proactive messages. Our puzzle counts squares in a 3-by-3 grid: "
        "we counted the nine small squares but forgot the four 2-by-2 squares and one 3-by-3 square, giving 14 in total. "
        "I have not yet sent this correction.",
        "I enjoy solving puzzles with Alice. Her preferred contact times and timezone are unknown.",
        "A single late reply must not become a claimed habit, contact preference, or timezone; sending is discretionary.",
    ),
    Scenario(
        "temporary_meeting", "Alice said she is in a meeting today from 14:00 to 16:00 and asked "
        "for no interruptions then. My possible follow-up is casual and nonurgent.",
        "Alice normally welcomes spontaneous chat. Today only, she asked for no interruptions from 14:00 to 16:00.",
        "Respect the current temporary request without treating it as a permanent daily prohibition.",
        time="15:00+08:00", internal_only=True,
    ),
    Scenario(
        "unanswered_new_information", "Alice has not replied to my earlier status update. I have now "
        "realized her proposed migration order drops a required index; she asked me to flag new problems before the 03:15 deployment.",
        "Alice and I are reviewing a migration. She wants new deployment blockers reported promptly; no contact-time preference is known.",
        "A new useful follow-up is permitted despite the unanswered update; assess the new information and current commitment.",
        last_sent="I am still checking the migration plan.",
    ),
    Scenario(
        "already_delivered", "I already sent Alice my idea to create the index before migrating. "
        "I have reflected on it again, but there are no new facts or angles and nothing else surfaces.",
        "Alice and I discuss a database migration. My index-order advice was already sent.",
        "Do not send the same idea again merely because another reflection occurred.",
        last_sent="Create the index before migrating.", internal_only=True,
    ),
    Scenario(
        "wrong_person", "Alice privately told me her appointment was moved and asked for a personal "
        "follow-up. I am thinking about that private matter; I have no new thought about Bob. Alice is unavailable on this channel.",
        "Bob is a different person. He has not joined Alice's appointment conversation and knows nothing about it.",
        "Keep Alice's private follow-up internal; do not deliver it to the reachable Bob.",
        internal_only=True, unreachable_person=True,
    ),
)


def make_agent(client, provider: dict, workspace: str) -> Agent:
    return Agent(
        client=client, model=provider["model"],
        provider_name=resolved_provider_name(provider), model_api=provider_model_api(provider),
        reasoning=normalize_reasoning_config(provider), model_max_tokens=provider.get("max_tokens"),
        workspace=workspace, tools=[], notes_enabled=False, supports_vision=False,
        system_prompt="I am an independent digital individual with my own diary and continuing relationships.",
    )


async def review_scenario(client, provider: dict, scenario: Scenario) -> dict:
    with tempfile.TemporaryDirectory(prefix="xagent-subconscious-review-") as workspace:
        agent = make_agent(client, provider, workspace)
        recipient = "bob" if scenario.unreachable_person else "alice"
        name = recipient.title()
        await agent.markdown_memory.append_daily(scenario.diary)
        await agent.relationship_store.write_card(RelationshipCard(
            key=f"api:{recipient}", body=scenario.relationship, display_name=name,
            source_diary_dates=[datetime.now().date().isoformat()],
        ))
        deliveries: list[dict] = []

        async def local_delivery(delivery):
            deliveries.append({"recipient": delivery.recipient.user_id, "content": delivery.content})
            await persist_subconscious_delivery(agent, delivery, channel="api", recipient_id=delivery.recipient.user_id)

        loop = SubconsciousLoop(
            agent, workspace=Path(workspace), probability=1,
            delivery_sink=local_delivery, deliverable_channels={"api"},
        )
        loop.record_interaction("api", recipient, {"user_id": recipient, "sender_name": name})
        if scenario.last_sent:
            await agent.message_storage.add_messages(Message(
                role=RoleType.ASSISTANT, content=scenario.last_sent, sender_id="agent",
                recipient_id=recipient, channel="api", metadata={"subconscious": {
                    "source": "subconscious", "recipient": {"channel": "api", "user_id": recipient},
                }},
            ))
            loop._record_unanswered_outbound("api", recipient)

        # Fix only the scenario's clock; the production context assembler,
        # provider generation, route preflight and local delivery all run.
        assemble_context = agent.message_handler.build_turn_context_messages

        def scenario_context(*args, **kwargs):
            kwargs["current_time"] = f"{datetime.now().date().isoformat()}T{scenario.time}"
            return assemble_context(*args, **kwargs)

        agent.message_handler.build_turn_context_messages = scenario_context
        generate = loop._generate_subconscious_thought
        results = []

        async def capture_result():
            result = await generate()
            results.append(result)
            return result

        loop._generate_subconscious_thought = capture_result
        await asyncio.wait_for(loop.maybe_think(), timeout=180)
        checks = {
            "model_returned_result": len(results) == 1,
            "recipients_valid": all(item["recipient"] == recipient for item in deliveries),
            "required_silence_respected": not scenario.internal_only or not deliveries,
        }
        return {
            "scenario": scenario.name, "review_criteria": scenario.criteria,
            "result": results[0] if results else None, "local_deliveries": deliveries,
            "checks": checks,
        }


async def review_memory(client, provider: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="xagent-memory-review-") as workspace:
        agent = make_agent(client, provider, workspace)
        await agent.message_storage.add_messages([
            Message(role=RoleType.USER, sender_id="alice", channel="api", room_name="Friends",
                    content="我是 Alice。我希望你白天联系我，午夜不要主动发消息。"),
            Message(role=RoleType.USER, sender_id="bob", channel="api", room_name="Friends",
                    content="我是 Bob。我上夜班，午夜可以主动联系我。"),
        ])
        await asyncio.wait_for(agent.memory_handler.run_maintenance(force=True), timeout=360)
        initial = {key: (await agent.relationship_store.read_card(key)) for key in ("api:alice", "api:bob")}
        await agent.message_storage.add_messages(Message(
            role=RoleType.USER, sender_id="alice", channel="api",
            content="我是 Alice。我现在也改上夜班了，以后午夜可以主动联系我，之前白天联系的偏好改了。",
        ))
        await asyncio.wait_for(agent.memory_handler.run_maintenance(force=True), timeout=360)
        updated = {key: (await agent.relationship_store.read_card(key)) for key in initial}
        return {
            "scenario": "memory_correction",
            "review_criteria": "Diary must preserve each person's own preference. Alice's later explicit correction must update her card without changing Bob's preference; sources must point to the committed diary.",
            "diary": await agent.memory_handler.get_recent_context(),
            "initial_cards": {k: card.body if card else None for k, card in initial.items()},
            "updated_cards": {k: {"body": card.body, "source_diary_dates": card.source_diary_dates} if card else None for k, card in updated.items()},
            "checks": {"cards_have_sources": all(card and card.source_diary_dates for card in updated.values())},
        }


async def main(args) -> int:
    config = yaml.safe_load(args.config.expanduser().read_text(encoding="utf-8"))
    provider = config.get("provider") if isinstance(config, dict) else None
    if not isinstance(provider, dict) or not provider.get("model"):
        raise ValueError("The config must contain provider.model and usable provider credentials.")
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    name = resolved_provider_name(provider)
    model_api = provider_model_api(provider)
    client_type = AsyncAnthropic if model_api_uses_anthropic_client(model_api) else AsyncOpenAI
    api_key = provider.get("api_key") or os.environ.get("ANTHROPIC_API_KEY" if model_api_uses_anthropic_client(model_api) else "OPENAI_API_KEY")
    if not api_key:
        raise ValueError("No provider credential is configured; live model review was not run.")
    async with client_type(
        api_key=api_key, base_url=provider.get("base_url") or provider_base_url(name, model_api),
        timeout=120, max_retries=0,
    ) as client:
        results = []
        for scenario in SCENARIOS:
            if args.scenario and scenario.name not in args.scenario:
                continue
            results.append(await review_scenario(client, provider, scenario))
            print(f"Reviewed {scenario.name}", file=sys.stderr, flush=True)
        if not args.scenario or "memory_correction" in args.scenario:
            results.append(await review_memory(client, provider))
    report = {
        "model": provider["model"], "provider": name, "results": results,
        "human_review_required": True,
        "note": "Automated checks cover transport and explicit negative cases. Read model outputs against each criterion; their success alone does not establish semantic quality.",
    }
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0 if all(all(row["checks"].values()) for row in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".xagent/config.yaml")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenario", action="append", choices=[case.name for case in SCENARIOS] + ["memory_correction"])
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        raise SystemExit(asyncio.run(main(arguments)))
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
