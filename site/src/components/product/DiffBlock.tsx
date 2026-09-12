import { cn } from "@/lib/cn";

/**
 * Unified-diff block with +/- gutters.
 *
 * DESIGN.md §9: "Colour is never the sole signal." So additions and deletions
 * carry a +/- GLYPH as well as patina/fail colour - a red-green colour-blind
 * reader gets the same information from the gutter that everyone else gets from
 * the hue.
 */
export type DiffLine = { kind: "add" | "del" | "ctx" | "meta"; text: string };

export function DiffBlock({
  file,
  lines,
  className,
}: {
  file: string;
  lines: DiffLine[];
  className?: string;
}) {
  return (
    <figure
      className={cn(
        "m-0 overflow-hidden rounded-lg border border-rule bg-char etch",
        className
      )}
    >
      <figcaption className="flex items-center justify-between border-b border-rule bg-slab px-4 py-2.5">
        <code className="font-mono text-mono text-ash">{file}</code>
        <span className="font-mono text-mono text-soot">unified diff</span>
      </figcaption>

      <div className="overflow-x-auto">
        <pre className="min-w-max py-2 font-mono text-mono leading-[1.75]">
          {lines.map((l, i) => (
            <div
              key={i}
              className={cn(
                "flex gap-3 px-4",
                l.kind === "add" && "bg-verdant-dim",
                l.kind === "del" && "bg-fail/10"
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "w-3 shrink-0 select-none text-center",
                  l.kind === "add" && "text-verdant-bright",
                  l.kind === "del" && "text-fail",
                  (l.kind === "ctx" || l.kind === "meta") && "text-soot"
                )}
              >
                {l.kind === "add" ? "+" : l.kind === "del" ? "-" : " "}
              </span>
              <code
                className={cn(
                  l.kind === "add" && "text-verdant-bright",
                  l.kind === "del" && "text-fail",
                  l.kind === "ctx" && "text-ash",
                  l.kind === "meta" && "text-soot"
                )}
              >
                {l.text}
              </code>
            </div>
          ))}
        </pre>
      </div>
    </figure>
  );
}
