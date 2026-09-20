import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, IconButton } from "../components/ui";
import { WORLD_NAME_RULE, validateWorldName } from "./protocol";
import { useWorld } from "./WorldContext";

interface CreateWorldDialogProps {
  open: boolean;
  onClose: () => void;
}

export function CreateWorldDialog({ open, onClose }: CreateWorldDialogProps) {
  const { createName, setCreateName, createWorld, creating, createError } = useWorld();
  const inputRef = useRef<HTMLInputElement>(null);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    if (!open) return;
    setLocalError("");
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  if (!open) return null;

  const close = () => {
    if (creating) return;
    setCreateName("");
    setLocalError("");
    onClose();
  };

  return (
    <div className="modal-overlay" role="presentation" onMouseDown={close}>
      <div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-world-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-header world-modal-header">
          <div className="world-modal-header-copy">
            <div className="world-modal-title-row">
              <h3 id="create-world-title">New world</h3>
              <IconButton type="button" onClick={close} disabled={creating} title="Close" aria-label="Close">
                <X size={16} />
              </IconButton>
            </div>
            <p className="wizard-subtitle">{WORLD_NAME_RULE}</p>
          </div>
        </div>
        <form
          className="modal-body"
          onSubmit={(event) => {
            event.preventDefault();
            if (!createName.trim()) {
              setLocalError("Name is required");
              return;
            }
            const invalid = validateWorldName(createName);
            if (invalid) {
              setLocalError(invalid);
              return;
            }
            void createWorld().then((ok) => {
              if (ok) {
                setLocalError("");
                onClose();
              }
            });
          }}
        >
          {localError || createError ? <div className="error-strip">{localError || createError}</div> : null}
          <label className="world-field">
            <span>Name</span>
            <input
              ref={inputRef}
              className="world-input"
              value={createName}
              autoComplete="off"
              spellCheck={false}
              placeholder="plaza"
              disabled={creating}
              onChange={(event) => {
                setCreateName(event.target.value);
                setLocalError("");
              }}
            />
          </label>
          <div className="modal-footer create-world-footer">
            <span />
            <div className="modal-footer-actions">
              <Button type="button" variant="secondary" disabled={creating} onClick={close}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={creating || !createName.trim()}>
                {creating ? "Creating…" : "Create"}
              </Button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
