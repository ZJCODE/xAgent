import { classNames } from "../lib/format";
import {
  AWAITING_REPLY_LABEL,
  AWAITING_REPLY_TIMEOUT_LABEL,
} from "../lib/awaitingReply";

export function AwaitingReply({
  timedOut = false,
  className,
}: {
  timedOut?: boolean;
  className?: string;
}) {
  return (
    <div
      className={classNames("awaiting-reply", timedOut && "is-muted", className)}
      aria-live="polite"
    >
      {timedOut ? AWAITING_REPLY_TIMEOUT_LABEL : AWAITING_REPLY_LABEL}
    </div>
  );
}
