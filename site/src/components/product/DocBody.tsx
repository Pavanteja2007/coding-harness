import { cn } from "@/lib/cn";
import { highlight } from "@/lib/highlight";
import type { DocBlock } from "@/lib/content/docs";

/**
 * Renders a documentation block.
 *
 * Deliberately a small typed renderer rather than MDX: the docs are structured
 * data, so the set of block kinds is closed and every one can be styled to the
 * design system without an escape hatch that lets arbitrary markup in.
 *
 * ASYNC because code blocks are tokenised with shiki at BUILD time - the
 * highlighted HTML is baked into the static output and shiki never reaches the
 * client. This is a server component, so making it async costs nothing and
 * keeps the runtime highlighting cost at zero.
 */
export async function DocBody({ blocks }: { blocks: DocBlock[] }) {
  // Highlight up front: a .map() inside JSX cannot await.
  const highlighted = await Promise.all(
    blocks.map((b) =>
      b.type === "code" ? highlight(b.lines.join("\n"), b.lang) : null
    )
  );

  return (
    <div className="flex flex-col gap-6">
      {blocks.map((b, i) => {
        switch (b.type) {
          case "h":
            return (
              <h2
                key={i}
                id={b.text.toLowerCase().replace(/[^a-z0-9]+/g, "-")}
                className="mt-4 scroll-mt-24 font-sans text-h3 text-quench"
              >
                {b.text}
              </h2>
            );

          case "p":
            return (
              <p key={i} className="max-w-[68ch] text-body text-ash">
                {b.text}
              </p>
            );

          case "list":
            return (
              <ul key={i} className="flex max-w-[68ch] flex-col gap-2.5">
                {b.items.map((it) => (
                  <li key={it} className="flex gap-3 text-body text-ash">
                    <span
                      aria-hidden="true"
                      className="mt-3 h-px w-4 shrink-0 bg-rule-hot"
                    />
                    <span>{it}</span>
                  </li>
                ))}
              </ul>
            );

          case "code":
            return (
              // tabIndex + label: a horizontally scrollable region must be
              // reachable by keyboard, or someone navigating without a mouse
              // cannot read a command that overflows.
              //
              // shiki emits its own <pre><code>, so this wrapper is a div -
              // nesting <pre> inside <pre> is invalid markup.
              <div
                key={i}
                tabIndex={0}
                role="region"
                aria-label={b.lang ? b.lang + " code block" : "Code block"}
                className={cn(
                  "overflow-x-auto rounded-lg border border-rule etch",
                  "[&_pre]:m-0 [&_pre]:bg-char [&_pre]:p-4",
                  "[&_code]:font-mono [&_code]:text-mono"
                )}
                dangerouslySetInnerHTML={{ __html: highlighted[i] ?? "" }}
              />
            );

          case "note":
            return (
              <aside
                key={i}
                className={cn(
                  "rounded-lg border border-rule border-l-2 bg-char p-5",
                  b.tone === "warn" ? "border-l-warn" : "border-l-ox-bright"
                )}
              >
                <h3
                  className={cn(
                    "mb-2 font-mono text-mono",
                    b.tone === "warn" ? "text-warn" : "text-ox-bright"
                  )}
                >
                  {b.title}
                </h3>
                <p className="max-w-[64ch] text-small text-ash">{b.text}</p>
              </aside>
            );

          case "table":
            return (
              <div
                key={i}
                tabIndex={0}
                role="region"
                aria-label="Table"
                className="overflow-x-auto rounded-lg border border-rule etch"
              >
                <table className="w-full min-w-[520px] border-collapse text-left">
                  <thead>
                    <tr className="border-b border-rule bg-slab">
                      {b.head.map((h) => (
                        <th
                          key={h}
                          scope="col"
                          className="px-4 py-3 font-mono text-mono font-medium text-smoke"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, ri) => (
                      <tr key={ri} className="border-b border-rule last:border-0">
                        {r.map((cell, ci) => (
                          <td
                            key={ci}
                            className={cn(
                              "px-4 py-3",
                              ci === 0
                                ? "font-mono text-mono text-ox-bright"
                                : "text-small text-ash"
                            )}
                          >
                            {cell}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
        }
      })}
    </div>
  );
}
