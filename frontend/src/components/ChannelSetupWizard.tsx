import { useEffect, useMemo, useState } from "react";
import { QrAuthPanel } from "./QrAuthPanel";
import { SetupWizardShell } from "./SetupWizardShell";
import { VoiceSetupFields } from "./VoiceSetupFields";
import { WizardField } from "./WizardField";
import { getChannelSetupSchema, setupChannel } from "../lib/api";
import type {
  SetupChannelId,
  ChannelSetupSchema,
  VoiceSelectionInput,
  VoiceSetupSchema,
  WeixinSetupSchema,
} from "../types";

const CHANNEL_TITLES: Record<SetupChannelId, string> = {
  voice: "Voice setup",
  weixin: "Weixin setup",
};

interface ChannelSetupWizardProps {
  channel: SetupChannelId;
  open: boolean;
  onClose: () => void;
  onComplete: () => void;
}

function defaultVoiceSelection(schema: VoiceSetupSchema): VoiceSelectionInput {
  const defaults = schema.defaults;
  return {
    voice_api_key: defaults.voice_api_key,
    voice: defaults.voice,
    interruptions: defaults.interruptions,
    audio_input: String(defaults.audio_input ?? "auto"),
    audio_output: String(defaults.audio_output ?? "auto"),
  };
}

export function ChannelSetupWizard({ channel, open, onClose, onComplete }: ChannelSetupWizardProps) {
  const [schema, setSchema] = useState<ChannelSetupSchema | null>(null);
  const [loadingSchema, setLoadingSchema] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [stepIndex, setStepIndex] = useState(0);
  const [force, setForce] = useState(false);
  const [voiceSelection, setVoiceSelection] = useState<VoiceSelectionInput | null>(null);
  const [weixinSelection, setWeixinSelection] = useState({
    owner_only: true,
    allow_users: "",
    media_enabled: true,
    account_id: "",
    owner_user_id: "",
    base_url: "",
    cdn_base_url: "",
    credentials: null as Record<string, unknown> | null,
  });
  const [qrReady, setQrReady] = useState(false);

  const steps = useMemo(() => {
    if (channel === "voice") {
      return [{ id: "voice", label: "Voice" }];
    }
    return [
      { id: "qr", label: "QR login" },
      { id: "access", label: "Access" },
    ];
  }, [channel]);

  const currentStepId = steps[stepIndex]?.id ?? steps[0]?.id ?? "voice";
  const isDirty = useMemo(() => {
    if (!schema || loadingSchema || force || qrReady) return Boolean(force || qrReady);
    if (channel === "voice") {
      return JSON.stringify(voiceSelection) !== JSON.stringify(defaultVoiceSelection(schema as VoiceSetupSchema));
    }
    const defaults = (schema as WeixinSetupSchema).defaults;
    return (
      weixinSelection.owner_only !== defaults.owner_only ||
      weixinSelection.allow_users !== "" ||
      weixinSelection.media_enabled !== defaults.media_enabled ||
      weixinSelection.account_id !== "" ||
      weixinSelection.owner_user_id !== "" ||
      weixinSelection.base_url !== defaults.base_url ||
      weixinSelection.cdn_base_url !== defaults.cdn_base_url ||
      weixinSelection.credentials !== null
    );
  }, [
    channel,
    force,
    loadingSchema,
    qrReady,
    schema,
    voiceSelection,
    weixinSelection,
  ]);

  useEffect(() => {
    if (!open) return;
    setLoadingSchema(true);
    setError("");
    setStepIndex(0);
    setForce(false);
    setQrReady(false);
    void getChannelSetupSchema(channel)
      .then((data) => {
        setSchema(data);
        if (channel === "voice") {
          setVoiceSelection(defaultVoiceSelection(data as VoiceSetupSchema));
        } else {
          const weixinSchema = data as WeixinSetupSchema;
          setWeixinSelection({
            owner_only: weixinSchema.defaults.owner_only,
            allow_users: "",
            media_enabled: weixinSchema.defaults.media_enabled,
            account_id: "",
            owner_user_id: "",
            base_url: weixinSchema.defaults.base_url,
            cdn_base_url: weixinSchema.defaults.cdn_base_url,
            credentials: null,
          });
        }
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLoadingSchema(false));
  }, [channel, open]);

  useEffect(() => {
    if (stepIndex >= steps.length) {
      setStepIndex(Math.max(steps.length - 1, 0));
    }
  }, [stepIndex, steps.length]);

  const validateStep = (): string => {
    if (!schema || loadingSchema) return "Load the setup options before saving.";
    if (channel === "voice" && voiceSelection) {
      if (
        !voiceSelection.voice_api_key.trim() &&
        !(schema as VoiceSetupSchema | null)?.configured
      ) {
        return "Soniox API key is required.";
      }
    }
    if (channel === "weixin") {
      if (currentStepId === "qr" && !qrReady) {
        return "Complete Weixin QR login before continuing.";
      }
    }
    if (channel !== "voice" && stepIndex === steps.length - 1 && schema?.configured && !force) {
      return "Confirm overwriting the existing channel configuration.";
    }
    return "";
  };

  const goNext = () => {
    const message = validateStep();
    if (message) {
      setError(message);
      return;
    }
    setError("");
    setStepIndex((current) => Math.min(current + 1, steps.length - 1));
  };

  const goBack = () => {
    setError("");
    setStepIndex((current) => Math.max(current - 1, 0));
  };

  const buildSelection = (): Record<string, unknown> => {
    if (channel === "voice" && voiceSelection) {
      return { ...voiceSelection, voice_api_key: voiceSelection.voice_api_key.trim() };
    }
    return {
      account_id: weixinSelection.account_id,
      owner_user_id: weixinSelection.owner_user_id,
      base_url: weixinSelection.base_url,
      cdn_base_url: weixinSelection.cdn_base_url,
      owner_only: weixinSelection.owner_only,
      allow_users: weixinSelection.allow_users
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean),
      media_enabled: weixinSelection.media_enabled,
      ...(weixinSelection.credentials ? { credentials: weixinSelection.credentials } : {}),
    };
  };

  const submit = async () => {
    const message = validateStep();
    if (message) {
      setError(message);
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await setupChannel(channel, {
        force: channel === "voice" ? true : force,
        selection: buildSelection(),
      });
      onComplete();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  };

  const handleWeixinQrConfirmed = (result: Record<string, unknown>) => {
    setWeixinSelection((current) => ({
      ...current,
      account_id: String(result.account_id || ""),
      owner_user_id: String(result.owner_user_id || ""),
      base_url: String(result.base_url || current.base_url),
      cdn_base_url: String(result.cdn_base_url || current.cdn_base_url),
      credentials: (result.credentials as Record<string, unknown> | undefined) ?? null,
    }));
    setQrReady(true);
    setError("");
  };

  return (
    <SetupWizardShell
      open={open}
      title={channel === "voice" && schema?.configured ? "Voice settings" : CHANNEL_TITLES[channel]}
      subtitle=""
      steps={steps}
      stepIndex={stepIndex}
      loading={loadingSchema}
      submitting={submitting}
      isDirty={isDirty}
      error={error}
      submitLabel={channel === "voice" && schema?.configured ? "Save changes" : "Save setup"}
      onClose={onClose}
      onBack={goBack}
      onNext={goNext}
      onSubmit={submit}
    >
      {channel === "voice" && voiceSelection && schema ? (
        <VoiceSetupFields
          schema={schema as VoiceSetupSchema}
          selection={voiceSelection}
          onChange={(patch) => setVoiceSelection((current) => (current ? { ...current, ...patch } : current))}
        />
      ) : null}

      {channel === "weixin" && currentStepId === "qr" ? (
        <QrAuthPanel channel="weixin" onConfirmed={handleWeixinQrConfirmed} onError={setError} />
      ) : null}

      {channel === "weixin" && currentStepId === "access" ? (
        <div className="wizard-grid">
          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={weixinSelection.owner_only}
              onChange={(event) =>
                setWeixinSelection((current) => ({ ...current, owner_only: event.target.checked }))
              }
            />
            <span>Owner only</span>
          </label>
          {!weixinSelection.owner_only ? (
            <WizardField label="Allowed users" hint="Comma-separated user IDs.">
              <input
                value={weixinSelection.allow_users}
                onChange={(event) =>
                  setWeixinSelection((current) => ({ ...current, allow_users: event.target.value }))
                }
              />
            </WizardField>
          ) : null}
          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={weixinSelection.media_enabled}
              onChange={(event) =>
                setWeixinSelection((current) => ({ ...current, media_enabled: event.target.checked }))
              }
            />
            <span>Enable media</span>
          </label>
        </div>
      ) : null}

      {channel !== "voice" && schema?.configured && stepIndex === steps.length - 1 ? (
        <label className="wizard-checkbox wizard-warning">
          <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
          <span>Overwrite existing {channel} channel settings</span>
        </label>
      ) : null}
    </SetupWizardShell>
  );
}
