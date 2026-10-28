import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

// Run the form logic directly without adding a browser or a test framework.
const source = readFileSync(new URL("../src/lib/taskFormUtils.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  convertIntervalUnit,
  createDefaultTaskFormState,
  formStateToCreateInput,
  formStateToDuplicateInput,
  formStateToUpdateInput,
  taskToDuplicateFormState,
  taskToFormState,
  validateTaskForm,
} = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

function intervalTask(seconds) {
  return {
    task_id: "reminder",
    title: "Reminder",
    task_type: "message",
    content: "Drink water",
    next_run_at: "2099-06-01 10:00:10",
    status: "active",
    recurrence: [{ kind: "interval", every_seconds: seconds, end_at: "2099-06-01 10:01:30" }],
  };
}

test("ten-second intervals can be created and intervals below ten seconds are rejected", () => {
  const state = {
    ...createDefaultTaskFormState(),
    content: "Drink water",
    scheduleKind: "interval",
    intervalValue: "10",
    intervalUnit: "seconds",
    intervalEndMode: "duration",
    intervalDurationMinutes: "1",
  };
  assert.equal(validateTaskForm(state), null);
  assert.equal(formStateToCreateInput(state).interval_seconds, 10);
  assert.equal(formStateToCreateInput(state).duration_seconds, 60);
  for (const value of ["9", "9.9", "0", "", "NaN"]) {
    assert.match(validateTaskForm({ ...state, intervalValue: value }), /at least 10 seconds/);
  }
  assert.match(validateTaskForm({ ...state, intervalValue: "10.5" }), /whole seconds/);
});

test("editing short intervals preserves their seconds and schedule timestamps", () => {
  for (const seconds of [10, 30, 90, 300]) {
    const original = intervalTask(seconds);
    const state = taskToFormState(original);
    assert.equal(validateTaskForm(state), null);
    assert.equal(formStateToCreateInput(state).interval_seconds, seconds);
    assert.equal(formStateToCreateInput(state).end_at, original.recurrence[0].end_at);
    assert.equal(formStateToUpdateInput({ ...state, content: "Updated" }, original).interval_seconds, undefined);
  }
  const original = intervalTask(30);
  const state = { ...taskToFormState(original), intervalValue: "10" };
  assert.equal(formStateToUpdateInput(state, original).interval_seconds, 10);
  assert.equal(formStateToUpdateInput(state, original).end_at, original.recurrence[0].end_at);
});

test("switching interval units preserves the actual interval and avoids changing the schedule", () => {
  const original = intervalTask(10);
  const state = taskToFormState(original);
  const minutes = {
    ...state,
    intervalUnit: "minutes",
    intervalValue: convertIntervalUnit(state.intervalValue, "seconds", "minutes"),
  };
  assert.equal(validateTaskForm(minutes), null);
  assert.equal(formStateToCreateInput(minutes).interval_seconds, 10);
  assert.equal(formStateToUpdateInput(minutes, original).interval_seconds, undefined);
  assert.equal(convertIntervalUnit(minutes.intervalValue, "minutes", "seconds"), "10");
  assert.equal(convertIntervalUnit("5", "minutes", "seconds"), "300");
  assert.match(validateTaskForm({ ...minutes, intervalValue: "0.15" }), /at least 10 seconds/);
});

test("duplicating an archived short interval retains its frequency", () => {
  const state = {
    ...taskToDuplicateFormState({ ...intervalTask(10), status: "completed" }),
    intervalDurationMinutes: "1",
  };
  assert.equal(validateTaskForm(state), null);
  assert.equal(formStateToDuplicateInput(state).interval_seconds, 10);
});

test("converting another schedule to an interval preserves seconds in the time window", () => {
  const original = { ...intervalTask(10), recurrence: [{ kind: "daily", time: "09:00:00" }] };
  const state = {
    ...taskToFormState(original),
    scheduleKind: "interval",
    intervalValue: "10",
    intervalUnit: "seconds",
    intervalStartMode: "at",
    intervalStartAt: "2099-06-01T10:00:15",
    intervalEndMode: "duration",
    intervalDurationMinutes: "1",
  };
  assert.equal(validateTaskForm(state), null);
  assert.deepEqual(formStateToUpdateInput(state, original).recurrence, [{
    kind: "interval",
    every_seconds: 10,
    start_at: "2099-06-01 10:00:15",
    end_at: "2099-06-01 10:01:15",
  }]);
});


test("editing an aware one-shot uses the frozen task timezone", () => {
  const task = { task_id: "zoned", title: "Call", task_type: "message", content: "Hello",
    next_run_at: "2099-06-01T02:00:00+00:00", timezone: "Asia/Shanghai", status: "active" };
  assert.equal(taskToFormState(task).runAt, "2099-06-01T10:00:00");
  assert.deepEqual(formStateToUpdateInput(taskToFormState(task), task), { title: "Call", task_type: "message", content: "Hello" });
});
