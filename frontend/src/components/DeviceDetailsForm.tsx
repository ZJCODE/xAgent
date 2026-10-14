import { useState } from "react";
import { updateDevice } from "../lib/api";
import type { DeviceInfo, DeviceProfileInput } from "../types";
import { Button } from "./ui";

export function DeviceDetailsForm({ device, onSaved, onCancel }: {
  device: DeviceInfo; onSaved: () => Promise<void>; onCancel: () => void;
}) {
  const [details, setDetails] = useState<DeviceProfileInput>({
    name: device.name || "", location: device.location || "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const fieldId = `device-details-${device.device_id}`;
  return <form className="device-details-form" aria-label={`Edit ${device.name || device.device_id}`} onSubmit={async (event) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true); setError("");
    try { await updateDevice(device.device_id, details); await onSaved(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setSaving(false); }
  }}>
    <div className="device-details-fields">
      <label className="wizard-field" htmlFor={`${fieldId}-name`}><span>Device name</span>
        <input id={`${fieldId}-name`} autoFocus maxLength={256} disabled={saving} value={details.name}
          placeholder="Desk camera" onChange={(event) => setDetails({ ...details, name: event.target.value })} />
      </label>
      <label className="wizard-field" htmlFor={`${fieldId}-location`}><span>Location <small>(optional)</small></span>
        <input id={`${fieldId}-location`} maxLength={256} disabled={saving} value={details.location}
          placeholder="Study · Work desk" onChange={(event) => setDetails({ ...details, location: event.target.value })} />
      </label>
    </div>
    <p id={`${fieldId}-hint`} className="wizard-hint">These details help the Agent choose and understand this device. Update them when you move it.</p>
    {error && <div className="error-strip" role="alert">{error}</div>}
    <div className="toolbar-actions">
      <Button type="submit" variant="primary" disabled={saving}>{saving ? "Saving…" : "Save details"}</Button>
      <Button type="button" disabled={saving} onClick={onCancel}>Cancel</Button>
    </div>
  </form>;
}
