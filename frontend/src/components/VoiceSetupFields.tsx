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
      <WizardField label="Soniox API key" hint={schema.configured ? "Leave blank to keep the existing key." : undefined}>
        <input
          type="password"
          value={selection.voice_api_key}
          placeholder={schema.placeholders.soniox_api_key}
          autoComplete="off"
          onChange={(event) => onChange({ voice_api_key: event.target.value })}
        />
      </WizardField>
    </div>
  );
}
