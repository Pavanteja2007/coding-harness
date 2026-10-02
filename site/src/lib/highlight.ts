import { createHighlighter, type Highlighter } from "shiki";

/**
 * Build-time syntax highlighting.
 *
 * Runs in the server component that renders a doc page, so the tokenised HTML
 * is baked into the static output and shiki never reaches the client. Zero
 * runtime JS, which is the whole reason to prefer it over a client highlighter.
 *
 * THEME: hand-written against the site's own tokens rather than an off-the-
 * shelf theme. Every bundled theme assumes a neutral or blue-ish dark ground;
 * dropping one onto oxblood-on-near-black produces the exact clash the design
 * language exists to avoid. The palette below uses only colours already in
 * globals.css, and every foreground was checked against --char (#1C1619), the
 * code-block ground:
 *
 *   quench   #F2EFEA  15.54:1   plain text, identifiers
 *   ash      #A39B9C   6.56:1   punctuation, operators
 *   smoke    #8F888B   5.15:1   comments
 *   ox-pale  #F0A8B8  11.64:1   keywords
 *   verdant  #5FCFA0   9.27:1   strings
 *   warn     #E0A03C   7.86:1   numbers, constants
 *
 * Comments sit at --smoke deliberately: they must recede without dropping
 * below AA, which the old --soot (1.79:1 on char) would have done.
 */

const THEME = {
  name: "oxblood",
  type: "dark" as const,
  colors: {
    "editor.background": "#1C1619",
    "editor.foreground": "#F2EFEA",
  },
  settings: [
    { scope: ["comment", "punctuation.definition.comment"], settings: { foreground: "#8F888B", fontStyle: "italic" } },
    { scope: ["string", "string.quoted", "constant.other.symbol"], settings: { foreground: "#5FCFA0" } },
    { scope: ["constant.numeric", "constant.language", "constant.character"], settings: { foreground: "#E0A03C" } },
    { scope: ["keyword", "storage", "storage.type", "keyword.control", "keyword.operator.new"], settings: { foreground: "#F0A8B8" } },
    { scope: ["entity.name.function", "support.function", "meta.function-call"], settings: { foreground: "#D4607A" } },
    { scope: ["variable", "variable.parameter", "entity.name.variable"], settings: { foreground: "#F2EFEA" } },
    { scope: ["entity.name.type", "entity.name.class", "support.type", "support.class"], settings: { foreground: "#E0A03C" } },
    { scope: ["punctuation", "meta.brace", "keyword.operator"], settings: { foreground: "#A39B9C" } },
    { scope: ["variable.function", "entity.name.tag"], settings: { foreground: "#D4607A" } },
    { scope: ["invalid", "invalid.illegal"], settings: { foreground: "#E8763F" } },
  ],
};

// One highlighter for the whole build. createHighlighter is expensive, and a
// static export renders every doc page in the same process.
let cached: Promise<Highlighter> | null = null;

function getHighlighter() {
  if (!cached) {
    cached = createHighlighter({
      themes: [THEME],
      langs: ["bash", "python", "json", "toml", "typescript"],
    });
  }
  return cached;
}

/** Languages the docs actually use. Anything else falls back to plain text. */
const SUPPORTED = new Set(["bash", "python", "json", "toml", "typescript"]);

export async function highlight(code: string, lang?: string): Promise<string> {
  const language = lang && SUPPORTED.has(lang) ? lang : "bash";
  try {
    const hl = await getHighlighter();
    return hl.codeToHtml(code, { lang: language, theme: "oxblood" });
  } catch {
    // Highlighting is presentation. If it fails, the reader still needs the
    // code, so fall back to escaped plain text rather than throwing the page.
    const escaped = code
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    return `<pre><code>${escaped}</code></pre>`;
  }
}
