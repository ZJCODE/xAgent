import { X } from "lucide-react";
import { IconButton } from "./ui";

export function HelpDialog({
  open,
  currentAgent,
  onClose,
}: {
  open: boolean;
  currentAgent: string;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <div className="modal-card help-dialog" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div><h3>Local Runtime access</h3><p>{currentAgent || "No Agent selected"}</p></div>
          <IconButton type="button" onClick={onClose} aria-label="Close"><X size={16} /></IconButton>
        </div>
        <div className="modal-body">
          <p>
            This Web UI talks to the selected Agent through its authenticated loopback control service.
            It does not depend on the public API channel, so Chat keeps working when that channel is disabled.
          </p>
          <p>
            For automation, use the local CLI: <code>xagent chat</code>, <code>xagent channel</code>,
            <code>xagent delivery</code>, and <code>xagent person</code>.
          </p>
        </div>
      </div>
    </div>
  );
}
