import type { VoiceSelectionInput, VoiceSetupSchema } from "../types";
import { WizardField } from "./WizardField";

interface VoiceSetupFieldsProps {
  schema: VoiceSetupSchema;
  selection: VoiceSelectionInput;
  onChange: (patch: Partial<VoiceSelectionInput>) => void;
}

export function VoiceSetupFields({ schema, selection, onChange }: VoiceSetupFieldsProps) {
  return (
    <div className="wizard-grid">
      <label className="wizard-checkbox">
        <input
          type="checkbox"
          checked={selection.voice_enabled}
          onChange={(event) => onChange({ voice_enabled: event.target.checked })}
        />
        <span>Enable Soniox voice</span>
      </label>

      {selection.voice_enabled ? (
        <>
          <WizardField label="Soniox API key" hint={schema.configured ? "Leave blank to keep the existing key." : undefined}>
            <input
              type="password"
              value={selection.voice_api_key}
              placeholder={schema.placeholders.soniox_api_key}
              autoComplete="off"
              onChange={(event) => onChange({ voice_api_key: event.target.value })}
            />
          </WizardField>

          <WizardField label="Device profile" hint="Room = shared speaker; headset = near-field mic.">
            <select
              value={selection.voice_profile}
              onChange={(event) =>
                onChange({ voice_profile: event.target.value as VoiceSelectionInput["voice_profile"] })
              }
            >
              {schema.profile_options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </WizardField>

          <WizardField label="TTS voice name">
            <input
              value={selection.voice_name}
              onChange={(event) => onChange({ voice_name: event.target.value })}
            />
          </WizardField>

          <WizardField label="Spoken languages" hint="Comma-separated language codes, e.g. zh, en">
            <input
              value={selection.language_hints.join(", ")}
              onChange={(event) =>
                onChange({
                  language_hints: event.target.value
                    .split(",")
                    .map((part) => part.trim())
                    .filter(Boolean),
                })
              }
            />
          </WizardField>

          <WizardField label="Fallback TTS language">
            <input
              value={selection.fallback_language}
              onChange={(event) => onChange({ fallback_language: event.target.value })}
            />
          </WizardField>

          <WizardField label="Recognised names" hint="People, places, or product names (comma-separated).">
            <input
              value={selection.names.join(", ")}
              onChange={(event) =>
                onChange({
                  names: event.target.value
                    .split(",")
                    .map((part) => part.trim())
                    .filter(Boolean),
                })
              }
            />
          </WizardField>

          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={selection.interruptions}
              onChange={(event) => onChange({ interruptions: event.target.checked })}
            />
            <span>Allow barge-in (needs echo-cancelling speakerphone)</span>
          </label>
        </>
      ) : null}
    </div>
  );
}
