import { useEffect, useMemo, useState } from "react";
import { QrAuthPanel } from "./QrAuthPanel";
import { SetupWizardShell } from "./SetupWizardShell";
import { VoiceSetupFields } from "./VoiceSetupFields";
import { WizardField } from "./WizardField";
import { getChannelSetupSchema, setupChannel } from "../lib/api";
import type {
  FeishuSetupSchema,
  SetupChannelId,
  ChannelSetupSchema,
  VoiceSelectionInput,
  VoiceSetupSchema,
  WeixinSetupSchema,
} from "../types";

const CHANNEL_TITLES: Record<SetupChannelId, string> = {
  voice: "Voice setup",
  feishu: "Feishu setup",
  weixin: "Weixin setup",
};

interface ChannelSetupWizardProps {
  channel: SetupChannelId;
  open: boolean;
  onClose: () => void;
  onComplete: () => void;
}

function defaultVoiceSelection(schema: VoiceSetupSchema): VoiceSelectionInput {
  return {
    voice_provider: schema.defaults.voice_provider,
    voice_api_key: "",
    voice_stt_provider: schema.defaults.voice_stt_provider,
    voice_stt_api_key: "",
    voice_tts_provider: schema.defaults.voice_tts_provider,
    voice_tts_api_key: "",
    voice_enable_interruptions: schema.defaults.voice_enable_interruptions,
    voice_wake_enabled: schema.defaults.voice_wake_enabled,
    voice_wake_phrases: [...schema.defaults.wake_phrases],
    voice_exit_phrases: [...schema.defaults.exit_phrases],
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
  const [feishuSelection, setFeishuSelection] = useState({
    credential_mode: "one_click",
    app_id: "",
    app_secret: "",
    stream: false,
    group_fetch_limit: 10,
    group_reply_only_when_mentioned: false,
  });
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
    if (channel === "feishu") {
      if (feishuSelection.credential_mode === "one_click") {
        return [
          { id: "access", label: "App access" },
          { id: "qr", label: "Authorize" },
          { id: "behavior", label: "Behavior" },
        ];
      }
      return [
        { id: "access", label: "App access" },
        { id: "behavior", label: "Behavior" },
      ];
    }
    return [
      { id: "qr", label: "QR login" },
      { id: "access", label: "Access" },
    ];
  }, [channel, feishuSelection.credential_mode]);

  const currentStepId = steps[stepIndex]?.id ?? steps[0]?.id ?? "voice";
  const isDirty = useMemo(() => {
    if (!schema || loadingSchema || force || qrReady) return Boolean(force || qrReady);
    if (channel === "voice") {
      return JSON.stringify(voiceSelection) !== JSON.stringify(defaultVoiceSelection(schema as VoiceSetupSchema));
    }
    if (channel === "feishu") {
      const defaults = (schema as FeishuSetupSchema).defaults;
      return (
        feishuSelection.credential_mode !== defaults.credential_mode ||
        feishuSelection.app_id !== "" ||
        feishuSelection.app_secret !== "" ||
        feishuSelection.stream !== defaults.stream ||
        feishuSelection.group_fetch_limit !== defaults.group_fetch_limit ||
        feishuSelection.group_reply_only_when_mentioned !== defaults.group_reply_only_when_mentioned
      );
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
    feishuSelection,
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
        } else if (channel === "feishu") {
          const feishuSchema = data as FeishuSetupSchema;
          setFeishuSelection({
            credential_mode: feishuSchema.defaults.credential_mode,
            app_id: "",
            app_secret: "",
            stream: feishuSchema.defaults.stream,
            group_fetch_limit: feishuSchema.defaults.group_fetch_limit,
            group_reply_only_when_mentioned: feishuSchema.defaults.group_reply_only_when_mentioned,
          });
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
    if (channel === "voice" && voiceSelection) {
      if (!voiceSelection.voice_provider) return "Choose a voice provider.";
    }
    if (channel === "feishu") {
      if (currentStepId === "access" && feishuSelection.credential_mode === "manual") {
        if (!feishuSelection.app_id.trim()) return "Feishu App ID is required.";
        if (!feishuSelection.app_secret.trim()) return "Feishu App Secret is required.";
      }
      if (currentStepId === "qr" && !qrReady) {
        return "Complete Feishu authorization before continuing.";
      }
    }
    if (channel === "weixin") {
      if (currentStepId === "qr" && !qrReady) {
        return "Complete Weixin QR login before continuing.";
      }
    }
    if (stepIndex === steps.length - 1 && schema?.configured && !force) {
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
      return { ...voiceSelection };
    }
    if (channel === "feishu") {
      return {
        credential_mode: feishuSelection.credential_mode,
        app_id: feishuSelection.app_id.trim(),
        app_secret: feishuSelection.app_secret.trim(),
        stream: feishuSelection.stream,
        group_fetch_limit: feishuSelection.group_fetch_limit,
        group_reply_only_when_mentioned: feishuSelection.group_reply_only_when_mentioned,
      };
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
        force,
        selection: buildSelection(),
      });
      onComplete();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  };

  const handleFeishuQrConfirmed = (result: Record<string, unknown>) => {
    setFeishuSelection((current) => ({
      ...current,
      credential_mode: String(result.credential_mode || "one_click"),
      app_id: String(result.app_id || ""),
      app_secret: String(result.app_secret || ""),
    }));
    setQrReady(true);
    setError("");
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
      title={CHANNEL_TITLES[channel]}
      subtitle=""
      steps={steps}
      stepIndex={stepIndex}
      loading={loadingSchema}
      submitting={submitting}
      isDirty={isDirty}
      error={error}
      submitLabel="Save setup"
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

      {channel === "feishu" && currentStepId === "access" && schema ? (
        <div className="wizard-grid">
          <WizardField label="App access">
            <select
              value={feishuSelection.credential_mode}
              onChange={(event) => {
                setQrReady(false);
                setFeishuSelection((current) => ({
                  ...current,
                  credential_mode: event.target.value,
                }));
              }}
            >
              {(schema as FeishuSetupSchema).credential_modes.map((mode) => (
                <option key={mode.id} value={mode.id}>
                  {mode.label || mode.id}
                </option>
              ))}
            </select>
          </WizardField>
          {feishuSelection.credential_mode === "manual" ? (
            <>
              <WizardField label="Feishu App ID">
                <input
                  value={feishuSelection.app_id}
                  onChange={(event) =>
                    setFeishuSelection((current) => ({ ...current, app_id: event.target.value }))
                  }
                />
              </WizardField>
              <WizardField label="Feishu App Secret">
                <input
                  type="password"
                  value={feishuSelection.app_secret}
                  autoComplete="off"
                  onChange={(event) =>
                    setFeishuSelection((current) => ({ ...current, app_secret: event.target.value }))
                  }
                />
              </WizardField>
            </>
          ) : (
            <p className="wizard-hint">On the next step, authorize a new Feishu app via QR or browser link.</p>
          )}
        </div>
      ) : null}

      {channel === "feishu" && currentStepId === "qr" ? (
        <QrAuthPanel channel="feishu" onConfirmed={handleFeishuQrConfirmed} onError={setError} />
      ) : null}

      {channel === "feishu" && currentStepId === "behavior" ? (
        <div className="wizard-grid">
          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={feishuSelection.stream}
              onChange={(event) =>
                setFeishuSelection((current) => ({ ...current, stream: event.target.checked }))
              }
            />
            <span>Enable stream mode</span>
          </label>
          <WizardField label="Group fetch limit">
            <input
              type="number"
              min={0}
              value={feishuSelection.group_fetch_limit}
              onChange={(event) =>
                setFeishuSelection((current) => ({
                  ...current,
                  group_fetch_limit: Number(event.target.value) || 0,
                }))
              }
            />
          </WizardField>
          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={feishuSelection.group_reply_only_when_mentioned}
              onChange={(event) =>
                setFeishuSelection((current) => ({
                  ...current,
                  group_reply_only_when_mentioned: event.target.checked,
                }))
              }
            />
            <span>Reply in groups only when mentioned</span>
          </label>
        </div>
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

      {schema?.configured && stepIndex === steps.length - 1 ? (
        <label className="wizard-checkbox wizard-warning">
          <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
          <span>Overwrite existing {channel} channel settings</span>
        </label>
      ) : null}
    </SetupWizardShell>
  );
}
