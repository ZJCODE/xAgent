import type { SetupOption, VoiceSelectionInput, VoiceSetupSchema } from "../types";
import { WizardField } from "./WizardField";

interface VoiceSetupFieldsProps {
  schema: VoiceSetupSchema;
  selection: VoiceSelectionInput;
  onChange: (patch: Partial<VoiceSelectionInput>) => void;
}

function optionsWithCurrent(options: SetupOption[], current: string): SetupOption[] {
  return options.some((option) => option.id === current)
    ? options
    : [...options, { id: current, label: `${current} (current/unavailable)` }];
}

export function VoiceSetupFields({ schema, selection, onChange }: VoiceSetupFieldsProps) {
  const input = String(selection.audio_input ?? "auto");
  const output = String(selection.audio_output ?? "auto");
  const autoOption = { id: "auto", label: "Auto (recommended)" };
  const voiceOptions = optionsWithCurrent(schema.voice_options, selection.voice);
  const inputOptions = optionsWithCurrent(
    schema.audio_devices.input.length ? schema.audio_devices.input : [autoOption],
    input,
  );
  const outputOptions = optionsWithCurrent(
    schema.audio_devices.output.length ? schema.audio_devices.output : [autoOption],
    output,
  );

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
      <WizardField label="Speaking voice">
        <select value={selection.voice} onChange={(event) => onChange({ voice: event.target.value })}>
          {voiceOptions.map((option) => (
            <option key={option.id} value={option.id}>{option.label || option.id}</option>
          ))}
        </select>
      </WizardField>
      <WizardField label="Allow interruptions" hint="Echo protection remains active when enabled.">
        <select
          value={String(selection.interruptions)}
          onChange={(event) => onChange({ interruptions: event.target.value === "true" })}
        >
          <option value="true">On</option>
          <option value="false">Off</option>
        </select>
      </WizardField>
      <WizardField label="Input device" hint={schema.audio_devices.error || "Choose the microphone used for speech input."}>
        <select value={input} onChange={(event) => onChange({ audio_input: event.target.value })}>
          {inputOptions.map((option) => (
            <option key={option.id} value={option.id}>{option.label || option.id}</option>
          ))}
        </select>
      </WizardField>
      <WizardField label="Output device" hint={schema.audio_devices.error || "Choose the speaker or headset used for voice playback."}>
        <select value={output} onChange={(event) => onChange({ audio_output: event.target.value })}>
          {outputOptions.map((option) => (
            <option key={option.id} value={option.id}>{option.label || option.id}</option>
          ))}
        </select>
      </WizardField>
    </div>
  );
}
