import { useEffect, useMemo, useState } from "react";
import { applyAgentEditSetup } from "../lib/api";
import type {
  AgentEditSetupFeatureId,
  AgentEditSetupResponse,
  AgentEditSetupSchema,
  ReasoningCapability,
  ReasoningConfigInput,
} from "../types";
import { SetupWizardShell } from "./SetupWizardShell";
import { WizardField } from "./WizardField";

interface FeatureSetupModalProps {
  open: boolean;
  feature: AgentEditSetupFeatureId;
  schema: AgentEditSetupSchema;
  onClose: () => void;
  onSaved: (result: AgentEditSetupResponse) => void;
}

function reasoningCapabilityFor(
  schema: AgentEditSetupSchema,
  provider: string,
  modelApi: string,
): ReasoningCapability | null {
  if (provider === "custom") {
    return schema.model.reasoning.custom_model_apis[modelApi] || null;
  }
  return schema.model.reasoning.providers[provider] || null;
}

function buildCustomReasoning(capability: ReasoningCapability): ReasoningConfigInput {
  if (capability.controls.includes("effort")) {
    const effort = capability.effort_values.includes("medium")
      ? "medium"
      : capability.effort_values[0];
    return { enabled: true, effort };
  }
  return {
    enabled: true,
    budget_tokens: Math.max(capability.min_budget_tokens || 1, 4096),
  };
}

function needsFeatureKey(modelProvider: string, featureProvider: string) {
  return featureProvider !== "none" && featureProvider !== modelProvider;
}

export function FeatureSetupModal({ open, feature, schema, onClose, onSaved }: FeatureSetupModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [provider, setProvider] = useState("none");
  const [apiKey, setApiKey] = useState("");
  const [modelName, setModelName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [modelApi, setModelApi] = useState("");
  const [supportsVision, setSupportsVision] = useState(false);
  const [reasoning, setReasoning] = useState<ReasoningConfigInput | null>(null);
  const [observabilityEnabled, setObservabilityEnabled] = useState(false);
  const [publicKey, setPublicKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [langfuseBaseUrl, setLangfuseBaseUrl] = useState("");

  useEffect(() => {
    if (!open) return;
    setError("");
    setSubmitting(false);
    setApiKey("");
    if (feature === "search") {
      setProvider(schema.search.current.provider || "none");
    } else if (feature === "image") {
      setProvider(schema.image.current.provider || "none");
    } else if (feature === "observability") {
      setObservabilityEnabled(schema.observability.current.enabled);
      setPublicKey("");
      setSecretKey("");
      setLangfuseBaseUrl(schema.observability.current.base_url || schema.observability.placeholders.base_url);
    } else {
      setProvider(schema.model.current.provider || "openai");
      setModelName(schema.model.current.model || "");
      setBaseUrl(schema.model.current.base_url || "");
      setModelApi(schema.model.current.model_api || schema.model.custom_model_apis[0] || "");
      setSupportsVision(schema.model.current.supports_vision);
      setReasoning(schema.model.current.reasoning || null);
    }
  }, [open, feature, schema]);

  const title =
    feature === "search"
      ? "Edit Search"
      : feature === "image"
        ? "Edit Image generation"
        : feature === "observability"
          ? "Edit Observability"
          : "Edit Model";

  const models = schema.model.models[provider] || [];
  const capability = useMemo(
    () => (feature === "model" ? reasoningCapabilityFor(schema, provider, modelApi) : null),
    [feature, schema, provider, modelApi],
  );
  const reasoningMode = reasoning?.enabled ? "custom" : "default";
  const reasoningControl = reasoning?.budget_tokens !== undefined ? "budget_tokens" : "effort";
  const showFeatureKey =
    (feature === "search" || feature === "image") && needsFeatureKey(schema.model_provider, provider);

  const isDirty = useMemo(() => {
    if (feature === "search") return provider !== schema.search.current.provider || Boolean(apiKey.trim());
    if (feature === "image") return provider !== schema.image.current.provider || Boolean(apiKey.trim());
    if (feature === "observability") {
      return (
        observabilityEnabled !== schema.observability.current.enabled ||
        Boolean(publicKey.trim()) ||
        Boolean(secretKey.trim()) ||
        langfuseBaseUrl !== schema.observability.current.base_url
      );
    }
    return (
      provider !== schema.model.current.provider ||
      modelName !== schema.model.current.model ||
      baseUrl !== schema.model.current.base_url ||
      modelApi !== (schema.model.current.model_api || schema.model.custom_model_apis[0] || "") ||
      supportsVision !== schema.model.current.supports_vision ||
      Boolean(apiKey.trim()) ||
      JSON.stringify(reasoning || null) !== JSON.stringify(schema.model.current.reasoning || null)
    );
  }, [
    feature,
    schema,
    provider,
    apiKey,
    observabilityEnabled,
    publicKey,
    secretKey,
    langfuseBaseUrl,
    modelName,
    baseUrl,
    modelApi,
    supportsVision,
    reasoning,
  ]);

  const onProviderChange = (next: string) => {
    setProvider(next);
    if (feature !== "model") return;
    const nextModels = schema.model.models[next] || [];
    setModelName(nextModels[0] || schema.model.placeholders.model || "");
    setBaseUrl(schema.model.provider_base_urls[next] || "");
    setModelApi(next === "custom" ? schema.model.custom_model_apis[0] || "" : "");
    setSupportsVision(false);
    setReasoning(null);
    setApiKey("");
  };

  const submit = async () => {
    setSubmitting(true);
    setError("");
    try {
      let selection: Record<string, unknown> = {};
      if (feature === "search" || feature === "image") {
        selection = {
          provider,
          api_key: apiKey.trim() || undefined,
        };
      } else if (feature === "observability") {
        selection = {
          enabled: observabilityEnabled,
          public_key: publicKey.trim() || undefined,
          secret_key: secretKey.trim() || undefined,
          base_url: langfuseBaseUrl.trim() || undefined,
        };
      } else {
        if (!modelName.trim()) {
          setError("Choose a model.");
          setSubmitting(false);
          return;
        }
        if (provider === "custom" && !baseUrl.trim()) {
          setError("Enter a custom provider base URL.");
          setSubmitting(false);
          return;
        }
        selection = {
          provider,
          model: modelName.trim(),
          api_key: apiKey.trim() || undefined,
          base_url: provider === "custom" ? baseUrl.trim() : undefined,
          model_api: provider === "custom" ? modelApi : undefined,
          supports_vision: provider === "custom" ? supportsVision : undefined,
          reasoning,
        };
      }
      const result = await applyAgentEditSetup(feature, selection);
      onSaved(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  };

  return (
    <SetupWizardShell
      open={open}
      title={title}
      subtitle="Aligned with CLI Edit Setup. Leave API keys blank to keep current values."
      steps={[{ id: feature, label: title.replace(/^Edit /, "") }]}
      stepIndex={0}
      submitting={submitting}
      isDirty={isDirty}
      error={error}
      submitLabel="Save"
      onClose={onClose}
      onBack={onClose}
      onNext={() => undefined}
      onSubmit={() => void submit()}
    >
      {feature === "search" || feature === "image" ? (
        <div className="wizard-grid">
          <WizardField label={feature === "search" ? "Search provider" : "Image generation provider"}>
            <select value={provider} onChange={(event) => setProvider(event.target.value)}>
              {(feature === "search" ? schema.search.providers : schema.image.providers).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.id}
                </option>
              ))}
            </select>
          </WizardField>
          {feature === "image" && provider !== schema.image.current.provider && provider !== "none" ? (
            <p className="wizard-hint">Switching providers resets image defaults (model/size/quality).</p>
          ) : null}
          {showFeatureKey ? (
            <WizardField
              label="API key"
              hint={
                (feature === "search" ? schema.search.current.has_api_key : schema.image.current.has_api_key)
                  ? "Leave blank to keep the current key."
                  : "Required because this differs from the model provider."
              }
            >
              <input
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
              />
            </WizardField>
          ) : provider !== "none" ? (
            <p className="wizard-hint">Uses the main model API key for this provider.</p>
          ) : null}
        </div>
      ) : null}

      {feature === "observability" ? (
        <div className="wizard-grid">
          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={observabilityEnabled}
              onChange={(event) => setObservabilityEnabled(event.target.checked)}
            />
            <span>Enable Langfuse observability</span>
          </label>
          {observabilityEnabled ? (
            <>
              <WizardField
                label="Langfuse public key"
                hint={schema.observability.current.has_public_key ? "Leave blank to keep current." : undefined}
              >
                <input value={publicKey} onChange={(event) => setPublicKey(event.target.value)} />
              </WizardField>
              <WizardField
                label="Langfuse secret key"
                hint={schema.observability.current.has_secret_key ? "Leave blank to keep current." : undefined}
              >
                <input
                  type="password"
                  autoComplete="off"
                  value={secretKey}
                  onChange={(event) => setSecretKey(event.target.value)}
                />
              </WizardField>
              <WizardField label="Langfuse base URL">
                <input value={langfuseBaseUrl} onChange={(event) => setLangfuseBaseUrl(event.target.value)} />
              </WizardField>
            </>
          ) : (
            <p className="wizard-hint">Disable keeps saved keys and turns tracing off.</p>
          )}
        </div>
      ) : null}

      {feature === "model" ? (
        <div className="wizard-grid">
          <WizardField label="Provider">
            <select value={provider} onChange={(event) => onProviderChange(event.target.value)}>
              {schema.model.providers.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label || item.id}
                </option>
              ))}
            </select>
          </WizardField>
          {provider !== "custom" ? (
            <WizardField label="Model">
              <select value={modelName} onChange={(event) => setModelName(event.target.value)}>
                {models.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            </WizardField>
          ) : (
            <>
              <WizardField label="Model API">
                <select
                  value={modelApi}
                  onChange={(event) => {
                    setModelApi(event.target.value);
                    setReasoning(null);
                  }}
                >
                  {schema.model.custom_model_apis.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </WizardField>
              <WizardField label="Base URL">
                <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
              </WizardField>
              <WizardField label="Model name">
                <input
                  value={modelName}
                  placeholder={schema.model.placeholders.model}
                  onChange={(event) => setModelName(event.target.value)}
                />
              </WizardField>
              <label className="wizard-checkbox">
                <input
                  type="checkbox"
                  checked={supportsVision}
                  onChange={(event) => setSupportsVision(event.target.checked)}
                />
                <span>Provider supports image URL input</span>
              </label>
            </>
          )}
          <WizardField
            label="API key"
            hint={schema.model.current.has_api_key ? "Leave blank to keep the current key." : "Leave blank to fill in later."}
          >
            <input
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
            />
          </WizardField>
          {capability?.supported ? (
            <>
              <WizardField label="Reasoning mode">
                <select
                  value={reasoningMode}
                  onChange={(event) => {
                    const mode = event.target.value;
                    if (mode === "default") setReasoning(null);
                    else setReasoning(buildCustomReasoning(capability));
                  }}
                >
                  <option value="default">Default</option>
                  <option value="custom">Custom</option>
                </select>
              </WizardField>
              {reasoningMode === "custom" && capability.controls.length > 1 ? (
                <WizardField label="Reasoning strength control">
                  <select
                    value={reasoningControl}
                    onChange={(event) => {
                      if (event.target.value === "budget_tokens") {
                        setReasoning({
                          enabled: true,
                          budget_tokens: Math.max(capability.min_budget_tokens || 1, 4096),
                        });
                      } else {
                        setReasoning(buildCustomReasoning(capability));
                      }
                    }}
                  >
                    {capability.controls.map((control) => (
                      <option key={control} value={control}>
                        {control === "effort" ? "Adaptive effort" : "Token budget"}
                      </option>
                    ))}
                  </select>
                </WizardField>
              ) : null}
              {reasoningMode === "custom" && reasoningControl === "effort" ? (
                <WizardField label="Reasoning effort">
                  <select
                    value={reasoning?.effort || capability.effort_values[0] || ""}
                    onChange={(event) => setReasoning({ enabled: true, effort: event.target.value })}
                  >
                    {capability.effort_values.map((effort) => (
                      <option key={effort} value={effort}>
                        {effort}
                      </option>
                    ))}
                  </select>
                </WizardField>
              ) : null}
              {reasoningMode === "custom" && reasoningControl === "budget_tokens" ? (
                <WizardField label="Reasoning token budget">
                  <input
                    type="number"
                    min={capability.min_budget_tokens || 1}
                    value={reasoning?.budget_tokens || ""}
                    onChange={(event) =>
                      setReasoning({ enabled: true, budget_tokens: Number(event.target.value) })
                    }
                  />
                </WizardField>
              ) : null}
            </>
          ) : (
            <p className="wizard-hint">This provider does not expose configurable reasoning controls.</p>
          )}
        </div>
      ) : null}
    </SetupWizardShell>
  );
}
