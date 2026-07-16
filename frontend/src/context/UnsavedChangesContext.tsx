import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";

interface UnsavedChangesContextValue {
  dirty: boolean;
  setDirty: (dirty: boolean) => void;
  confirmDiscard: () => Promise<boolean>;
}

const UnsavedChangesContext = createContext<UnsavedChangesContextValue | null>(null);

export function UnsavedChangesProvider({ children }: { children: ReactNode }) {
  const [dirty, setDirty] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const resolverRef = useRef<((value: boolean) => void) | null>(null);

  useEffect(() => {
    if (!dirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [dirty]);

  const settle = useCallback((value: boolean) => {
    const resolve = resolverRef.current;
    resolverRef.current = null;
    setDialogOpen(false);
    if (value) setDirty(false);
    resolve?.(value);
  }, []);

  const confirmDiscard = useCallback(() => {
    if (!dirty) return Promise.resolve(true);
    if (resolverRef.current) {
      return new Promise<boolean>((resolve) => {
        const previous = resolverRef.current;
        resolverRef.current = (value) => {
          previous?.(false);
          resolve(value);
        };
      });
    }
    return new Promise<boolean>((resolve) => {
      resolverRef.current = resolve;
      setDialogOpen(true);
    });
  }, [dirty]);

  const value = useMemo(() => ({ dirty, setDirty, confirmDiscard }), [dirty, confirmDiscard]);

  return (
    <UnsavedChangesContext.Provider value={value}>
      {children}
      <ConfirmDialog
        open={dialogOpen}
        title="Discard unsaved changes?"
        description="Your changes have not been saved and will be lost."
        confirmLabel="Discard changes"
        onConfirm={() => settle(true)}
        onCancel={() => settle(false)}
      />
    </UnsavedChangesContext.Provider>
  );
}

export function useUnsavedChanges() {
  const value = useContext(UnsavedChangesContext);
  if (!value) throw new Error("useUnsavedChanges must be used inside UnsavedChangesProvider");
  return value;
}
