import MarkdownIt from "markdown-it";

export const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  typographer: true,
});
