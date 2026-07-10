import { useEffect, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { classNames } from "../lib/format";
import { ConfirmDialog } from "./ConfirmDialog";
import { Button, IconButton } from "./ui";

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
  isDirty?: boolean;
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
  isDirty = false,
  error = "",
  submitLabel = "Save",
  onClose,
  onBack,
  onNext,
  onSubmit,
  children,
}: SetupWizardShellProps) {
  const [discardConfirmationOpen, setDiscardConfirmationOpen] = useState(false);
  const currentStep = steps[stepIndex] ?? steps[0];
  const isLastStep = stepIndex >= steps.length - 1;

  const requestClose = () => {
    if (submitting) return;
    if (isDirty) {
      setDiscardConfirmationOpen(true);
      return;
    }
    onClose();
  };

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || discardConfirmationOpen) return;
      event.preventDefault();
      requestClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [discardConfirmationOpen, isDirty, onClose, open, submitting]);

  if (!open) return null;

  return (
    <div className="modal-overlay" role="presentation">
      <div
        className="modal-card wizard-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="setup-wizard-title"
      >
        <div className="wizard-header-block">
          <div className="wizard-header-heading">
            <div>
              <h3 id="setup-wizard-title">{title}</h3>
              <p className="wizard-subtitle">
                Step {stepIndex + 1} of {steps.length}: {currentStep?.label}
              </p>
            </div>
            <IconButton
              type="button"
              onClick={requestClose}
              disabled={submitting}
              title="Close setup"
              aria-label="Close setup"
            >
              <X size={16} />
            </IconButton>
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
          <Button type="button" variant="ghost" onClick={requestClose} disabled={submitting}>
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
      <ConfirmDialog
        open={discardConfirmationOpen}
        title="Discard unsaved setup?"
        description="Your changes have not been saved and will be lost."
        confirmLabel="Discard changes"
        onConfirm={() => {
          setDiscardConfirmationOpen(false);
          onClose();
        }}
        onCancel={() => setDiscardConfirmationOpen(false)}
      />
    </div>
  );
}
