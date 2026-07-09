import { useEffect, useRef, useState } from "react";
import { cancelChannelQr, pollChannelQr, startChannelQr } from "../lib/api";
import type { ChannelId, QrSessionResponse } from "../types";

interface QrAuthPanelProps {
  channel: Extract<ChannelId, "feishu" | "weixin">;
  onConfirmed: (result: Record<string, unknown>) => void;
  onError?: (message: string) => void;
}

function qrImageUrl(url: string): string {
  return `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(url)}`;
}

export function QrAuthPanel({ channel, onConfirmed, onError }: QrAuthPanelProps) {
  const [session, setSession] = useState<QrSessionResponse | null>(null);
  const [statusText, setStatusText] = useState("Starting QR session...");
  const confirmedRef = useRef(false);
  const sessionIdRef = useRef<string | null>(null);
  const cancelRequestedRef = useRef(false);

  const cancelActiveSession = (activeChannel: typeof channel) => {
    const sessionId = sessionIdRef.current;
    if (!sessionId || cancelRequestedRef.current) return;
    cancelRequestedRef.current = true;
    sessionIdRef.current = null;
    void cancelChannelQr(activeChannel, sessionId);
  };

  useEffect(() => {
    confirmedRef.current = false;
    cancelRequestedRef.current = false;
    sessionIdRef.current = null;
    setSession(null);
    let cancelled = false;

    const begin = async () => {
      try {
        const started = await startChannelQr(channel);
        if (cancelled) {
          if (started.session_id) {
            void cancelChannelQr(channel, started.session_id);
          }
          return;
        }
        sessionIdRef.current = started.session_id;
        setSession(started);
        setStatusText("Waiting for scan...");
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        onError?.(message);
        setStatusText(message);
      }
    };

    void begin();

    return () => {
      cancelled = true;
      cancelActiveSession(channel);
    };
  }, [channel, onError]);

  useEffect(() => {
    if (!session?.session_id) return undefined;

    const poll = async () => {
      try {
        const next = await pollChannelQr(channel, session.session_id);
        setSession(next);

        if (next.status === "waiting") {
          setStatusText("Scan the QR code or open the link in your browser.");
        } else if (next.status === "scanned") {
          setStatusText("QR scanned. Confirm the login in the app.");
        } else if (next.status === "confirmed" && next.result && !confirmedRef.current) {
          confirmedRef.current = true;
          setStatusText("Authorization confirmed.");
          onConfirmed(next.result);
        } else if (next.status === "expired") {
          setStatusText("QR expired. A new code should appear shortly.");
        } else if (next.status === "error") {
          const message = next.error || "QR authorization failed.";
          setStatusText(message);
          onError?.(message);
        } else if (next.status === "cancelled") {
          setStatusText("Authorization cancelled.");
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setStatusText(message);
        onError?.(message);
      }
    };

    void poll();
    const interval = window.setInterval(() => {
      void poll();
    }, 2000);

    return () => {
      window.clearInterval(interval);
      cancelActiveSession(channel);
    };
  }, [channel, onConfirmed, onError, session?.session_id]);

  return (
    <div className="qr-auth-panel">
      <p className="qr-auth-status">{statusText}</p>
      {session?.qr_url ? (
        <div className="qr-auth-visual">
          <img src={qrImageUrl(session.qr_url)} alt={`${channel} QR code`} width={220} height={220} />
          <a href={session.qr_url} target="_blank" rel="noreferrer" className="qr-auth-link">
            Open authorization link
          </a>
        </div>
      ) : null}
    </div>
  );
}
