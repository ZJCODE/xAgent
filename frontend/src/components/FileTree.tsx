import { ChevronRight, FileText, Folder } from "lucide-react";
import { useState } from "react";
import { classNames, formatBytes } from "../lib/format";
import type { FileNode } from "../types";

interface FileTreeProps {
  nodes: FileNode[];
  selectedPath?: string;
  onSelect: (node: FileNode) => void;
}

function TreeNode({ node, selectedPath, onSelect }: { node: FileNode; selectedPath?: string; onSelect: (node: FileNode) => void }) {
  const [open, setOpen] = useState(true);
  const isDir = node.type === "dir";

  return (
    <div>
      <button
        type="button"
        className={classNames("tree-item", selectedPath === node.path && "selected")}
        onClick={() => {
          if (isDir) setOpen((value) => !value);
          else onSelect(node);
        }}
      >
        {isDir ? (
          <ChevronRight size={14} className={classNames("transition", open && "rotate-90")} />
        ) : (
          <FileText size={14} />
        )}
        {isDir && <Folder size={14} />}
        <span className="min-w-0 flex-1 truncate text-left">{node.name}</span>
        {!isDir && node.size != null && <span className="text-[10px] text-zinc-400">{formatBytes(node.size)}</span>}
      </button>
      {isDir && open && node.children?.length ? (
        <div className="tree-children">
          {node.children.map((child) => (
            <TreeNode key={child.path} node={child} selectedPath={selectedPath} onSelect={onSelect} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function FileTree({ nodes, selectedPath, onSelect }: FileTreeProps) {
  if (!nodes.length) {
    return <div className="empty-state">No files found</div>;
  }

  return (
    <div className="space-y-1">
      {nodes.map((node) => (
        <TreeNode key={node.path} node={node} selectedPath={selectedPath} onSelect={onSelect} />
      ))}
    </div>
  );
}
