import { ABLATION_ROWS, ABLATION_SOURCE } from "@/lib/content/ablation";
import { cn } from "@/lib/cn";

/**
 * The router ablation table.
 *
 * DESIGN.md §8 / MASTER_BUILD_PROMPT §5.2: tabular numerals, the adaptive rows
 * emphasised, horizontal scroll INSIDE its own container on mobile so the body
 * never scrolls sideways. Every figure is read from the content layer, which
 * cites README.md:58-63.
 *
 * The adaptive rows get --quench text and a hot rule; the always-expensive
 * baseline stays --ash. Cost and wall for the adaptive rows go ox - these
 * are >=16px, where the §1.7 ledger permits ox.
 */
function fmt(n: number) {
  return n.toLocaleString("en-US");
}

export function RouterTable({ className }: { className?: string }) {
  return (
    <figure className={cn("m-0", className)}>
      {/* Keyboard-scrollable: the table is wider than a phone. */}
      <div
        tabIndex={0}
        role="region"
        aria-label="Adaptive routing ablation results"
        className="overflow-x-auto rounded-lg border border-rule etch"
      >
        <table className="tnum w-full min-w-[720px] border-collapse text-left">
          <thead>
            <tr className="border-b border-rule bg-slab">
              {["Task set", "Arm", "Success", "Calls", "Tokens", "Cost", "Wall"].map(
                (h) => (
                  <th
                    key={h}
                    scope="col"
                    className="px-4 py-3 font-mono text-mono font-medium text-smoke"
                  >
                    {h}
                  </th>
                )
              )}
            </tr>
          </thead>
          <tbody>
            {ABLATION_ROWS.map((r, i) => {
              const adaptive = r.arm === "adaptive";
              return (
                <tr
                  key={i}
                  className={cn(
                    "border-b border-rule last:border-0",
                    adaptive ? "bg-ox/[0.04]" : ""
                  )}
                >
                  <th
                    scope="row"
                    className={cn(
                      "px-4 py-3 font-sans text-small font-normal",
                      adaptive ? "text-quench" : "text-ash"
                    )}
                  >
                    <span className="flex items-center gap-2">
                      {adaptive ? (
                        <span
                          aria-hidden="true"
                          className="inline-block h-3 w-0.5 rounded bg-ox"
                        />
                      ) : (
                        <span
                          aria-hidden="true"
                          className="inline-block h-3 w-0.5 rounded bg-soot"
                        />
                      )}
                      {r.set}
                    </span>
                  </th>
                  <td
                    className={cn(
                      "px-4 py-3 font-mono text-mono",
                      adaptive ? "text-ox-bright" : "text-smoke"
                    )}
                  >
                    {r.arm}
                  </td>
                  <td className="px-4 py-3 font-mono text-mono text-ash">
                    {r.success}
                  </td>
                  <td className="px-4 py-3 font-mono text-mono text-ash">
                    {r.calls}
                  </td>
                  <td className="px-4 py-3 font-mono text-mono text-ash">
                    {fmt(r.tokens)}
                  </td>
                  <td
                    className={cn(
                      "px-4 py-3 font-mono text-mono",
                      adaptive ? "font-medium text-ox-bright" : "text-ash"
                    )}
                  >
                    ${r.costUsd.toFixed(4)}
                  </td>
                  <td
                    className={cn(
                      "px-4 py-3 font-mono text-mono",
                      adaptive ? "font-medium text-ox-bright" : "text-ash"
                    )}
                  >
                    {r.wallS}s
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <figcaption className="mt-3 font-mono text-mono text-smoke">
        Source: {ABLATION_SOURCE}
      </figcaption>
    </figure>
  );
}
