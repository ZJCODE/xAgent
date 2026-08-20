import {
  markdown,
  markdownWithoutImages,
  splitMarkdownDocument,
  type MarkdownFrontmatterEntry,
  type MarkdownFrontmatterValue,
} from "../lib/markdown";

interface MarkdownProps {
  content: string;
  className?: string;
  renderImages?: boolean;
}

function FrontmatterValue({ value }: { value: MarkdownFrontmatterValue }) {
  if (value.kind === "scalar") {
    return value.text ? <span>{value.text}</span> : <span className="markdown-frontmatter-empty">—</span>;
  }
  if (value.kind === "list") {
    if (!value.items.length) {
      return <span className="markdown-frontmatter-empty">—</span>;
    }
    return (
      <ul className="markdown-frontmatter-chips">
        {value.items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    );
  }
  if (!value.entries.length) {
    return <span className="markdown-frontmatter-empty">—</span>;
  }
  return <FrontmatterEntries entries={value.entries} nested />;
}

function FrontmatterEntries({
  entries,
  nested = false,
}: {
  entries: MarkdownFrontmatterEntry[];
  nested?: boolean;
}) {
  return (
    <dl className={nested ? "markdown-frontmatter-nested" : undefined}>
      {entries.map((entry) => (
        <div
          key={entry.key}
          className={
            entry.value.kind === "map"
              ? "markdown-frontmatter-row is-nested"
              : "markdown-frontmatter-row"
          }
        >
          <dt>{entry.key}</dt>
          <dd>
            <FrontmatterValue value={entry.value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function Markdown({ content, className = "", renderImages = true }: MarkdownProps) {
  const renderer = renderImages ? markdown : markdownWithoutImages;
  const document = splitMarkdownDocument(content || "");
  const showFrontmatter = document.entries.length > 0;
  const body = showFrontmatter ? document.body : content || "";

  return (
    <div className={`markdown-content ${className}`.trim()}>
      {showFrontmatter ? (
        <aside className="markdown-frontmatter" aria-label="Frontmatter">
          <FrontmatterEntries entries={document.entries} />
        </aside>
      ) : null}
      {body.trim() ? (
        <div dangerouslySetInnerHTML={{ __html: renderer.render(body) }} />
      ) : null}
    </div>
  );
}
