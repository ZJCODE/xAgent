import type { KeyboardEvent } from "react";

interface TextEditorProps {
  value: string;
  path: string;
  onChange?: (value: string) => void;
  onSave?: () => void;
}

interface ConflictEditorProps extends Required<Pick<TextEditorProps, "value" | "path" | "onChange" | "onSave">> {
  current: string;
}

function saveShortcut(event: KeyboardEvent<HTMLTextAreaElement>, onSave?: () => void) {
  if (!onSave || event.key.toLowerCase() !== "s" || (!event.metaKey && !event.ctrlKey)) return;
  event.preventDefault();
  onSave();
}

export function TextEditor({ value, path, onChange, onSave }: TextEditorProps) {
  return (
    <textarea
      className="skill-text-editor"
      aria-label={`Edit ${path}`}
      value={value}
      spellCheck={false}
      onChange={(event) => onChange?.(event.target.value)}
      onKeyDown={(event) => saveShortcut(event, onSave)}
    />
  );
}

export function ConflictEditor({ current, value, path, onChange, onSave }: ConflictEditorProps) {
  return (
    <div className="skill-merge-editor" aria-label={`Resolve changes for ${path}`}>
      <section>
        <strong>Current on disk</strong>
        <pre className="skill-conflict-current">{current}</pre>
      </section>
      <section>
        <strong>Your draft / merged result</strong>
        <TextEditor value={value} path={path} onChange={onChange} onSave={onSave} />
      </section>
    </div>
  );
}
