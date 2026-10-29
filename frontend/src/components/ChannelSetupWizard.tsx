import { useEffect, useMemo, useState } from "react";
import { QrAuthPanel } from "./QrAuthPanel";
import { SetupWizardShell } from "./SetupWizardShell";
import { WizardField } from "./WizardField";
import { getChannelSetupSchema, setupChannel } from "../lib/api";
import type { WeixinSetupSchema } from "../types";

interface ChannelSetupWizardProps {
  channel: "weixin";
  open: boolean;
  onClose: () => void;
  onComplete: () => void;
}

export function ChannelSetupWizard({ channel, open, onClose, onComplete }: ChannelSetupWizardProps) {
  const [schema, setSchema] = useState<WeixinSetupSchema | null>(null);
  const [loadingSchema, setLoadingSchema] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [stepIndex, setStepIndex] = useState(0);
  const [force, setForce] = useState(false);
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
    return [
      { id: "qr", label: "QR login" },
      { id: "access", label: "Access" },
    ];
  }, [channel]);

  const currentStepId = steps[stepIndex]?.id ?? steps[0]?.id ?? "qr";
  const isDirty = useMemo(() => {
    if (!schema || loadingSchema || force || qrReady) return Boolean(force || qrReady);
    const defaults = schema.defaults;
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
    weixinSelection,
  ]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoadingSchema(true);
    setError("");
    setStepIndex(0);
    setForce(false);
    setQrReady(false);
    void getChannelSetupSchema(channel)
      .then((data) => {
        if (cancelled) return;
        const weixinSchema = data;
        setSchema(weixinSchema);
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
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => { if (!cancelled) setLoadingSchema(false); });
    return () => { cancelled = true; };
  }, [channel, open]);

  useEffect(() => {
    if (stepIndex >= steps.length) {
      setStepIndex(Math.max(steps.length - 1, 0));
    }
  }, [stepIndex, steps.length]);

  const validateStep = (): string => {
    if (!schema || loadingSchema) return "Load the setup options before saving.";
    if (currentStepId === "qr" && !qrReady) {
      return "Complete Weixin QR login before continuing.";
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
      await setupChannel(channel, { force, selection: buildSelection() });
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
      title="Weixin setup"
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
      {currentStepId === "qr" ? (
        <QrAuthPanel channel="weixin" onConfirmed={handleWeixinQrConfirmed} onError={setError} />
      ) : null}

      {currentStepId === "access" ? (
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
