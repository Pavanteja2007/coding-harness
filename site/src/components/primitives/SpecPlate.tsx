import { cn } from "@/lib/cn";

/**
 * The hero eyebrow, restyled per decision D3.
 *
 * MASTER_BUILD_PROMPT asked for "CLI-FIRST · VERIFIER-GATED · OPEN SOURCE" -
 * an all-caps mono kicker with middle dots. frontend-design names all-caps
 * labels, mono data labels, middle-dot meta strings and labels-above-content
 * as the four commonest tells of a generated page, and the brief's own DoD
 * says the "does not look templated" test overrides every other checkbox.
 *
 * So: lowercase, hairline-separated, no middle-dot string. It reads as a spec
 * plate on a machine rather than a marketing kicker.
 *
 * Colour note: --color-smoke is ~2.9:1 and the DESIGN.md §1.7 ledger restricts
 * it to >=14px. --text-mono is 13px, so this uses --color-ash instead.
 */
export function SpecPlate({
  items,
  className,
}: {
  items: readonly string[];
  className?: string;
}) {
  return (
    <div
      className={cn(
        "inline-flex flex-wrap items-stretch overflow-hidden",
        "rounded-sm border border-rule bg-basalt/60 etch",
        className
      )}
    >
      {items.map((item, i) => (
        <span
          key={item}
          className={cn(
            "px-3 py-1.5 font-mono text-mono lowercase text-ash",
            i > 0 && "border-l border-rule"
          )}
        >
          {item}
        </span>
      ))}
    </div>
  );
}
