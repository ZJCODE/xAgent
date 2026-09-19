import { X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, IconButton } from "../components/ui";
import {
  MEMBER_ID_RULE,
  suggestMemberId,
  validateDisplayName,
  validateMemberId,
  type PersonIdentity,
} from "./protocol";
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
  const [memberId, setMemberId] = useState("");
  const [memberIdTouched, setMemberIdTouched] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!identityDialogOpen) return;
    setError("");
    setMemberIdTouched(false);
    setDisplayName(identity?.display_name || "");
    setMemberId(identity?.member_id || "");
    const timer = window.setTimeout(() => nameRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [identity, identityDialogOpen]);

  if (!identityDialogOpen) return null;

  const onDisplayNameChange = (value: string) => {
    setDisplayName(value);
    setError("");
    if (!memberIdTouched) {
      setMemberId(suggestMemberId(value));
    }
  };

  const submit = async () => {
    const nameErr = validateDisplayName(displayName);
    if (nameErr) {
      setError(nameErr);
      return;
    }
    const handle = memberId.trim() || suggestMemberId(displayName);
    const idErr = validateMemberId(handle, taken);
    if (idErr) {
      setError(idErr);
      return;
    }
    const next: PersonIdentity = {
      member_id: handle,
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
        <div className="modal-header">
          <div className="wizard-header-heading">
            <div>
              <h3 id="person-identity-title">Who are you?</h3>
              <p className="wizard-subtitle">
                Your name is how others see you in the room. The handle is a stable id across reloads.
              </p>
            </div>
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
            <span>Display name</span>
            <input
              ref={nameRef}
              className="world-input"
              value={displayName}
              autoComplete="nickname"
              spellCheck={false}
              placeholder="Alice"
              disabled={saving}
              onChange={(event) => onDisplayNameChange(event.target.value)}
            />
          </label>
          <label className="world-field">
            <span>Handle (member id)</span>
            <small>{MEMBER_ID_RULE}</small>
            <input
              className="world-input"
              value={memberId}
              autoComplete="off"
              spellCheck={false}
              placeholder="alice"
              disabled={saving}
              onChange={(event) => {
                setMemberIdTouched(true);
                setMemberId(event.target.value);
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
