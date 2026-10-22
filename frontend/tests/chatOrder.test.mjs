import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/chatOrder.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { insertUserBeforeRunningReply, placeQueuedFollowup, placeSteeredUser } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

function message(id, role, content) {
  return { id, role, content };
}

test("a steered user message is inserted above the running reply", () => {
  const messages = [
    message("user-1", "user", "今天杭州有什么新闻"),
    message("assistant-1", "assistant", "正在查杭州新闻"),
  ];
  const steer = message("user-2", "user", "财经方向");
  assert.deepEqual(
    insertUserBeforeRunningReply(messages, steer).map((item) => item.content),
    ["今天杭州有什么新闻", "财经方向", "正在查杭州新闻"],
  );
});

test("a steer stays above every assistant bubble already in the running turn", () => {
  const messages = [
    message("user-1", "user", "今天杭州有什么新闻"),
    message("assistant-interim", "assistant", "先看综合新闻"),
    message("assistant-final", "assistant", "财经方面"),
  ];
  const placed = placeSteeredUser([...messages, message("user-2", "user", "财经方向")], "user-2");
  assert.deepEqual(
    placed.map((item) => item.id),
    ["user-1", "user-2", "assistant-interim", "assistant-final"],
  );
  assert.equal(placed[1].meta, undefined);
  assert.equal(
    placed.some((item) => item.meta === "Added to the running task" || item.meta === "Will be handled next"),
    false,
  );
});

test("a second steer lines up with the earlier user messages", () => {
  const messages = [
    message("user-1", "user", "今天杭州有什么新闻"),
    message("user-2", "user", "财经方向"),
    message("assistant-1", "assistant", "财经方面"),
  ];
  const placed = insertUserBeforeRunningReply(messages, message("user-3", "user", "只要上市公司"));
  assert.deepEqual(
    placed.map((item) => item.id),
    ["user-1", "user-2", "user-3", "assistant-1"],
  );
});

test("placing an already ordered steer does not move it again", () => {
  const messages = [
    message("user-1", "user", "今天杭州有什么新闻"),
    message("user-2", "user", "财经方向"),
    message("assistant-1", "assistant", "财经方面"),
  ];
  const placed = placeSteeredUser(messages, "user-2");
  assert.deepEqual(
    placed.map((item) => item.id),
    ["user-1", "user-2", "assistant-1"],
  );
  assert.equal(placed[1].meta, undefined);
});

test("a rejected steer falls back below the running reply with its own assistant bubble", () => {
  const messages = [
    message("user-1", "user", "今天杭州有什么新闻"),
    message("user-2", "user", "财经方向"),
    message("assistant-1", "assistant", "财经方面"),
  ];
  const queued = placeQueuedFollowup(messages, "user-2", message("assistant-2", "assistant", ""));
  assert.deepEqual(
    queued.map((item) => item.id),
    ["user-1", "assistant-1", "user-2", "assistant-2"],
  );
  assert.equal(queued[2].meta, undefined);
  assert.equal(
    queued.some((item) => item.meta === "Added to the running task" || item.meta === "Will be handled next"),
    false,
  );
});

test("older turns stay above the running reply when a new steer arrives", () => {
  const messages = [
    message("user-old", "user", "你好"),
    message("assistant-old", "assistant", "你好"),
    message("user-1", "user", "今天杭州有什么新闻"),
    message("assistant-1", "assistant", ""),
  ];
  const placed = insertUserBeforeRunningReply(messages, message("user-2", "user", "财经方向"));
  assert.deepEqual(
    placed.map((item) => item.id),
    ["user-old", "assistant-old", "user-1", "user-2", "assistant-1"],
  );
});
