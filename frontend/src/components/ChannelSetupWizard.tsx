import { useEffect, useMemo, useRef, useState } from "react";
import { QrAuthPanel } from "./QrAuthPanel";
import { SetupWizardShell } from "./SetupWizardShell";
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
  const submittingRef = useRef(false);
  const schemaReadyRef = useRef(false);
  const [weixinSelection, setWeixinSelection] = useState({
    account_id: "",
    owner_user_id: "",
    base_url: "",
    cdn_base_url: "",
    credentials: null as Record<string, unknown> | null,
  });
  const [qrReady, setQrReady] = useState(false);

  const steps = [{ id: "qr", label: "QR login" }];
  const isDirty = useMemo(() => {
    if (!schema || loadingSchema || qrReady) return qrReady;
    const defaults = schema.defaults;
    return (
      weixinSelection.account_id !== "" ||
      weixinSelection.owner_user_id !== "" ||
      weixinSelection.base_url !== defaults.base_url ||
      weixinSelection.cdn_base_url !== defaults.cdn_base_url ||
      weixinSelection.credentials !== null
    );
  }, [
    channel,
    loadingSchema,
    qrReady,
    schema,
    weixinSelection,
  ]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    schemaReadyRef.current = false;
    setLoadingSchema(true);
    setError("");
    setQrReady(false);
    void getChannelSetupSchema(channel)
      .then((data) => {
        if (cancelled) return;
        schemaReadyRef.current = true;
        const weixinSchema = data;
        setSchema(weixinSchema);
        setWeixinSelection({
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
    return () => { cancelled = true; schemaReadyRef.current = false; };
  }, [channel, open]);

  const validateStep = (): string => {
    if (!schema || loadingSchema) return "Load the setup options before saving.";
    if (!qrReady) {
      return "Complete Weixin QR login before saving.";
    }
    return "";
  };

  const buildSelection = (): Record<string, unknown> => {
    return {
      account_id: weixinSelection.account_id,
      owner_user_id: weixinSelection.owner_user_id,
      base_url: weixinSelection.base_url,
      cdn_base_url: weixinSelection.cdn_base_url,
      owner_only: true,
      allow_users: [],
      media_enabled: true,
      ...(weixinSelection.credentials ? { credentials: weixinSelection.credentials } : {}),
    };
  };

  const submit = async () => {
    if (submittingRef.current) return;
    const message = validateStep();
    if (message) {
      setError(message);
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    setError("");
    try {
      await setupChannel(channel, { force: true, selection: buildSelection() });
      onComplete();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    } finally {
      submittingRef.current = false;
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

  useEffect(() => {
    if (open && schemaReadyRef.current && qrReady && schema && !loadingSchema) {
      void submit();
    }
  }, [open, qrReady, schema, loadingSchema]);

  return (
    <SetupWizardShell
      open={open}
      title="Weixin setup"
      subtitle=""
      steps={steps}
      stepIndex={0}
      loading={loadingSchema}
      submitting={submitting}
      isDirty={isDirty}
      error={error}
      submitLabel="Save setup"
      submitDisabled={!qrReady}
      onClose={onClose}
      onBack={() => {}}
      onNext={() => {}}
      onSubmit={submit}
    >
      <QrAuthPanel channel="weixin" onConfirmed={handleWeixinQrConfirmed} onError={setError} />

    </SetupWizardShell>
  );
}
