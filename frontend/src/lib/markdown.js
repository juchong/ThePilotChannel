// Markdown to HTML for the admin page's copy of the display API reference: ATX
// headings, paragraphs, "-" lists, fenced code, pipe tables, code spans and bold,
// with every character of the source escaped. Comment lines are left out.
import { esc } from "./dom.js";

const FENCE_RE = /^(`{3,})\s*([\w-]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.*?)\s*$/;
const ITEM_RE = /^-\s+(.*)$/;
const COMMENT_RE = /^<!--.*-->\s*$/;
const DIVIDER_RE = /^\|?(\s*:?-{3,}:?\s*\|)*\s*:?-{3,}:?\s*\|?\s*$/;
const CODE_RE = /(`+)([\s\S]*?[^`])\1(?!`)/g;

// Code spans as written (a run of n backticks closes at the next run of n), bold elsewhere.
export function inlineHtml(text) {
  const plain = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const s = String(text);
  let out = "";
  let at = 0;
  for (const m of s.matchAll(CODE_RE)) {
    let code = m[2];
    if (code.length > 2 && code.startsWith(" ") && code.endsWith(" ") && code.trim()) code = code.slice(1, -1);
    out += plain(s.slice(at, m.index)) + `<code>${esc(code)}</code>`;
    at = m.index + m[0].length;
  }
  return out + plain(s.slice(at));
}

// A table row's cells; "\|" is a pipe inside a cell.
export function tableCells(line) {
  const body = line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  return body.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

const isRow = (line) => /^\s*\|/.test(line);
const startsTable = (lines, i) => isRow(lines[i]) && i + 1 < lines.length && DIVIDER_RE.test(lines[i + 1].trim());
const startsBlock = (lines, i) =>
  FENCE_RE.test(lines[i]) || HEADING_RE.test(lines[i]) || ITEM_RE.test(lines[i]) || COMMENT_RE.test(lines[i]) || startsTable(lines, i);

// `shift` lowers every heading by that many levels, under the page's own heading.
export function markdownHtml(src, { shift = 1 } = {}) {
  const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim() || COMMENT_RE.test(line)) {
      i++;
    } else if ((m = FENCE_RE.exec(line))) {
      const body = [];
      for (i++; i < lines.length && !(lines[i].trim().startsWith(m[1]) && /^`+$/.test(lines[i].trim())); i++) body.push(lines[i]);
      i++;
      out.push(`<pre><code>${esc(body.join("\n"))}</code></pre>`);
    } else if ((m = HEADING_RE.exec(line))) {
      const level = Math.min(6, m[1].length + shift);
      out.push(`<h${level}>${inlineHtml(m[2])}</h${level}>`);
      i++;
    } else if (startsTable(lines, i)) {
      const head = tableCells(line);
      const rows = [];
      for (i += 2; i < lines.length && isRow(lines[i]); i++) rows.push(tableCells(lines[i]));
      out.push(
        `<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inlineHtml(c)}</th>`).join("")}</tr></thead><tbody>` +
          rows.map((r) => `<tr>${r.map((c) => `<td>${inlineHtml(c)}</td>`).join("")}</tr>`).join("") +
          `</tbody></table></div>`
      );
    } else if (ITEM_RE.test(line)) {
      const items = [];
      while (i < lines.length && (m = ITEM_RE.exec(lines[i]))) {
        const parts = [m[1]];
        for (i++; i < lines.length && /^\s{2,}\S/.test(lines[i]) && !ITEM_RE.test(lines[i]); i++) parts.push(lines[i].trim());
        items.push(`<li>${inlineHtml(parts.join(" "))}</li>`);
      }
      out.push(`<ul>${items.join("")}</ul>`);
    } else {
      const parts = [line.trim()];
      for (i++; i < lines.length && lines[i].trim() && !startsBlock(lines, i); i++) parts.push(lines[i].trim());
      out.push(`<p>${inlineHtml(parts.join(" "))}</p>`);
    }
  }
  return out.join("\n");
}
