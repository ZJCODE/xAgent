import MarkdownIt from "markdown-it";

const markdownOptions = {
  html: false,
  linkify: true,
  breaks: true,
  typographer: true,
};

export const markdown = new MarkdownIt(markdownOptions);

export const markdownWithoutImages = new MarkdownIt(markdownOptions);

markdownWithoutImages.renderer.rules.image = (tokens, index) => {
  const token = tokens[index];
  const src = token.attrGet("src") || "";
  const label = token.content || src || "Image";
  return markdownWithoutImages.utils.escapeHtml(label);
};
