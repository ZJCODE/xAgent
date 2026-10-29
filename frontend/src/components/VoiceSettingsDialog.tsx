import { useEffect, useState } from "react";
import { getAgentVoiceSettings, getVoiceAudioDevices, updateAgentVoiceSettings } from "../lib/api";
import type { AgentEditSetupResponse, AgentVoiceSettings, SetupOption, VoiceAudioDevices, VoicePreferences } from "../types";
import { SetupWizardShell } from "./SetupWizardShell";
import { WizardField } from "./WizardField";
import { Button } from "./ui";

const SPEED_MIN = 0.7;
const SPEED_MAX = 1.3;
const SPEED_PRESETS = [0.7, 0.85, 1, 1.15, 1.3];

const deviceValue = (value: string | number | null) => String(value ?? "auto");
function clampSpeed(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(SPEED_MAX, Math.max(SPEED_MIN, value));
}
function deviceOptions(options: SetupOption[] | undefined, current: string): SetupOption[] {
  const available = options?.length ? options : [{ id: "auto", label: "Auto" }];
  return available.some((option) => option.id === current)
    ? available : [...available, { id: current, label: `${current} (saved)` }];
}

export function VoiceSettingsDialog({ onClose, onSaved }: {
  onClose: () => void;
  onSaved: (result: AgentEditSetupResponse) => void;
}) {
  const [settings, setSettings] = useState<AgentVoiceSettings | null>(null);
  const [voice, setVoice] = useState("");
  const [speed, setSpeed] = useState(1);
  const [interruptions, setInterruptions] = useState(false);
  const [audioInput, setAudioInput] = useState("auto");
  const [audioOutput, setAudioOutput] = useState("auto");
  const [apiKey, setApiKey] = useState("");
  const [changingKey, setChangingKey] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [devices, setDevices] = useState<VoiceAudioDevices | null>(null);
  const [loadingDevices, setLoadingDevices] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const dirty = Boolean(settings && (voice !== settings.voice || speed !== settings.speed
    || interruptions !== settings.interruptions || audioInput !== deviceValue(settings.audio_input)
    || audioOutput !== deviceValue(settings.audio_output) || apiKey.trim()));
  const speeds = [...new Set([...SPEED_PRESETS, clampSpeed(speed)])].sort((a, b) => a - b);

  useEffect(() => {
    let cancelled = false;
    void getAgentVoiceSettings().then((data) => {
      if (cancelled) return;
      setSettings(data);
      setVoice(data.voice);
      setSpeed(clampSpeed(data.speed));
      setInterruptions(data.interruptions);
      setAudioInput(deviceValue(data.audio_input));
      setAudioOutput(deviceValue(data.audio_output));
      setChangingKey(!data.has_api_key);
    }).catch((err: unknown) => {
      if (!cancelled) setError(err instanceof Error ? err.message : String(err));
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!moreOpen || devices) { setLoadingDevices(false); return; }
    let cancelled = false;
    setLoadingDevices(true);
    void getVoiceAudioDevices().then((data) => {
      if (!cancelled) setDevices(data);
    }).catch(() => {
      if (!cancelled) setDevices({ input: [], output: [], error: "unavailable" });
    }).finally(() => { if (!cancelled) setLoadingDevices(false); });
    return () => { cancelled = true; };
  }, [moreOpen, devices]);

  const save = async () => {
    if (!settings || saving || !dirty) return;
    if (!settings.has_api_key && !apiKey.trim()) {
      setError("Enter your Soniox API key to use voice.");
      return;
    }
    setSaving(true);
    setError("");
    const changes: Partial<VoicePreferences> & { voice_api_key?: string } = {};
    if (apiKey.trim()) changes.voice_api_key = apiKey.trim();
    if (voice !== settings.voice) changes.voice = voice;
    if (speed !== settings.speed) changes.speed = speed;
    if (interruptions !== settings.interruptions) changes.interruptions = interruptions;
    if (audioInput !== deviceValue(settings.audio_input)) changes.audio_input = audioInput;
    if (audioOutput !== deviceValue(settings.audio_output)) changes.audio_output = audioOutput;
    try {
      const result = await updateAgentVoiceSettings(changes);
      onSaved(result);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSaving(false);
    }
  };

  return (
    <SetupWizardShell open className="voice-settings-modal" title="Voice" subtitle="One voice for all conversations."
      steps={[{ id: "voice", label: "Voice" }]} stepIndex={0}
      loading={loading} submitting={saving} isDirty={dirty} error={error}
      submitLabel="Save" submitDisabled={!settings || !dirty}
      onClose={onClose} onBack={() => {}} onNext={() => {}} onSubmit={() => void save()}
    >
      {settings ? <div className="voice-settings-fields">
        {changingKey ? (
          <WizardField label="Soniox API key" hint={settings.has_api_key ? "Leave blank to keep the current key." : "You only need to set this once."}>
            <input type="password" value={apiKey} autoComplete="off" spellCheck={false}
              onChange={(event) => setApiKey(event.target.value)} disabled={saving} />
          </WizardField>
        ) : (
          <div className="voice-service-status">
            <span>Soniox key configured</span>
            <Button type="button" variant="ghost" onClick={() => setChangingKey(true)}>Change key</Button>
          </div>
        )}
        <div className="voice-preference-row">
          <WizardField label="Voice">
            <select value={voice} onChange={(event) => setVoice(event.target.value)} disabled={saving}>
              {settings.voice_options.map((option) => <option key={option.id} value={option.id}>{option.label || option.id}</option>)}
            </select>
          </WizardField>
          <WizardField label="Speed">
            <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))} disabled={saving}>
              {speeds.map((value) => <option key={value} value={value}>{value}×</option>)}
            </select>
          </WizardField>
        </div>
        <details className="voice-more-options" onToggle={(event) => setMoreOpen(event.currentTarget.open)}>
          <summary>More options</summary>
          <div className="voice-more-fields">
            <label className="voice-interruptions-row">
              <span>Allow interruptions</span>
              <input type="checkbox" role="switch" checked={interruptions} disabled={saving}
                onChange={(event) => setInterruptions(event.target.checked)} />
            </label>
            <div className="voice-host-devices">
              <p className="wizard-hint">Audio devices on the Agent’s computer</p>
              <div className="voice-device-row">
                <WizardField label="Microphone">
                  <select value={audioInput} onChange={(event) => setAudioInput(event.target.value)} disabled={saving || loadingDevices}>
                    {deviceOptions(devices?.input, audioInput).map((option) => <option key={option.id} value={option.id}>{option.label || option.id}</option>)}
                  </select>
                </WizardField>
                <WizardField label="Speaker">
                  <select value={audioOutput} onChange={(event) => setAudioOutput(event.target.value)} disabled={saving || loadingDevices}>
                    {deviceOptions(devices?.output, audioOutput).map((option) => <option key={option.id} value={option.id}>{option.label || option.id}</option>)}
                  </select>
                </WizardField>
              </div>
              {devices?.error ? <p className="wizard-hint">Device list unavailable. Auto and saved selections remain available.</p> : null}
            </div>
          </div>
        </details>
      </div> : null}
    </SetupWizardShell>
  );
}
