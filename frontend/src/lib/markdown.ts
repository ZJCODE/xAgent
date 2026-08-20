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

export type MarkdownFrontmatterValue =
  | { kind: "scalar"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "map"; entries: MarkdownFrontmatterEntry[] };

export interface MarkdownFrontmatterEntry {
  key: string;
  value: MarkdownFrontmatterValue;
}

export interface SplitMarkdownDocument {
  frontmatter: string | null;
  entries: MarkdownFrontmatterEntry[];
  body: string;
}

function stripYamlQuotes(value: string): string {
  const text = value.trim();
  if (
    (text.startsWith('"') && text.endsWith('"')) ||
    (text.startsWith("'") && text.endsWith("'"))
  ) {
    return text.slice(1, -1);
  }
  return text;
}

function lineIndent(line: string): number {
  const match = line.match(/^[\t ]*/);
  return match ? match[0].length : 0;
}

function isBlank(line: string): boolean {
  return !line.trim();
}

function isListItem(line: string): boolean {
  return /^\s*-\s+/.test(line);
}

function isMappingLine(line: string): boolean {
  return /^\s*[A-Za-z0-9_-]+:\s*/.test(line) && !isListItem(line);
}

function listItemText(line: string): string {
  return stripYamlQuotes(line.replace(/^\s*-\s+/, ""));
}

/** Split a leading YAML frontmatter fence from common Markdown documents. */
export function splitMarkdownDocument(content: string): SplitMarkdownDocument {
  const source = content || "";
  const match = source.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (!match) {
    return { frontmatter: null, entries: [], body: source };
  }
  const frontmatter = match[1];
  return {
    frontmatter,
    entries: parseFrontmatterEntries(frontmatter),
    body: source.slice(match[0].length),
  };
}

export function parseFrontmatterEntries(raw: string): MarkdownFrontmatterEntry[] {
  const lines = raw.replace(/\t/g, "  ").split(/\r?\n/);
  return parseMapLines(lines, 0, 0).entries;
}

function parseMapLines(
  lines: string[],
  start: number,
  baseIndent: number,
): { entries: MarkdownFrontmatterEntry[]; next: number } {
  const entries: MarkdownFrontmatterEntry[] = [];
  let index = start;

  while (index < lines.length) {
    const line = lines[index];
    if (isBlank(line)) {
      index += 1;
      continue;
    }
    const indent = lineIndent(line);
    if (indent < baseIndent) break;
    if (indent > baseIndent && baseIndent > 0) break;
    if (indent > baseIndent && baseIndent === 0) {
      // Orphan indented line at top level — skip.
      index += 1;
      continue;
    }

    if (isListItem(line) && indent === baseIndent) {
      // Bare sequence belonging to previous key should have been consumed.
      index += 1;
      continue;
    }

    const field = line.slice(indent).match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!field) {
      index += 1;
      continue;
    }

    const key = field[1];
    const inline = field[2];
    index += 1;

    if (inline.trim()) {
      entries.push({ key, value: { kind: "scalar", text: stripYamlQuotes(inline) } });
      continue;
    }

    const parsed = parseValueBlock(lines, index, baseIndent);
    entries.push({ key, value: parsed.value });
    index = parsed.next;
  }

  return { entries, next: index };
}

function parseValueBlock(
  lines: string[],
  start: number,
  parentIndent: number,
): { value: MarkdownFrontmatterValue; next: number } {
  let index = start;
  while (index < lines.length && isBlank(lines[index])) index += 1;
  if (index >= lines.length) {
    return { value: { kind: "scalar", text: "" }, next: index };
  }

  const first = lines[index];
  const firstIndent = lineIndent(first);

  // Common YAML: `tags:` followed by unindented `- item` lines.
  if (isListItem(first) && firstIndent <= parentIndent) {
    const items: string[] = [];
    while (index < lines.length) {
      const line = lines[index];
      if (isBlank(line)) {
        index += 1;
        continue;
      }
      if (!isListItem(line) || lineIndent(line) > parentIndent) break;
      if (lineIndent(line) < parentIndent) break;
      if (lineIndent(line) === parentIndent && isMappingLine(line)) break;
      if (lineIndent(line) === parentIndent && !isListItem(line)) break;
      items.push(listItemText(line));
      index += 1;
    }
    return { value: { kind: "list", items }, next: index };
  }

  if (firstIndent <= parentIndent) {
    return { value: { kind: "scalar", text: "" }, next: start };
  }

  const childIndent = firstIndent;

  if (isListItem(first)) {
    const items: string[] = [];
    while (index < lines.length) {
      const line = lines[index];
      if (isBlank(line)) {
        index += 1;
        continue;
      }
      const indent = lineIndent(line);
      if (indent < childIndent) break;
      if (indent === childIndent && isListItem(line)) {
        items.push(listItemText(line));
        index += 1;
        continue;
      }
      if (indent === childIndent) break;
      // Nested content under a list item — fold into the last item text.
      if (items.length) {
        items[items.length - 1] = `${items[items.length - 1]}\n${line.slice(childIndent)}`.trimEnd();
      }
      index += 1;
    }
    return { value: { kind: "list", items }, next: index };
  }

  if (isMappingLine(first)) {
    // Mixed map/list under a key, e.g.
    // source:
    //   diary:
    //   - '2026-08-20'
    //   person: api:web_user
    const entries: MarkdownFrontmatterEntry[] = [];
    while (index < lines.length) {
      const line = lines[index];
      if (isBlank(line)) {
        index += 1;
        continue;
      }
      const indent = lineIndent(line);
      if (indent < childIndent) break;

      if (indent === childIndent && isListItem(line)) {
        // Sequence at map indent after a key like `diary:` with empty value —
        // attach to the previous entry if it is an empty scalar.
        const item = listItemText(line);
        const previous = entries[entries.length - 1];
        if (previous && previous.value.kind === "scalar" && !previous.value.text) {
          previous.value = { kind: "list", items: [item] };
        } else if (previous && previous.value.kind === "list") {
          previous.value.items.push(item);
        } else {
          entries.push({ key: "items", value: { kind: "list", items: [item] } });
        }
        index += 1;
        continue;
      }

      if (indent === childIndent && isMappingLine(line)) {
        const field = line.slice(indent).match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
        if (!field) {
          index += 1;
          continue;
        }
        const key = field[1];
        const inline = field[2];
        index += 1;
        if (inline.trim()) {
          entries.push({ key, value: { kind: "scalar", text: stripYamlQuotes(inline) } });
          continue;
        }
        // Peek: following unindented-to-child list items belong to this key.
        const nested = parseValueBlock(lines, index, childIndent);
        entries.push({ key, value: nested.value });
        index = nested.next;
        continue;
      }

      // Deeper indent — let nested parser consume from previous empty key, else skip.
      index += 1;
    }
    return { value: { kind: "map", entries }, next: index };
  }

  // Fallback: keep indented prose as scalar.
  const block: string[] = [];
  while (index < lines.length) {
    const line = lines[index];
    if (isBlank(line)) {
      if (block.length) block.push("");
      index += 1;
      continue;
    }
    if (lineIndent(line) < childIndent) break;
    block.push(line.slice(childIndent));
    index += 1;
  }
  while (block.length && !block[block.length - 1].trim()) block.pop();
  return { value: { kind: "scalar", text: block.join("\n") }, next: index };
}
