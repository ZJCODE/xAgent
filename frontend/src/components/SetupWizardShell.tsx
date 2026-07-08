import type { ReactNode } from "react";
import { classNames } from "../lib/format";
import { Button } from "./ui";

export interface SetupWizardStep {
  id: string;
  label: string;
}

export interface SetupWizardShellProps {
  open: boolean;
  title: string;
  subtitle: string;
  steps: SetupWizardStep[];
  stepIndex: number;
  loading?: boolean;
  submitting?: boolean;
  error?: string;
  submitLabel?: string;
  onClose: () => void;
  onBack: () => void;
  onNext: () => void;
  onSubmit: () => void;
  children: ReactNode;
}

export function SetupWizardShell({
  open,
  title,
  subtitle,
  steps,
  stepIndex,
  loading = false,
  submitting = false,
  error = "",
  submitLabel = "Save",
  onClose,
  onBack,
  onNext,
  onSubmit,
  children,
}: SetupWizardShellProps) {
  if (!open) return null;

  const currentStep = steps[stepIndex] ?? steps[0];
  const isLastStep = stepIndex >= steps.length - 1;

  const close = () => {
    if (submitting) return;
    onClose();
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={close}>
      <div
        className="modal-card wizard-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="setup-wizard-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="wizard-header-block">
          <div>
            <h3 id="setup-wizard-title">{title}</h3>
            <p className="wizard-subtitle">
              Step {stepIndex + 1} of {steps.length}: {currentStep?.label}
            </p>
          </div>
          <div className="wizard-steps" aria-hidden="true">
            {steps.map((step, index) => (
              <span
                key={step.id}
                className={classNames(
                  "wizard-step-chip",
                  index === stepIndex && "active",
                  index < stepIndex && "done",
                )}
              >
                {step.label}
              </span>
            ))}
          </div>
        </div>

        <div className="modal-body wizard-body">
          {loading ? <p>Loading setup options...</p> : null}
          {error ? <div className="error-strip">{error}</div> : null}
          {!loading ? children : null}
        </div>

        <div className="modal-footer">
          <Button type="button" variant="ghost" onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <div className="modal-footer-actions">
            {stepIndex > 0 ? (
              <Button type="button" variant="secondary" onClick={onBack} disabled={submitting}>
                Back
              </Button>
            ) : null}
            {!isLastStep ? (
              <Button type="button" variant="primary" onClick={onNext} disabled={loading || submitting}>
                Next
              </Button>
            ) : (
              <Button
                type="button"
                variant="primary"
                onClick={() => void onSubmit()}
                disabled={loading || submitting}
              >
                {submitting ? "Saving..." : submitLabel}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
