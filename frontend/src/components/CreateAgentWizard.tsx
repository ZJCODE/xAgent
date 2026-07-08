import { useEffect, useMemo, useReducer, useState } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { getAgentNameAvailability, getAgentSetupSchema } from "../lib/api";
import { classNames } from "../lib/format";
import type { AgentSetupSchema, CreateAgentInput, InitSelectionInput } from "../types";
import { VoiceSetupFields } from "./VoiceSetupFields";
import { WizardField } from "./WizardField";
import { Button } from "./ui";

const NAME_PATTERN = /^[a-z][a-z0-9_-]*$/;

const STEP_ORDER = ["basics", "provider", "tools", "observability", "voice", "identity"] as const;
type StepId = (typeof STEP_ORDER)[number];

const STEP_LABELS: Record<StepId, string> = {
  basics: "Basics",
  provider: "Provider",
  tools: "Tools",
  observability: "Observability",
  voice: "Voice",
  identity: "Identity",
};

interface CreateAgentWizardProps {
  open: boolean;
  onClose: () => void;
}

interface WizardState {
  stepIndex: number;
  name: string;
  replaceExisting: boolean;
  selection: InitSelectionInput;
}

type WizardAction =
  | { type: "reset"; schema: AgentSetupSchema }
  | { type: "set-step-index"; stepIndex: number }
  | { type: "patch"; patch: Partial<WizardState> }
  | { type: "patch-selection"; patch: Partial<InitSelectionInput> };

function defaultSelection(schema: AgentSetupSchema): InitSelectionInput {
  const provider = schema.providers[0]?.id || "openai";
  const models = schema.models[provider] || [];
  const model = models[1] || models[0] || schema.placeholders.model;
  return {
    provider,
    base_url: schema.provider_base_urls[provider] || "",
    api_key: "",
    model,
    identity: schema.defaults.identity,
    model_api: schema.custom_model_apis[0] || "",
    supports_vision: false,
    search_provider: "none",
    search_api_key: "",
    image_generation_provider: "none",
    image_generation_api_key: "",
    observability_enabled: false,
    langfuse_public_key: "",
    langfuse_secret_key: "",
    langfuse_base_url: schema.placeholders.langfuse_base_url || "",
    voice_enabled: false,
    voice_provider: "none",
    voice_api_key: "",
    voice_stt_provider: schema.voice_custom_providers[0] || "soniox",
    voice_stt_api_key: "",
    voice_tts_provider: schema.voice_custom_providers[0] || "soniox",
    voice_tts_api_key: "",
    voice_enable_interruptions: false,
    voice_wake_enabled: false,
    voice_wake_phrases: [...schema.defaults.wake_phrases],
    voice_exit_phrases: [...schema.defaults.exit_phrases],
  };
}

function wizardReducer(state: WizardState, action: WizardAction): WizardState {
  if (action.type === "reset") {
    return {
      stepIndex: 0,
      name: "",
      replaceExisting: false,
      selection: defaultSelection(action.schema),
    };
  }
  if (action.type === "set-step-index") {
    return { ...state, stepIndex: action.stepIndex };
  }
  if (action.type === "patch") {
    return { ...state, ...action.patch };
  }
  if (action.type === "patch-selection") {
    return { ...state, selection: { ...state.selection, ...action.patch } };
  }
  return state;
}

function supportsLangfuse(provider: string, modelApi: string) {
  if (provider === "openai" || provider === "deepseek" || provider === "qwen") return true;
  if (provider === "custom") {
    return modelApi === "openai_chat_completions" || modelApi === "openai_responses";
  }
  return false;
}

function visibleSteps(selection: InitSelectionInput): StepId[] {
  return STEP_ORDER.filter(
    (id) => id !== "observability" || supportsLangfuse(selection.provider, selection.model_api),
  );
}

export function CreateAgentWizard({ open, onClose }: CreateAgentWizardProps) {
  const { agents, createAgent } = useAgentSession();
  const [schema, setSchema] = useState<AgentSetupSchema | null>(null);
  const [loadingSchema, setLoadingSchema] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [directoryExists, setDirectoryExists] = useState(false);
  const [state, dispatch] = useReducer(wizardReducer, {
    stepIndex: 0,
    name: "",
    replaceExisting: false,
    selection: defaultSelection({
      providers: [{ id: "openai" }],
      models: { openai: ["gpt-5.4-mini"] },
      provider_base_urls: { openai: "" },
      custom_model_apis: [],
      search_providers: [],
      image_generation_providers: [],
      voice_providers: [],
      voice_custom_providers: [],
      defaults: { identity: "", wake_phrases: [], exit_phrases: [] },
      placeholders: {},
      name_pattern: NAME_PATTERN.source,
    }),
  });

  const steps = useMemo(() => visibleSteps(state.selection), [state.selection]);
  const currentStepId = steps[state.stepIndex] ?? steps[0] ?? "basics";
  const isLastStep = state.stepIndex >= steps.length - 1;

  useEffect(() => {
    if (!open) return;
    setLoadingSchema(true);
    setError("");
    void getAgentSetupSchema()
      .then((data) => {
        setSchema(data);
        dispatch({ type: "reset", schema: data });
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLoadingSchema(false));
  }, [open]);

  useEffect(() => {
    if (state.stepIndex >= steps.length) {
      dispatch({ type: "set-step-index", stepIndex: Math.max(steps.length - 1, 0) });
    }
  }, [state.stepIndex, steps.length]);

  useEffect(() => {
    if (!open || currentStepId !== "identity" || !state.name.trim()) {
      setDirectoryExists(false);
      return;
    }
    void getAgentNameAvailability(state.name.trim())
      .then((result) => setDirectoryExists(result.directory_exists))
      .catch(() => setDirectoryExists(false));
  }, [open, currentStepId, state.name]);

  const models = useMemo(
    () => schema?.models[state.selection.provider] || [],
    [schema, state.selection.provider],
  );

  const close = () => {
    if (submitting) return;
    onClose();
  };

  const validateStep = (stepId: StepId): string => {
    if (stepId === "basics") {
      const name = state.name.trim();
      if (!NAME_PATTERN.test(name)) {
        return "Name must start with a lowercase letter and use only lowercase letters, digits, hyphens, or underscores.";
      }
      if (agents.some((agent) => agent.name === name)) {
        return `Agent ${name} is already registered.`;
      }
    }
    if (stepId === "provider") {
      if (!state.selection.provider) return "Choose a provider.";
      if (!state.selection.model.trim()) return "Choose a model.";
      if (state.selection.provider === "custom" && !state.selection.base_url.trim()) {
        return "Enter a custom provider base URL.";
      }
    }
    if (stepId === "identity") {
      if (!state.selection.identity.trim()) return "Identity cannot be empty.";
      if (directoryExists && !state.replaceExisting) {
        return "Confirm replacing the existing directory on the review step.";
      }
    }
    return "";
  };

  const goNext = () => {
    const message = validateStep(currentStepId);
    if (message) {
      setError(message);
      return;
    }
    setError("");
    dispatch({ type: "set-step-index", stepIndex: Math.min(state.stepIndex + 1, steps.length - 1) });
  };

  const goBack = () => {
    setError("");
    dispatch({ type: "set-step-index", stepIndex: Math.max(state.stepIndex - 1, 0) });
  };

  const submit = async () => {
    const message = validateStep("identity");
    if (message) {
      setError(message);
      return;
    }
    if (!schema) return;

    setSubmitting(true);
    setError("");
    try {
      const payload: CreateAgentInput = {
        name: state.name.trim(),
        replace_existing: state.replaceExisting,
        selection: {
          ...state.selection,
          api_key: state.selection.api_key.trim() || schema.placeholders.api_key,
          langfuse_public_key:
            state.selection.langfuse_public_key.trim() || schema.placeholders.langfuse_public_key,
          langfuse_secret_key:
            state.selection.langfuse_secret_key.trim() || schema.placeholders.langfuse_secret_key,
          langfuse_base_url:
            state.selection.langfuse_base_url.trim() || schema.placeholders.langfuse_base_url,
        },
      };
      await createAgent(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  };

  const onProviderChange = (provider: string) => {
    if (!schema) return;
    const nextModels = schema.models[provider] || [];
    const nextModelApi =
      provider === "custom" ? state.selection.model_api || schema.custom_model_apis[0] || "" : "";
    dispatch({
      type: "patch-selection",
      patch: {
        provider,
        base_url: schema.provider_base_urls[provider] || "",
        model: nextModels[1] || nextModels[0] || schema.placeholders.model,
        model_api: nextModelApi,
      },
    });
  };

  const onModelApiChange = (modelApi: string) => {
    dispatch({ type: "patch-selection", patch: { model_api: modelApi } });
    const nextSteps = visibleSteps({ ...state.selection, model_api: modelApi });
    if (currentStepId === "observability" && !nextSteps.includes("observability")) {
      const voiceIndex = nextSteps.indexOf("voice");
      if (voiceIndex >= 0) {
        dispatch({ type: "set-step-index", stepIndex: voiceIndex });
      }
    }
  };

  if (!open) return null;

  return (
    <div className="modal-overlay" role="presentation" onClick={close}>
      <div
        className="modal-card wizard-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-agent-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="wizard-header-block">
          <div>
            <h3 id="create-agent-title">Create agent</h3>
            <p className="wizard-subtitle">
              Step {state.stepIndex + 1} of {steps.length}: {STEP_LABELS[currentStepId]}
            </p>
          </div>
          <div className="wizard-steps" aria-hidden="true">
            {steps.map((stepId, index) => (
              <span
                key={stepId}
                className={classNames(
                  "wizard-step-chip",
                  index === state.stepIndex && "active",
                  index < state.stepIndex && "done",
                )}
              >
                {STEP_LABELS[stepId]}
              </span>
            ))}
          </div>
        </div>

        <div className="modal-body wizard-body">
          {loadingSchema ? <p>Loading setup options...</p> : null}
          {error ? <div className="error-strip">{error}</div> : null}

          {!loadingSchema && currentStepId === "basics" ? (
            <div className="wizard-grid wizard-grid-narrow">
              <WizardField label="Agent name" hint="Lowercase letters, digits, hyphens, or underscores.">
                <input
                  value={state.name}
                  placeholder="work-agent"
                  onChange={(event) => dispatch({ type: "patch", patch: { name: event.target.value } })}
                />
              </WizardField>
            </div>
          ) : null}

          {!loadingSchema && currentStepId === "provider" && schema ? (
            <div className="wizard-grid">
              <WizardField label="Provider">
                <select
                  value={state.selection.provider}
                  onChange={(event) => onProviderChange(event.target.value)}
                >
                  {schema.providers.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.label || provider.id}
                    </option>
                  ))}
                </select>
              </WizardField>
              {state.selection.provider !== "custom" ? (
                <WizardField label="Model">
                  <select
                    value={state.selection.model}
                    onChange={(event) =>
                      dispatch({ type: "patch-selection", patch: { model: event.target.value } })
                    }
                  >
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
                    <select value={state.selection.model_api} onChange={(event) => onModelApiChange(event.target.value)}>
                      {schema.custom_model_apis.map((modelApi) => (
                        <option key={modelApi} value={modelApi}>
                          {modelApi}
                        </option>
                      ))}
                    </select>
                  </WizardField>
                  <WizardField label="Base URL">
                    <input
                      value={state.selection.base_url}
                      onChange={(event) =>
                        dispatch({ type: "patch-selection", patch: { base_url: event.target.value } })
                      }
                    />
                  </WizardField>
                  <WizardField label="Model name">
                    <input
                      value={state.selection.model}
                      placeholder={schema.placeholders.model}
                      onChange={(event) =>
                        dispatch({ type: "patch-selection", patch: { model: event.target.value } })
                      }
                    />
                  </WizardField>
                  <label className="wizard-checkbox">
                    <input
                      type="checkbox"
                      checked={state.selection.supports_vision}
                      onChange={(event) =>
                        dispatch({
                          type: "patch-selection",
                          patch: { supports_vision: event.target.checked },
                        })
                      }
                    />
                    <span>Provider supports image URL input</span>
                  </label>
                </>
              )}
              <WizardField label="API key" hint="Leave blank to fill in later on the Agent page.">
                <input
                  type="password"
                  value={state.selection.api_key}
                  autoComplete="off"
                  onChange={(event) =>
                    dispatch({ type: "patch-selection", patch: { api_key: event.target.value } })
                  }
                />
              </WizardField>
            </div>
          ) : null}

          {!loadingSchema && currentStepId === "tools" && schema ? (
            <div className="wizard-grid">
              <WizardField label="Search provider">
                <select
                  value={state.selection.search_provider}
                  onChange={(event) =>
                    dispatch({ type: "patch-selection", patch: { search_provider: event.target.value } })
                  }
                >
                  {schema.search_providers.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.id}
                    </option>
                  ))}
                </select>
              </WizardField>
              {state.selection.search_provider !== "none" &&
              state.selection.search_provider !== state.selection.provider ? (
                <WizardField label="Search API key">
                  <input
                    type="password"
                    value={state.selection.search_api_key}
                    autoComplete="off"
                    onChange={(event) =>
                      dispatch({ type: "patch-selection", patch: { search_api_key: event.target.value } })
                    }
                  />
                </WizardField>
              ) : null}
              <WizardField label="Image generation">
                <select
                  value={state.selection.image_generation_provider}
                  onChange={(event) =>
                    dispatch({
                      type: "patch-selection",
                      patch: { image_generation_provider: event.target.value },
                    })
                  }
                >
                  {schema.image_generation_providers.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.id}
                    </option>
                  ))}
                </select>
              </WizardField>
              {state.selection.image_generation_provider !== "none" &&
              state.selection.image_generation_provider !== state.selection.provider ? (
                <WizardField label="Image generation API key">
                  <input
                    type="password"
                    value={state.selection.image_generation_api_key}
                    autoComplete="off"
                    onChange={(event) =>
                      dispatch({
                        type: "patch-selection",
                        patch: { image_generation_api_key: event.target.value },
                      })
                    }
                  />
                </WizardField>
              ) : null}
            </div>
          ) : null}

          {!loadingSchema && currentStepId === "observability" && schema ? (
            <div className="wizard-grid">
              <label className="wizard-checkbox">
                <input
                  type="checkbox"
                  checked={state.selection.observability_enabled}
                  onChange={(event) =>
                    dispatch({
                      type: "patch-selection",
                      patch: { observability_enabled: event.target.checked },
                    })
                  }
                />
                <span>Enable Langfuse observability</span>
              </label>
              {state.selection.observability_enabled ? (
                <>
                  <WizardField label="Langfuse public key">
                    <input
                      value={state.selection.langfuse_public_key}
                      onChange={(event) =>
                        dispatch({
                          type: "patch-selection",
                          patch: { langfuse_public_key: event.target.value },
                        })
                      }
                    />
                  </WizardField>
                  <WizardField label="Langfuse secret key">
                    <input
                      type="password"
                      value={state.selection.langfuse_secret_key}
                      autoComplete="off"
                      onChange={(event) =>
                        dispatch({
                          type: "patch-selection",
                          patch: { langfuse_secret_key: event.target.value },
                        })
                      }
                    />
                  </WizardField>
                  <WizardField label="Langfuse base URL">
                    <input
                      value={state.selection.langfuse_base_url}
                      onChange={(event) =>
                        dispatch({
                          type: "patch-selection",
                          patch: { langfuse_base_url: event.target.value },
                        })
                      }
                    />
                  </WizardField>
                </>
              ) : null}
            </div>
          ) : null}

          {!loadingSchema && currentStepId === "voice" && schema ? (
            <VoiceSetupFields
              schema={schema}
              selection={state.selection}
              onChange={(patch) => dispatch({ type: "patch-selection", patch })}
              showEnableToggle
            />
          ) : null}

          {!loadingSchema && currentStepId === "identity" ? (
            <div className="wizard-grid">
              <WizardField label="Identity">
                <textarea
                  className="identity-editor"
                  value={state.selection.identity}
                  onChange={(event) =>
                    dispatch({ type: "patch-selection", patch: { identity: event.target.value } })
                  }
                />
              </WizardField>
              <div className="wizard-review">
                <h4>Review</h4>
                <ul>
                  <li>
                    <strong>Name:</strong> {state.name.trim()}
                  </li>
                  <li>
                    <strong>Provider:</strong> {state.selection.provider}
                  </li>
                  <li>
                    <strong>Model:</strong> {state.selection.model}
                  </li>
                  <li>
                    <strong>Search:</strong> {state.selection.search_provider}
                  </li>
                  <li>
                    <strong>Observability:</strong>{" "}
                    {state.selection.observability_enabled ? "Langfuse enabled" : "disabled"}
                  </li>
                  <li>
                    <strong>Voice:</strong>{" "}
                    {state.selection.voice_enabled ? state.selection.voice_provider : "disabled"}
                  </li>
                </ul>
                {directoryExists ? (
                  <label className="wizard-checkbox wizard-warning">
                    <input
                      type="checkbox"
                      checked={state.replaceExisting}
                      onChange={(event) =>
                        dispatch({ type: "patch", patch: { replaceExisting: event.target.checked } })
                      }
                    />
                    <span>Replace existing directory for this agent name</span>
                  </label>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>

        <div className="modal-footer">
          <Button type="button" variant="ghost" onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <div className="modal-footer-actions">
            {state.stepIndex > 0 ? (
              <Button type="button" variant="secondary" onClick={goBack} disabled={submitting}>
                Back
              </Button>
            ) : null}
            {!isLastStep ? (
              <Button type="button" variant="primary" onClick={goNext} disabled={loadingSchema || submitting}>
                Next
              </Button>
            ) : (
              <Button type="button" variant="primary" onClick={() => void submit()} disabled={loadingSchema || submitting}>
                {submitting ? "Creating..." : "Create agent"}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
