import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const chatContext = readFileSync(new URL("../src/context/ChatContext.tsx", import.meta.url), "utf8");
const chatPage = readFileSync(new URL("../src/pages/ChatPage.tsx", import.meta.url), "utf8");

test("steered empty assistant keeps Chinese ack placeholder", () => {
  assert.match(chatContext, /STEERED_ACK_CONTENT\s*=\s*"已收到，做完这步马上回你"/);
  assert.match(chatContext, /steeredStream && !message\.content/);
  assert.doesNotMatch(
    chatContext,
    /\(aborted \|\| steeredStream\) && !message\.content\) return \[\]/,
  );
});

test("chat running badge invites continued sending", () => {
  assert.match(chatPage, /Chat running · 可继续发/);
  assert.match(chatPage, /you can keep sending/);
});
