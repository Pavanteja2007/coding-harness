import { TriangleAlert } from "lucide-react";
import { HONESTY_NOTE } from "@/lib/content/ablation";
import { cn } from "@/lib/cn";

/**
 * The honesty note.
 *
 * MASTER_BUILD_PROMPT §5.4 requires this VERBATIM, shipped WITH the numbers.
 * DESIGN.md §11: "The honesty note is a designed component, not fine print."
 *
 * So it is deliberately NOT small grey text at the bottom of the page: it gets
 * a warn-toned rule, a real icon, and sits directly beside the table it
 * qualifies. The text comes from the content layer and must not be edited,
 * paraphrased, or shortened here.
 */
export function HonestyNote({ className }: { className?: string }) {
  return (
    <aside
      id="honesty"
      aria-label="How these numbers were measured"
      className={cn(
        "scroll-mt-24 rounded-lg border border-rule border-l-2 border-l-warn bg-char p-6 etch",
        className
      )}
    >
      <div className="mb-3 flex items-center gap-2.5">
        <TriangleAlert
          size={20}
          strokeWidth={1.5}
          aria-hidden="true"
          className="shrink-0 text-warn"
        />
        <h3 className="font-mono text-mono text-warn">
          How these numbers were measured
        </h3>
      </div>
      <p className="max-w-[68ch] text-small leading-relaxed text-ash">
        {HONESTY_NOTE}
      </p>
    </aside>
  );
}
