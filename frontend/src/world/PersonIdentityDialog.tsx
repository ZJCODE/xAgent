import { X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, IconButton } from "../components/ui";
import { allocateMemberId, validateDisplayName, type PersonIdentity } from "./protocol";
import { useWorld } from "./WorldContext";

export function PersonIdentityDialog() {
  const {
    identityDialogOpen,
    identityPrompt,
    identity,
    neighbors,
    closeIdentityDialog,
    setIdentity,
  } = useWorld();
  const taken = useMemo(() => new Set(neighbors.map((agent) => agent.name)), [neighbors]);
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const nameTaken = identityPrompt.toLowerCase().includes("already");

  useEffect(() => {
    if (!identityDialogOpen) return;
    setError("");
    setDisplayName(identity?.display_name || "");
    const timer = window.setTimeout(() => nameRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [identity, identityDialogOpen]);

  if (!identityDialogOpen) return null;

  const submit = async () => {
    const nameErr = validateDisplayName(displayName);
    if (nameErr) {
      setError(nameErr);
      return;
    }
    const reserved = new Set(taken);
    if (nameTaken && identity?.member_id) reserved.add(identity.member_id);
    const next: PersonIdentity = {
      member_id: allocateMemberId(
        displayName,
        reserved,
        nameTaken ? undefined : identity?.member_id,
      ),
      display_name: displayName.trim(),
    };
    setSaving(true);
    try {
      await setIdentity(next);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onMouseDown={() => !saving && closeIdentityDialog()}>
      <div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="person-identity-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-header world-modal-header">
          <div className="world-modal-header-copy">
            <div className="world-modal-title-row">
              <h3 id="person-identity-title">Who are you?</h3>
              <IconButton
                type="button"
                onClick={closeIdentityDialog}
                disabled={saving}
                title="Close"
                aria-label="Close"
              >
                <X size={16} />
              </IconButton>
            </div>
              <p className="wizard-subtitle">
                Your name is how others see you in the room. This browser remembers it.
              </p>
          </div>
        </div>
        <form
          className="modal-body"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          {identityPrompt ? <p className="world-hint">{identityPrompt}</p> : null}
          {error ? <div className="error-strip">{error}</div> : null}
          <label className="world-field">
            <span>Name</span>
            <input
              ref={nameRef}
              className="world-input"
              value={displayName}
              autoComplete="nickname"
              autoCapitalize="words"
              spellCheck={false}
              lang="en"
              placeholder="Alice"
              disabled={saving}
              onChange={(event) => {
                setDisplayName(event.target.value);
                setError("");
              }}
            />
          </label>
          <div className="modal-footer create-world-footer">
            <span />
            <div className="modal-footer-actions">
              <Button type="button" variant="secondary" disabled={saving} onClick={closeIdentityDialog}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={saving || !displayName.trim()}>
                {saving ? "Entering…" : "Enter"}
              </Button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
