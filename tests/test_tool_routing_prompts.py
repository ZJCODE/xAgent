"""Assert execution-tool routing guidance stays in tool prompts."""
from __future__ import annotations

import unittest

from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.tools.jobs_tool import create_manage_jobs_tool
from xagent.tools.scheduler_tool import create_schedule_task_tool
from xagent.tools.shell_tool import run_command


class ToolRoutingPromptTests(unittest.TestCase):
    def test_tool_system_prompts_encode_three_primitive_routing(self):
        command = AgentConfig.TOOL_SYSTEM_PROMPTS["run_command"]
        tasks = AgentConfig.TOOL_SYSTEM_PROMPTS["manage_scheduled_tasks"]
        jobs = AgentConfig.TOOL_SYSTEM_PROMPTS["manage_jobs"]
        skills = AgentConfig.TOOL_SYSTEM_PROMPTS["read_skill"]

        self.assertIn("manage_jobs", command)
        self.assertIn("~30s", command)
        self.assertIn("manage_scheduled_tasks", command)

        self.assertIn("one-shot", tasks.lower())
        self.assertIn("daily", tasks.lower())
        self.assertIn("weekly", tasks.lower())
        self.assertIn("interval", tasks.lower())
        self.assertIn("not a background job", tasks.lower())
        self.assertIn("manage_jobs", tasks)
        self.assertIn("Periodic repetition", tasks)

        self.assertIn("handed off", jobs.lower())
        self.assertIn("~30s", jobs)
        self.assertIn("Do not wait, poll in a loop", jobs)
        self.assertIn("manage_scheduled_tasks", jobs)
        self.assertIn("run_command", jobs)

        self.assertIn("manage_jobs", skills)
        self.assertIn("run_command", skills)

    def test_assembled_tool_policy_includes_routing_for_active_tools(self):
        policy = MessageHandler._build_tool_policy(
            ["run_command", "manage_scheduled_tasks", "manage_jobs", "read_skill"]
        )
        self.assertIn("<tool_policy>", policy)
        self.assertIn("Periodic repetition", policy)
        self.assertIn("handed off", policy.lower())
        self.assertIn("Do not wait, poll in a loop", policy)
        self.assertIn("~30s", policy)

    def test_tool_descriptions_mirror_routing_one_liners(self):
        self.assertIn("manage_jobs", run_command.tool_spec["function"]["description"])
        self.assertIn("~30s", run_command.tool_spec["function"]["description"])

        schedule_tool = create_schedule_task_tool(tasks_dir="/tmp/xagent-test-tasks")
        schedule_desc = schedule_tool.tool_spec["function"]["description"]
        self.assertIn("one-shot", schedule_desc.lower())
        self.assertIn("not a background job", schedule_desc.lower())
        self.assertIn("manage_jobs", schedule_desc)

        jobs_tool = create_manage_jobs_tool(jobs_dir="/tmp/xagent-test-jobs")
        jobs_desc = jobs_tool.tool_spec["function"]["description"]
        self.assertIn("handed off", jobs_desc.lower())
        self.assertIn("manage_scheduled_tasks", jobs_desc)
        self.assertIn("~30s", jobs_desc)


if __name__ == "__main__":
    unittest.main()
