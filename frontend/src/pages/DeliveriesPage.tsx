import { RefreshCw, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button, EmptyState, IconButton, PageShell, PageToolbar, StatusBadge } from "../components/ui";
import { getDeliveries, retryDelivery } from "../lib/api";
import type { RuntimeDelivery } from "../types";

const attentionStatuses = ["blocked", "unknown", "failed"] as const;

function statusCopy(status: RuntimeDelivery["status"]): string {
  if (status === "blocked") return "The channel was off. Retry only when you want this message sent.";
  if (status === "unknown") return "It may already have been sent before a crash. Verify in the channel before taking action.";
  return "The channel could not send this message. Check its error and configuration.";
}

function targetLabel(target: Record<string, unknown>): string {
  const value = target.user_id || target.chat_id || target.account_id || target.sender_id;
  return typeof value === "string" && value ? value : "Channel default";
}

export function DeliveriesPage() {
  const [deliveries, setDeliveries] = useState<RuntimeDelivery[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const results = await Promise.all(attentionStatuses.map((status) => getDeliveries(status)));
      setDeliveries(
        results
          .flatMap((result) => result.deliveries)
          .sort((left, right) => right.updated_at - left.updated_at),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const retry = async (deliveryId: string) => {
    try {
      await retryDelivery(deliveryId);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <PageShell>
      <PageToolbar
        eyebrow="Needs attention"
        title="Deliveries"
        subtitle="Only outbound messages that require a decision appear here"
        actions={
          <IconButton type="button" title="Refresh" aria-label="Refresh" onClick={() => void load()}>
            <RefreshCw size={15} />
          </IconButton>
        }
      />
      <div className="page-body delivery-issues-body">
        {error ? <div className="error-strip">{error}</div> : null}

        <section className="delivery-issues-section">
          <div className="content-heading">
            <div><h3>Messages requiring review</h3><span>Nothing here is resent automatically.</span></div>
          </div>
          {deliveries.length ? (
            <div className="task-list">
              {deliveries.map((delivery) => (
                <article className="task-row" key={delivery.delivery_id}>
                  <div className="task-row-main">
                    <div className="task-row-title">
                      <h3>{delivery.channel} · {targetLabel(delivery.target)}</h3>
                      <StatusBadge tone={delivery.status === "unknown" || delivery.status === "failed" ? "danger" : delivery.status === "blocked" ? "info" : "muted"}>
                        {delivery.status}
                      </StatusBadge>
                    </div>
                    <p>{String(delivery.payload.content || "No message content")}</p>
                    <p className="delivery-status-copy">{statusCopy(delivery.status)}</p>
                    <div className="chip-list">
                      <span className="data-chip">{delivery.attempts} {delivery.attempts === 1 ? "attempt" : "attempts"}</span>
                      <span className="data-chip">{new Date(delivery.updated_at * 1000).toLocaleString()}</span>
                    </div>
                    {delivery.error ? <p className="task-error-copy">{delivery.error}</p> : null}
                  </div>
                  {delivery.status === "blocked" ? (
                    <Button type="button" onClick={() => void retry(delivery.delivery_id)}>
                      <RotateCcw size={14} />Retry
                    </Button>
                  ) : null}
                </article>
              ))}
            </div>
          ) : (
            <EmptyState title={loading ? "Checking deliveries…" : "No delivery issues"}>
              Blocked, failed, or uncertain outbound messages will appear here.
            </EmptyState>
          )}
        </section>
      </div>
    </PageShell>
  );
}
