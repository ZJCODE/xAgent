import { ChevronRight, FileText, Folder } from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";
import { classNames, formatBytes } from "../lib/format";
import type { FileNode } from "../types";

interface FileTreeProps {
  nodes: FileNode[];
  selectedPath?: string;
  onSelect: (node: FileNode) => void;
  onDirectoryOpen?: (node: FileNode) => boolean;
  renderActions?: (node: FileNode) => ReactNode;
}

function TreeNode({
  node,
  selectedPath,
  onSelect,
  onDirectoryOpen,
  renderActions,
}: {
  node: FileNode;
  selectedPath?: string;
  onSelect: (node: FileNode) => void;
  onDirectoryOpen?: (node: FileNode) => boolean;
  renderActions?: (node: FileNode) => ReactNode;
}) {
  const [open, setOpen] = useState(true);
  const isDir = node.type === "dir";

  return (
    <div>
      <div className={classNames("tree-item-row", selectedPath === node.path && "selected")}>
        {isDir ? (
          <button
            type="button"
            className="tree-toggle"
            onClick={() => setOpen((value) => !value)}
            title={open ? `Collapse ${node.name}` : `Expand ${node.name}`}
            aria-label={open ? `Collapse ${node.name}` : `Expand ${node.name}`}
            aria-expanded={open}
          >
            <ChevronRight size={14} className={classNames("transition", open && "rotate-90")} />
          </button>
        ) : <span className="tree-toggle-spacer" />}
        <button
          type="button"
          className="tree-item"
          onClick={() => {
            if (isDir) {
              if (!onDirectoryOpen?.(node)) setOpen((value) => !value);
              return;
            }
            onSelect(node);
          }}
        >
          {isDir ? <Folder size={14} /> : <FileText size={14} />}
          <span className="min-w-0 flex-1 truncate text-left">{node.name}</span>
          {!isDir && node.size != null && <span className="text-[10px] text-zinc-400">{formatBytes(node.size)}</span>}
        </button>
        {renderActions ? <div className="tree-item-actions">{renderActions(node)}</div> : null}
      </div>
      {isDir && open && node.children?.length ? (
        <div className="tree-children">
          {node.children.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              selectedPath={selectedPath}
              onSelect={onSelect}
              onDirectoryOpen={onDirectoryOpen}
              renderActions={renderActions}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function FileTree({ nodes, selectedPath, onSelect, onDirectoryOpen, renderActions }: FileTreeProps) {
  if (!nodes.length) {
    return <div className="empty-state">No files found</div>;
  }

  return (
    <div className="space-y-1">
      {nodes.map((node) => (
        <TreeNode
          key={node.path}
          node={node}
          selectedPath={selectedPath}
          onSelect={onSelect}
          onDirectoryOpen={onDirectoryOpen}
          renderActions={renderActions}
        />
      ))}
    </div>
  );
}
