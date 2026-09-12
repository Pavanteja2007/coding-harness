import { cn } from "@/lib/cn";
import type { DocBlock } from "@/lib/content/docs";

/**
 * Renders a documentation block.
 *
 * Deliberately a small typed renderer rather than MDX: the docs are structured
 * data, so the set of block kinds is closed and every one can be styled to the
 * design system without an escape hatch that lets arbitrary markup in.
 */
export function DocBody({ blocks }: { blocks: DocBlock[] }) {
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
              <pre
                key={i}
                className="overflow-x-auto rounded-lg border border-rule bg-char p-4 etch"
              >
                <code className="font-mono text-mono text-quench">
                  {b.lines.map((l, j) => (
                    <span key={j} className="block whitespace-pre">
                      {l || " "}
                    </span>
                  ))}
                </code>
              </pre>
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
