/** Same-origin WebSocket URL. HTTPS pages use WSS. */
export function webSocketUrl(path: string): string {
  const url = new URL(path, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
