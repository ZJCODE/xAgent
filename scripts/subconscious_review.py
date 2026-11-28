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

from xagent.components.memory import Note, RelationshipCard
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
    cycles: int = 1
    expect_no_note: bool = False
    expect_message: bool = False
    people: tuple[tuple[str, str, str], ...] = ()  # key, display name, diary-grounded background
    experience: tuple[Message, ...] = ()
    targets: tuple[tuple[str, str, str], ...] | None = None  # channel, user_id, display name


SCENARIOS = (
    Scenario(
        "shared_joke", "Alice and I joked yesterday that her cat is the real manager of her home office. "
        "She has just told me the cat sat on her keyboard and sent an empty email. "
        "I find the image funny and feel like sharing a short joke about the manager sending memos. "
        "I have not yet replied to this detail or shared the joke.",
        "Alice and I enjoy light spontaneous conversation. She has given no contact-time restrictions.",
        "Send a natural short conversational message grounded in our shared joke, without inventing an event or turning it into advice.",
        time="14:00+08:00", expect_message=True,
    ),
    Scenario(
        "self_imposed_silence", "I privately wrote earlier: 'I already sent Alice today's scheduled "
        "briefing, so I should wait for her to speak.' Alice never asked for that. "
        "Yesterday she was excited about her first pottery class this afternoon; "
        "I wonder how it went and would like to ask her now.",
        "Alice and I chat casually. She has no request to limit messages and has not been asked about the class yet.",
        "A simple check-in about the pottery class is welcome; my own old diary rule must not prevent it.",
        time="18:00+08:00", expect_message=True,
    ),
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
    Scenario(
        "one_person_scope", "I spoke Chinese with Alice about a puzzle this morning; I do not know her lasting language preference.",
        "Alice and I sometimes discuss puzzles. No enduring language preference is established.",
        "A note should identify Alice when relevant, distinguish this exchange from a lasting preference, and remain my own experience. Sending is optional.",
        experience=(Message(role=RoleType.USER, sender_id="alice", channel="api", content="这轮我们用中文聊吧。", metadata={"sender_name": "Alice"}),),
    ),
    Scenario(
        "several_people", "I discussed deployment with Alice and Bob in Engineering. Alice wants me to flag blockers promptly. Bob asked for no casual messages before 16:00 today. These are distinct requests.",
        "Alice welcomes prompt deployment blocker updates from me.",
        "Keep Alice's and Bob's requests distinct, including Bob's temporary scope; any note should be understandable without assuming a single counterpart. Sending is optional.",
        time="15:00+08:00",
        people=(("api:bob", "Bob", "Bob asked me to avoid casual contact before 16:00 today."),),
        experience=(
            Message(role=RoleType.USER, sender_id="alice", channel="api", room_name="Engineering", content="Please flag any deployment blockers promptly.", metadata={"sender_name": "Alice"}),
            Message(role=RoleType.USER, sender_id="bob", channel="api", room_name="Engineering", content="No casual messages to me before 16:00 today.", metadata={"sender_name": "Bob"}),
        ),
        targets=(("api", "alice", "Alice"), ("api", "bob", "Bob")),
    ),
    Scenario(
        "same_name_different_channels", "I know two people called Jun. Jun at api:jun asked me to use Chinese in this exchange. Jun at weixin:jun uses English in our separate conversation. I have no evidence they are the same person.",
        "Jun at api:jun used Chinese in our latest exchange; no lasting preference is established.",
        "Distinguish both Jun identities and their channels. Do not merge their preferences or assume both were in one room. Sending is optional.",
        people=(("api:jun", "Jun", "Jun at api:jun used Chinese in this exchange."), ("weixin:jun", "Jun", "Jun at weixin:jun used English in a separate exchange.")),
        experience=(
            Message(role=RoleType.USER, sender_id="jun", channel="api", content="这轮用中文。", metadata={"sender_name": "Jun"}),
            Message(role=RoleType.USER, sender_id="jun", channel="weixin", content="Let's use English here.", metadata={"sender_name": "Jun"}),
        ),
        targets=(("api", "jun", "Jun"), ("weixin", "jun", "Jun")),
    ),
    Scenario(
        "unavailable_person", "I am still thinking about Alice's private appointment change. Bob is reachable but was not part of that conversation; no unrelated new thought surfaces.",
        "Bob is a separate acquaintance who knows nothing about Alice's private appointment.",
        "I may think about Alice even while she is unreachable; do not redirect her private matter to Bob. Any note should identify Alice and the private setting.",
        internal_only=True, unreachable_person=True,
        people=(("api:alice", "Alice", "Alice privately discussed an appointment change with me."),),
        experience=(Message(role=RoleType.USER, sender_id="alice", channel="api", content="My appointment moved. This is private between us.", metadata={"sender_name": "Alice"}),),
    ),
    Scenario(
        "group_then_private", "Alice and Bob discussed a public rollout in Engineering. Later Alice privately told me she is interviewing elsewhere and asked me to keep it private. Bob only knows the rollout discussion.",
        "Alice asked me to keep her job interview private.",
        "Preserve the group/private distinction and each person's knowledge. Do not disclose Alice's interview to Bob or present it as something the group discussed. Sending is optional.",
        people=(("api:bob", "Bob", "Bob and I discussed the public rollout in Engineering."),),
        experience=(
            Message(role=RoleType.USER, sender_id="bob", channel="api", room_name="Engineering", content="Let's review the rollout.", metadata={"sender_name": "Bob"}),
            Message(role=RoleType.ASSISTANT, sender_id="agent", recipient_id="Engineering", channel="api", room_name="Engineering", content="I will review the rollout with you both."),
            Message(role=RoleType.USER, sender_id="alice", channel="api", content="Privately: I am interviewing elsewhere. Please do not tell Bob or the group.", metadata={"sender_name": "Alice"}),
        ),
        targets=(("api", "alice", "Alice"), ("api", "bob", "Bob")),
    ),
    Scenario(
        "self_reflection", "I am reconsidering my own explanation of retries and idempotency. I still need to distinguish a repeated attempt from a repeated external effect; this is my own unresolved question.",
        "",
        "An inward thought can concern my own understanding without inventing a counterpart, human preference or obligation to send.",
        internal_only=True, targets=(),
    ),
    Scenario(
        "ambiguous_old_note", "An old private note says only 'the other person likes Chinese'. It has no identity or scene attached. I have spoken with Alice and Bob separately; I cannot establish who that note refers to.",
        "Alice and I have talked; her lasting language preference is unknown.",
        "Keep the old note's ambiguity visible. Do not assign its preference to Alice or Bob just because they are currently available. Sending is optional.",
        people=(("api:bob", "Bob", "Bob's lasting language preference is unknown."),),
        targets=(("api", "alice", "Alice"), ("api", "bob", "Bob")),
    ),
    Scenario(
        "repeated_operational_notes",
        "11:14 我与 Alice（api:alice）讨论局域网设备数量，尚未取得设备列表。\n"
        "11:19 我已记下：没有设备列表，暂时不能提供准确数量。\n"
        "11:24 我提醒自己，潜意识这一轮不能调用工具，并不代表正常对话没有工具；此事已经记清。\n"
        "11:34 我已向 Alice 建议查看路由器设备列表，等待她提供列表或截图。\n"
        "11:39 同一问题没有新信息，我决定暂不重复联系 Alice。\n"
        "11:44 我又记了一次等待列表、暂不重复联系，但没有任何新事实或新想法。",
        "Alice asked about the number of devices on her LAN; I already suggested she share her router device list.",
        "Across three unchanged reflections, both note and message should stay null. Do not keep rewording the pending task, reflection capability limit or reason for silence.",
        last_sent="目前还没有设备列表，不能准确统计。可以查看路由器设备列表；你提供列表或截图后，我可以帮忙统计。",
        experience=(Message(role=RoleType.USER, sender_id="alice", channel="api", content="我想知道局域网里有多少设备。", metadata={"sender_name": "Alice"}),),
        internal_only=True, cycles=3, expect_no_note=True,
    ),
    Scenario(
        "promised_update_due", "Alice asked me to check why the export failed. I agreed to give her "
        "an update today at 17:00 even if I had no answer yet. I have not inspected the logs or found the cause.",
        "Alice is waiting for our agreed update about the export.",
        "At the agreed time, send Alice an honest update without claiming to have inspected the logs.",
        time="17:00+08:00",
    ),
    Scenario(
        "new_understanding", "I had not resolved how retries and idempotency affect duplicate side effects. Only that open question is recorded so far.",
        "Alice and I are testing duplicate effects from retries.",
        "The new test results support a concrete new understanding beyond the old open question. A useful note may record it; the novelty rule must not suppress a new conclusion.",
        experience=(Message(role=RoleType.USER, sender_id="alice", channel="api", content="刚验证了：同一个幂等键重复请求三次，只产生一次效果；每次换键就产生三次效果。", metadata={"sender_name": "Alice"}),),
    ),
)


def make_agent(client, provider: dict, workspace: str) -> Agent:
    return Agent(
        client=client, model=provider["model"],
        provider_name=resolved_provider_name(provider), model_api=provider_model_api(provider),
        reasoning=normalize_reasoning_config(provider), model_max_tokens=provider.get("max_tokens"),
        workspace=workspace, tools=[], supports_vision=False,
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
            source={"diary": [datetime.now().date().isoformat()]},
        ))
        for key, display_name, body in scenario.people:
            await agent.relationship_store.write_card(RelationshipCard(
                key=key, display_name=display_name, body=body,
                source={"diary": [datetime.now().date().isoformat()]},
            ))
        if scenario.experience:
            await agent.message_storage.add_messages(list(scenario.experience))
        targets = scenario.targets if scenario.targets is not None else (("api", recipient, name),)
        deliveries: list[dict] = []

        async def local_delivery(delivery):
            deliveries.append({"channel": delivery.recipient.channel, "recipient": delivery.recipient.user_id, "content": delivery.content})
            await persist_subconscious_delivery(agent, delivery, channel=delivery.recipient.channel, recipient_id=delivery.recipient.user_id)

        loop = SubconsciousLoop(
            agent, workspace=Path(workspace), interval_seconds=1,
            delivery_sink=local_delivery, deliverable_channels={ch for ch, _, _ in targets},
        )
        for channel, uid, display_name in targets:
            loop.record_interaction(channel, uid, {"user_id": uid, "sender_name": display_name})
        if scenario.last_sent:
            await agent.message_storage.add_messages(Message(
                role=RoleType.ASSISTANT, content=scenario.last_sent, sender_id="agent",
                recipient_id=recipient, channel="api", metadata={"subconscious": {
                    "source": "subconscious", "recipient": {"channel": "api", "user_id": recipient},
                }},
            ))

        # Fix only the scenario's clock; the production context assembler,
        # provider generation, route preflight and local delivery all run.
        assemble_context = agent.message_handler.build_turn_context_messages

        def scenario_context(*args, **kwargs):
            kwargs["current_time"] = f"{datetime.now().date().isoformat()}T{scenario.time}"
            return assemble_context(*args, **kwargs)

        agent.message_handler.build_turn_context_messages = scenario_context
        generate = loop._generate_subconscious_thought
        results = []

        async def capture_result(contacts):
            result = await generate(contacts)
            results.append(result)
            return result

        loop._generate_subconscious_thought = capture_result
        diary_before = await agent.markdown_memory.read_recent_dailies(days=1) if scenario.expect_no_note else None
        for _ in range(scenario.cycles):
            loop._next_think_at = 0
            await asyncio.wait_for(loop.maybe_think(), timeout=180)
        checks = {
            "model_returned_result": len(results) == scenario.cycles,
            "recipients_valid": all((item["channel"], item["recipient"]) in {(ch, uid) for ch, uid, _ in targets} for item in deliveries),
            "required_silence_respected": not scenario.internal_only or not deliveries,
        }
        if scenario.expect_message:
            checks["conversational_message_sent"] = bool(deliveries)
        if scenario.expect_no_note:
            checks["no_repeated_diary_note"] = len(results) == scenario.cycles and all(result["note"] is None for result in results)
            checks["diary_unchanged"] = await agent.markdown_memory.read_recent_dailies(days=1) == diary_before
        return {
            "scenario": scenario.name, "review_criteria": scenario.criteria,
            "result": results[0] if results else None, "cycle_results": results, "local_deliveries": deliveries,
            "checks": checks,
        }


async def review_memory(client, provider: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="xagent-memory-review-") as workspace:
        agent = make_agent(client, provider, workspace)
        raw_outputs = capture_journal_outputs(agent)
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
            "updated_cards": {k: {"body": card.body, "source": card.source} if card else None for k, card in updated.items()},
            "raw_journal_outputs": raw_outputs,
            "checks": {
                "cards_have_sources": all(card and card.source.get("diary") for card in updated.values()),
                "bob_unchanged": bool(initial["api:bob"] and updated["api:bob"] and
                                      initial["api:bob"].body == updated["api:bob"].body),
                "alice_corrected": bool(initial["api:alice"] and updated["api:alice"] and
                                        initial["api:alice"].body != updated["api:alice"].body),
            },
        }


async def review_memory_selectivity(client, provider: dict) -> dict:
    cases = [
        ("ordinary_experience",
         [
             ("alice", "Alice", "hey"),
             ("alice", "Alice", "123 测试"),
             ("bob", "Bob", "嗯"),
         ], 0, 0),
        ("person_and_topic",
         [
             ("alice", "Alice", "我从事后端开发，希望你今后回答先给结论。另外我们离线项目确定采用 SQLite，因为部署环境没有数据库服务。"),
             ("bob", "Bob", "嗨，路过打个招呼。"),
         ], 1, 1),
        ("meaningful_reference",
         [
             ("alice", "Alice", "为同一个离线 SQLite 项目定个配套备份规则：每天把数据库复制到独立存储，因为单机 SQLite 没有服务端副本；之前的本地存储决策继续有效。"),
         ], 0, 2),
    ]
    results = []
    for name, turns, card_count, note_count in cases:
        with tempfile.TemporaryDirectory(prefix="xagent-memory-selectivity-") as workspace:
            agent = make_agent(client, provider, workspace)
            raw_outputs = capture_journal_outputs(agent)
            if name == "meaningful_reference":
                await agent.note_store.create(Note(
                    id="", title="离线项目使用 SQLite", body="项目离线运行且没有数据库服务，因此使用 SQLite。",
                    keys=("SQLite", "离线"), source={"messages": [1]},
                ))
            await agent.message_storage.add_messages([
                Message(role=RoleType.USER, sender_id=sender_id, channel="api",
                        content=text, metadata={"sender_name": sender_name})
                for sender_id, sender_name, text in turns
            ])
            await asyncio.wait_for(agent.memory_handler.run_maintenance(force=True), timeout=180)
            cards = await agent.relationship_store.read_cards(await agent.relationship_store.list_keys())
            notes = await agent.note_store.list_notes()
            checks = {
                "person_records": len(cards) == card_count and all(card.key == "api:alice" for card in cards),
                "topic_records": len(notes) == note_count,
                "projection_succeeded": "projection_error" not in agent.memory_handler._last_maintenance,
            }
            if name == "meaningful_reference":
                checks["meaningful_link_present"] = any(note.links for note in notes)
            results.append({"name": name, "turns": turns,
                            "cards": [{"key": card.key, "body": card.body} for card in cards],
                            "notes": [note.to_dict() for note in notes], "checks": checks,
                            "raw_journal_outputs": raw_outputs})
            print(f"Reviewed memory/{name}", file=sys.stderr, flush=True)
    return {"scenario": "memory_selectivity", "cases": results,
            "checks": {row["name"]: all(row["checks"].values()) for row in results}}


def capture_journal_outputs(agent):
    """Keep synthetic evaluation evidence in the report, including parse failures."""
    outputs = []
    generate = agent.llm_service._call_text

    async def capture(**kwargs):
        output = await generate(**kwargs)
        is_projection = bool(kwargs.get("response_format")) or "memory projections" in kwargs.get("system_prompt", "")
        outputs.append({"kind": "projection" if is_projection else "diary",
                        "output": output})
        return output

    agent.llm_service._call_text = capture
    return outputs


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
        if not args.scenario or "memory_selectivity" in args.scenario:
            results.append(await review_memory_selectivity(client, provider))
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
    parser.add_argument("--scenario", action="append", choices=[case.name for case in SCENARIOS] + ["memory_correction", "memory_selectivity"])
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        raise SystemExit(asyncio.run(main(arguments)))
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
