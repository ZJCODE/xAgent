import type { SetupOption, VoiceSelectionInput } from "../types";
import { WizardField } from "./WizardField";

export interface VoiceSetupFieldsSchema {
  voice_providers: SetupOption[];
  voice_custom_providers: string[];
  inherit_api_key_from?: {
    provider: string;
    can_inherit_qwen_key: boolean;
  };
}

interface VoiceSetupFieldsProps {
  schema: VoiceSetupFieldsSchema;
  selection: VoiceSelectionInput;
  onChange: (patch: Partial<VoiceSelectionInput>) => void;
  showEnableToggle?: boolean;
}

export function VoiceSetupFields({
  schema,
  selection,
  onChange,
  showEnableToggle = false,
}: VoiceSetupFieldsProps) {
  const enabled = showEnableToggle ? Boolean(selection.voice_enabled) : true;
  const providers = schema.voice_providers.filter((provider) => provider.id !== "none");
  const inheritHint =
    schema.inherit_api_key_from?.can_inherit_qwen_key && selection.voice_provider === "qwen"
      ? `Leave blank to reuse the agent ${schema.inherit_api_key_from.provider} API key.`
      : undefined;

  return (
    <div className="wizard-grid">
      {showEnableToggle ? (
        <label className="wizard-checkbox">
          <input
            type="checkbox"
            checked={Boolean(selection.voice_enabled)}
            onChange={(event) =>
              onChange({
                voice_enabled: event.target.checked,
                voice_provider: event.target.checked
                  ? selection.voice_provider === "none"
                    ? "soniox"
                    : selection.voice_provider
                  : "none",
              })
            }
          />
          <span>Enable voice mode</span>
        </label>
      ) : null}

      {enabled ? (
        <>
          <WizardField label="Voice provider">
            <select
              value={selection.voice_provider}
              onChange={(event) => onChange({ voice_provider: event.target.value })}
            >
              {providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.id}
                </option>
              ))}
            </select>
          </WizardField>

          {selection.voice_provider === "custom" ? (
            <>
              <WizardField label="STT provider">
                <select
                  value={selection.voice_stt_provider}
                  onChange={(event) => onChange({ voice_stt_provider: event.target.value })}
                >
                  {schema.voice_custom_providers.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider}
                    </option>
                  ))}
                </select>
              </WizardField>
              <WizardField label="STT API key" hint={inheritHint}>
                <input
                  type="password"
                  value={selection.voice_stt_api_key}
                  autoComplete="off"
                  onChange={(event) => onChange({ voice_stt_api_key: event.target.value })}
                />
              </WizardField>
              <WizardField label="TTS provider">
                <select
                  value={selection.voice_tts_provider}
                  onChange={(event) => onChange({ voice_tts_provider: event.target.value })}
                >
                  {schema.voice_custom_providers.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider}
                    </option>
                  ))}
                </select>
              </WizardField>
              <WizardField label="TTS API key" hint={inheritHint}>
                <input
                  type="password"
                  value={selection.voice_tts_api_key}
                  autoComplete="off"
                  onChange={(event) => onChange({ voice_tts_api_key: event.target.value })}
                />
              </WizardField>
            </>
          ) : (
            <WizardField label="Voice API key" hint={inheritHint}>
              <input
                type="password"
                value={selection.voice_api_key}
                autoComplete="off"
                onChange={(event) => onChange({ voice_api_key: event.target.value })}
              />
            </WizardField>
          )}

          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={selection.voice_wake_enabled}
              onChange={(event) => onChange({ voice_wake_enabled: event.target.checked })}
            />
            <span>Enable wake phrases</span>
          </label>

          {selection.voice_wake_enabled ? (
            <>
              <WizardField label="Wake phrases" hint="Comma-separated.">
                <input
                  value={selection.voice_wake_phrases.join(", ")}
                  onChange={(event) =>
                    onChange({
                      voice_wake_phrases: event.target.value
                        .split(",")
                        .map((part) => part.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </WizardField>
              <WizardField label="Exit phrases" hint="Comma-separated.">
                <input
                  value={selection.voice_exit_phrases.join(", ")}
                  onChange={(event) =>
                    onChange({
                      voice_exit_phrases: event.target.value
                        .split(",")
                        .map((part) => part.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </WizardField>
            </>
          ) : null}

          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={selection.voice_enable_interruptions}
              onChange={(event) => onChange({ voice_enable_interruptions: event.target.checked })}
            />
            <span>Allow interruptions</span>
          </label>
        </>
      ) : null}
    </div>
  );
}
