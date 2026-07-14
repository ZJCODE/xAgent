import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

interface UnsavedChangesContextValue {
  dirty: boolean;
  setDirty: (dirty: boolean) => void;
  confirmDiscard: () => boolean;
}

const UnsavedChangesContext = createContext<UnsavedChangesContextValue | null>(null);

export function UnsavedChangesProvider({ children }: { children: ReactNode }) {
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!dirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [dirty]);

  const confirmDiscard = useCallback(() => {
    if (!dirty) return true;
    if (!window.confirm("Discard unsaved changes?")) return false;
    setDirty(false);
    return true;
  }, [dirty]);

  const value = useMemo(() => ({ dirty, setDirty, confirmDiscard }), [dirty, confirmDiscard]);
  return <UnsavedChangesContext.Provider value={value}>{children}</UnsavedChangesContext.Provider>;
}

export function useUnsavedChanges() {
  const value = useContext(UnsavedChangesContext);
  if (!value) throw new Error("useUnsavedChanges must be used inside UnsavedChangesProvider");
  return value;
}
