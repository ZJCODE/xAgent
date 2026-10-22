export interface OrderedChatMessage {
  id: string;
  role: string;
  meta?: string;
}

function trailingAssistantRun<T extends OrderedChatMessage>(messages: T[]): number {
  let index = messages.length - 1;
  if (index < 0 || messages[index].role !== "assistant") return -1;
  while (index > 0 && messages[index - 1].role === "assistant") index -= 1;
  return index;
}

/** Place a user message immediately before the reply already on screen for this turn. */
export function insertUserBeforeRunningReply<T extends OrderedChatMessage>(messages: T[], userMessage: T): T[] {
  const start = trailingAssistantRun(messages);
  if (start < 0) return [...messages, userMessage];
  return [...messages.slice(0, start), userMessage, ...messages.slice(start)];
}

/** Keep a confirmed steer above the running reply, including when it was appended later. */
export function placeSteeredUser<T extends OrderedChatMessage>(messages: T[], userMessageId: string): T[] {
  const index = messages.findIndex((message) => message.id === userMessageId);
  if (index < 0) return messages;
  const user = messages[index];
  const rest = messages.filter((message) => message.id !== userMessageId);
  return insertUserBeforeRunningReply(rest, user);
}

/** A message that could not join the running turn is the next turn, after that reply. */
export function placeQueuedFollowup<T extends OrderedChatMessage>(
  messages: T[],
  userMessageId: string,
  assistantMessage: T,
): T[] {
  const user = messages.find((message) => message.id === userMessageId);
  const rest = messages.filter(
    (message) => message.id !== userMessageId && message.id !== assistantMessage.id,
  );
  if (!user) return [...rest, assistantMessage];
  return [...rest, user, assistantMessage];
}
