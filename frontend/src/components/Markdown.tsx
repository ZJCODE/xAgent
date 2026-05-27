import { markdown, markdownWithoutImages } from "../lib/markdown";

interface MarkdownProps {
  content: string;
  className?: string;
  renderImages?: boolean;
}

export function Markdown({ content, className = "", renderImages = true }: MarkdownProps) {
  const renderer = renderImages ? markdown : markdownWithoutImages;
  return (
    <div
      className={`markdown-content ${className}`}
      dangerouslySetInnerHTML={{ __html: renderer.render(content || "") }}
    />
  );
}
