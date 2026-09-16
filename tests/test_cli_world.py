import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from xagent.interfaces.cli import (
    build_parser,
    handle_run_world_internal,
    handle_world_create,
    handle_world_join,
    handle_world_leave,
    handle_world_list,
    handle_world_remove,
    handle_world_start,
    handle_world_status,
    handle_world_stop,
)
from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.cli.launcher import _launcher_options, _world_hub_actions
from xagent.interfaces.cli.overview import build_runtime_overview
from xagent.interfaces.cli.processes import StartResult, iter_managed_process_refs
from xagent.interfaces.cli.world_hub import (
    DEFAULT_WORLD_PORT,
    create_world_on_disk,
    delete_world_on_disk,
    list_world_summaries_from_disk,
    restore_local_world_presence,
    world_hub_config,
    world_hub_paths,
    world_ws_url,
)
from xagent.integrations.world.presence import mark_world_presence, read_world_presence, world_id_from_url


def _write_runtime(directory: str) -> None:
    config = {
        "provider": {
            "name": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "test-key",
            "model": "gpt-5.4-mini",
        },
        "channels": {"api": {"host": "127.0.0.1", "port": 8010}},
    }
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class WorldCliTests(unittest.TestCase):
    def test_parser_exposes_world_lifecycle_and_presence_commands(self):
        parser = build_parser()
        start = parser.parse_args(["world", "start"])
        self.assertEqual(start.handler, handle_world_start)
        join = parser.parse_args(["world", "join", "plaza", "--start-api", "--start-hub"])
        self.assertEqual(join.handler, handle_world_join)
        self.assertEqual(join.world, "plaza")
        self.assertTrue(join.start_api)
        self.assertTrue(join.start_hub)
        chat = parser.parse_args(["world", "chat", "plaza"])
        self.assertEqual(chat.world, "plaza")
        self.assertEqual(chat.member_id, "human")
        remove = parser.parse_args(["world", "remove", "plaza", "--yes"])
        self.assertEqual(remove.handler, handle_world_remove)
        self.assertEqual(remove.world, "plaza")
        self.assertTrue(remove.yes)
        start_open = parser.parse_args(["world", "start", "--open"])
        self.assertTrue(start_open.open_browser)
        for command in (
            ["world", "status", "--open"],
            ["world", "list", "--open"],
            ["world", "create", "plaza", "--open"],
            ["world", "remove", "plaza", "--open"],
            ["world", "join", "plaza", "--open"],
            ["world", "chat", "plaza", "--open"],
        ):
            with self.assertRaises(SystemExit):
                parser.parse_args(command)

    def test_root_help_mentions_world(self):
        help_text = build_parser().format_help()
        self.assertIn("world", help_text)
        self.assertIn("xagent world start", help_text)
        self.assertIn("xagent world join plaza", help_text)
        self.assertIn("xagent world remove plaza", help_text)

    def test_launcher_options_include_world(self):
        titles = [option.title for option in _launcher_options(initialized=True)]
        self.assertIn("World", titles)
        world = next(option for option in _launcher_options(initialized=False) if option.key == "world")
        self.assertFalse(world.disabled)

    def test_world_hub_actions_disable_open_until_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("xagent.interfaces.cli.launcher.running_pid", return_value=None):
                stopped = {option.key: option for option in _world_hub_actions(Path(tmpdir))}
            with patch("xagent.interfaces.cli.launcher.running_pid", return_value=4321):
                running = {option.key: option for option in _world_hub_actions(Path(tmpdir))}
        self.assertTrue(stopped["open"].disabled)
        self.assertFalse(running["open"].disabled)
        with patch("xagent.interfaces.cli.launcher.running_pid", return_value=None):
            titles = [option.title for option in _world_hub_actions(Path(tmpdir))]
        self.assertEqual(
            titles,
            ["Open", "Start", "Stop", "Restart", "Logs", "Back"],
        )

    def test_world_hub_paths_are_machine_level(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.world_hub.management_root", return_value=root):
                paths = world_hub_paths()
        self.assertEqual(paths.pid_path, root / "run" / "world.pid")
        self.assertEqual(paths.log_path, root / "logs" / "world.log")

    def test_world_start_uses_global_pid_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            args = argparse.Namespace(host=None, port=None, open_browser=False)
            with patch("xagent.interfaces.cli.world_hub.management_root", return_value=root):
                with patch("xagent.interfaces.cli.world_hub.start_background", return_value=StartResult(ok=True, pid=4321)) as starter:
                    with patch("xagent.interfaces.cli.world_hub._after_hub_up"):
                        exit_code = handle_world_start(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(starter.call_args.kwargs["pid_path"], root / "run" / "world.pid")
        self.assertEqual(starter.call_args.kwargs["log_path"], root / "logs" / "world.log")
        command = starter.call_args.args[0]
        self.assertIn("_run-world", command)
        self.assertIn(str(root), command)

    def test_world_start_when_already_running_succeeds(self):
        args = argparse.Namespace(host=None, port=None, open_browser=False)
        with patch(
            "xagent.interfaces.cli.world_hub.start_background",
            return_value=StartResult(ok=False, pid=99, error="already running (pid=99)"),
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = handle_world_start(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("already running (pid=99)", stdout.getvalue())

    def test_world_runs_in_foreground(self):
        args = argparse.Namespace(host="127.0.0.1", port=7182, data_root="/tmp/xagent", open_browser=False)

        def _run(coro):
            coro.close()
            return None

        with patch("agents_world.server.WorldHub") as hub_cls:
            hub_cls.create.return_value = object()
            with patch("xagent.interfaces.cli.world_hub.asyncio.run", side_effect=_run) as runner:
                exit_code = handle_run_world_internal(args)
        self.assertEqual(exit_code, 0)
        hub_cls.create.assert_called_once_with(host="127.0.0.1", port=7182, data_root="/tmp/xagent")
        runner.assert_called_once()

    def test_world_status_uses_global_pid_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            args = argparse.Namespace(host=None, port=None, json_output=True)
            with patch("xagent.interfaces.cli.world_hub.management_root", return_value=root):
                with patch("xagent.interfaces.cli.world_hub.running_pid", return_value=88):
                    with patch("xagent.interfaces.cli.world_hub.list_world_summaries", return_value=[{"id": "plaza", "name": "plaza"}]):
                        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = handle_world_status(args)
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["world"]["status"], "running")
        self.assertEqual(payload["world"]["pid"], 88)
        self.assertEqual(payload["world"]["pid_path"], str(root / "run" / "world.pid"))
        self.assertEqual(payload["world"]["world_count"], 1)

    def test_world_stop_uses_global_pid_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.world_hub.management_root", return_value=root):
                with patch("xagent.interfaces.cli.world_hub.stop_managed_process", return_value=(True, "stopped")) as stopper:
                    with patch("sys.stdout", new_callable=io.StringIO):
                        exit_code = handle_world_stop(argparse.Namespace())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stopper.call_args.args[0], root / "run" / "world.pid")

    def test_iter_managed_process_refs_includes_world(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                refs = iter_managed_process_refs(root=root)
        labels = {(ref.scope, ref.agent, ref.channel) for ref in refs}
        self.assertIn(("world", None, None), labels)

    def test_create_and_list_worlds_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            code, payload, error = create_world_on_disk("plaza", root=root)
            self.assertEqual(code, 0)
            self.assertEqual(error, "")
            self.assertEqual(payload["id"], "plaza")
            summaries = list_world_summaries_from_disk(root=root)
            self.assertEqual([item["id"] for item in summaries], ["plaza"])

            args = argparse.Namespace(name="plaza", host=None, port=None)
            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=False):
                with patch("xagent.interfaces.cli.world_hub.world_hub_runtime_root", return_value=root):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = handle_world_create(args)
            self.assertEqual(exit_code, 0)
            created = json.loads(stdout.getvalue())
            self.assertEqual(created["id"], "plaza-2")

            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=False):
                with patch("xagent.interfaces.cli.world_hub.world_hub_runtime_root", return_value=root):
                    with patch("xagent.interfaces.cli.world_hub.fetch_hub_worlds", return_value=None):
                        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = handle_world_list(argparse.Namespace(host=None, port=None, json_output=True))
            self.assertEqual(exit_code, 0)
            listed = json.loads(stdout.getvalue())
            self.assertEqual(listed["worlds"][0]["id"], "plaza")

    def test_remove_world_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            code, payload, error = create_world_on_disk("plaza", root=root)
            self.assertEqual(code, 0, error)
            self.assertTrue((root / "worlds" / "plaza" / "world.sqlite3").is_file())

            args = argparse.Namespace(world="plaza", host=None, port=None, yes=False)
            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=False):
                with patch("xagent.interfaces.cli.world_hub.world_hub_runtime_root", return_value=root):
                    with patch("xagent.interfaces.cli.world_hub.fetch_hub_worlds", return_value=None):
                        with patch("sys.stdin.isatty", return_value=False):
                            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                exit_code = handle_world_remove(args)
            self.assertEqual(exit_code, 1)
            self.assertIn("--yes", stdout.getvalue())
            self.assertTrue((root / "worlds" / "plaza" / "world.sqlite3").is_file())

            args.yes = True
            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=False):
                with patch("xagent.interfaces.cli.world_hub.world_hub_runtime_root", return_value=root):
                    with patch("xagent.interfaces.cli.world_hub.fetch_hub_worlds", return_value=None):
                        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = handle_world_remove(args)
            self.assertEqual(exit_code, 0)
            removed = json.loads(stdout.getvalue().splitlines()[0])
            self.assertEqual(removed["id"], "plaza")
            self.assertTrue(removed["deleted"])
            self.assertFalse((root / "worlds" / "plaza").exists())

            code, payload, error = delete_world_on_disk("plaza", root=root)
            self.assertEqual(code, 1)
            self.assertIn("unknown world", error)

            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=False):
                with patch("xagent.interfaces.cli.world_hub.world_hub_runtime_root", return_value=root):
                    with patch("xagent.interfaces.cli.world_hub.fetch_hub_worlds", return_value=None):
                        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = handle_world_remove(argparse.Namespace(world="plaza", host=None, port=None, yes=True))
            self.assertEqual(exit_code, 1)
            self.assertIn("unknown world", stdout.getvalue())

    def test_remove_clears_local_presence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            agent_dir = root / "agents" / "work"
            _write_runtime(str(agent_dir))
            create_world_on_disk("plaza", root=root)
            mark_world_presence(
                agent_dir,
                world_url="ws://127.0.0.1:7182/ws/plaza",
                member_id="work",
                world_id="plaza",
                want_present=True,
            )
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                register_agent("work", title="Work", make_active=True)
                args = argparse.Namespace(world="plaza", host=None, port=None, yes=True)
                with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=False):
                    with patch("xagent.interfaces.cli.world_hub.world_hub_runtime_root", return_value=root):
                        with patch("xagent.interfaces.cli.world_hub.fetch_hub_worlds", return_value=None):
                            with patch("xagent.interfaces.cli.world_hub.running_pid", return_value=None):
                                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                    exit_code = handle_world_remove(args)
            self.assertEqual(exit_code, 0)
            self.assertIn("recorded leave from plaza", stdout.getvalue())
            self.assertFalse(read_world_presence(agent_dir)["want_present"])

    def test_join_requires_running_hub(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir) / "agents" / "work"
            _write_runtime(str(agent_dir))
            args = argparse.Namespace(
                world="plaza",
                agent=None,
                config_dir=str(agent_dir),
                host=None,
                port=None,
                member_id=None,
                name=None,
                start_hub=False,
                start_api=False,
            )
            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=False):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = handle_world_join(args)
        self.assertEqual(exit_code, 1)
        self.assertIn("World hub is not running", stdout.getvalue())
        self.assertIn("xagent world start", stdout.getvalue())

    def test_join_requires_running_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir) / "agents" / "work"
            _write_runtime(str(agent_dir))
            args = argparse.Namespace(
                world="plaza",
                agent=None,
                config_dir=str(agent_dir),
                host=None,
                port=None,
                member_id=None,
                name=None,
                start_hub=False,
                start_api=False,
            )
            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=True):
                with patch("xagent.interfaces.cli.world_hub.wait_for_world_hub", return_value=True):
                    with patch("xagent.interfaces.cli.world_hub.running_pid", return_value=None):
                        with patch("xagent.interfaces.cli.world_hub.wait_for_agent_world_api", return_value=False):
                            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                exit_code = handle_world_join(args)
        self.assertEqual(exit_code, 1)
        self.assertIn("API channel is not running", stdout.getvalue())
        self.assertIn("xagent api start", stdout.getvalue())

    def test_join_posts_to_agent_world_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            agent_dir = root / "agents" / "work"
            _write_runtime(str(agent_dir))
            args = argparse.Namespace(
                world="plaza",
                agent=None,
                config_dir=str(agent_dir),
                host=None,
                port=None,
                member_id=None,
                name=None,
                start_hub=False,
                start_api=False,
            )
            with patch("xagent.interfaces.cli.world_hub.world_hub_is_running", return_value=True):
                with patch("xagent.interfaces.cli.world_hub.wait_for_world_hub", return_value=True):
                    with patch("xagent.interfaces.cli.world_hub.wait_for_agent_world_api", return_value=True):
                        with patch("xagent.interfaces.cli.world_hub.running_pid", return_value=12):
                            with patch(
                                "xagent.interfaces.cli.world_hub.list_world_summaries",
                                return_value=[{"id": "plaza", "name": "plaza"}],
                            ):
                                with patch(
                                    "xagent.interfaces.cli.world_hub._http_json",
                                    return_value=(200, {"connected": True, "world_id": "plaza", "member_id": "work"}, ""),
                                ) as http:
                                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                        exit_code = handle_world_join(args)
            self.assertEqual(exit_code, 0)
            self.assertIn("joined plaza", stdout.getvalue())
            method, url = http.call_args.args[:2]
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/world/join"))
            self.assertEqual(http.call_args.kwargs["body"]["world_url"], world_ws_url("plaza"))
            presence = read_world_presence(agent_dir)
            self.assertTrue(presence["want_present"])
            self.assertEqual(presence["world_id"], "plaza")

    def test_leave_records_presence_without_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir) / "agents" / "work"
            _write_runtime(str(agent_dir))
            mark_world_presence(
                agent_dir,
                world_url="ws://127.0.0.1:7182/ws/plaza",
                member_id="work",
                world_id="plaza",
                want_present=True,
            )
            args = argparse.Namespace(agent=None, config_dir=str(agent_dir))
            with patch("xagent.interfaces.cli.world_hub.running_pid", return_value=None):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = handle_world_leave(args)
            self.assertEqual(exit_code, 0)
            self.assertIn("recorded leave", stdout.getvalue())
            self.assertFalse(read_world_presence(agent_dir)["want_present"])

    def test_restore_skips_agents_without_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            agent_dir = root / "agents" / "work"
            _write_runtime(str(agent_dir))
            mark_world_presence(
                agent_dir,
                world_url="ws://127.0.0.1:7182/ws/plaza",
                member_id="work",
                world_id="plaza",
                want_present=True,
            )
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                register_agent("work", title="Work", make_active=True)
                with patch("xagent.interfaces.cli.world_hub.running_pid", return_value=None):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        restored = restore_local_world_presence()
            self.assertEqual(restored[0]["ok"], False)
            self.assertEqual(restored[0]["message"], "api not running")
            self.assertIn("api channel is not running", stdout.getvalue())

    def test_overview_includes_world_and_presence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            mark_world_presence(
                tmpdir,
                world_url="ws://127.0.0.1:7182/ws/plaza",
                member_id="work",
                world_id="plaza",
                want_present=True,
            )
            with patch("xagent.interfaces.cli.overview.running_pid", return_value=None):
                overview = build_runtime_overview(Path(tmpdir))
        world = next(item for item in overview.items if item.name == "World")
        self.assertEqual(world.value, "stopped")
        self.assertIn("in plaza", world.detail)

    def test_world_id_from_url(self):
        self.assertEqual(world_id_from_url("ws://127.0.0.1:7182/ws/plaza"), "plaza")
        self.assertEqual(world_id_from_url("http://example"), "")

    def test_world_hub_config_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            cfg = world_hub_config(root=root)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["port"], DEFAULT_WORLD_PORT)


if __name__ == "__main__":
    unittest.main()
