import { ChevronDown, Globe, Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Button, StatusBadge } from "../components/ui";
import { classNames } from "../lib/format";
import { CreateWorldDialog } from "./CreateWorldDialog";
import { useWorld } from "./WorldContext";
import type { WorldSummary } from "./protocol";

export function WorldSwitcher() {
  const {
    worlds,
    worldId,
    worldName,
    connected,
    joined,
    enterWorld,
    createError,
    deleteWorld,
    deleting,
    deleteError,
  } = useWorld();
  const [open, setOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<WorldSummary | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onPointerDown);
    return () => window.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const openWizard = () => {
    setOpen(false);
    setWizardOpen(true);
  };

  const requestDelete = (world: WorldSummary) => {
    setOpen(false);
    setPendingDelete(world);
  };

  const confirmDelete = () => {
    if (!pendingDelete || deleting) return;
    void deleteWorld(pendingDelete.id).then((ok) => {
      if (ok) setPendingDelete(null);
    });
  };

  const presentCount = pendingDelete?.present_count || 0;

  if (worlds.length === 0) {
    return (
      <div className="agent-switcher">
        <span className="agent-switcher-label">World</span>
        <div className="agent-switcher-empty">
          <div className="agent-switcher-empty-icon" aria-hidden="true">
            <Globe size={18} />
          </div>
          <p className="agent-switcher-empty-title">Create your first world</p>
          <p className="agent-switcher-empty-copy">Try names like plaza or cafe.</p>
          {createError ? <p className="world-error">{createError}</p> : null}
          <Button type="button" variant="primary" className="agent-switcher-empty-button" onClick={openWizard}>
            <Plus size={14} />
            New world
          </Button>
        </div>
        <CreateWorldDialog open={wizardOpen} onClose={() => setWizardOpen(false)} />
      </div>
    );
  }

  return (
    <div className="agent-switcher" ref={rootRef}>
      <span className="agent-switcher-label">World</span>
      <button
        type="button"
        className={classNames("agent-switcher-trigger", open && "open")}
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="agent-switcher-current">
          <strong>{worldName || "Select a world"}</strong>
        </span>
        <ChevronDown size={15} />
      </button>

      {open ? (
        <div className="agent-switcher-menu" role="listbox">
          {worlds.map((world) => {
            const selected = world.id === worldId && connected && joined;
            return (
              <div
                key={world.id}
                className={classNames("world-switcher-option-row", selected && "selected")}
              >
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={classNames("agent-switcher-option", selected && "selected")}
                  onClick={() => {
                    setOpen(false);
                    void enterWorld(world.id);
                  }}
                >
                  <div className="agent-switcher-option-copy">
                    <strong>{world.name}</strong>
                  </div>
                  <div className="agent-switcher-option-meta">
                    {typeof world.present_count === "number" && world.present_count > 0 ? (
                      <StatusBadge tone="good" className="agent-switcher-badge">
                        {world.present_count}
                      </StatusBadge>
                    ) : null}
                  </div>
                </button>
                <button
                  type="button"
                  className="world-switcher-delete"
                  title={`Delete ${world.name}`}
                  aria-label={`Delete ${world.name}`}
                  onClick={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    requestDelete(world);
                  }}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            );
          })}
          <button type="button" className="agent-switcher-new" onClick={openWizard}>
            <Plus size={14} />
            New world
          </button>
        </div>
      ) : null}

      <CreateWorldDialog open={wizardOpen} onClose={() => setWizardOpen(false)} />
      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title={`Delete world “${pendingDelete?.name || ""}”?`}
        description={
          <>
            {presentCount > 0 ? (
              <p>
                {presentCount === 1
                  ? "1 person is present and will be disconnected."
                  : `${presentCount} people are present and will be disconnected.`}
              </p>
            ) : null}
            <p>This permanently removes the event log and spoken files.</p>
            {deleteError ? <p className="world-error">{deleteError}</p> : null}
          </>
        }
        confirmLabel={deleting ? "Deleting…" : "Delete world"}
        confirmDisabled={deleting}
        onCancel={() => {
          if (!deleting) setPendingDelete(null);
        }}
        onConfirm={confirmDelete}
      />
    </div>
  );
}
