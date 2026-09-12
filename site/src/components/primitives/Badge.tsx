import { cn } from "@/lib/cn";

/**
 * Badges are the ONE thing allowed to be a pill (DESIGN.md §6).
 * `state` is semantic and follows §1.4: patina means genuinely verified.
 */
export function Badge({
  children,
  state = "neutral",
  className,
}: {
  children: React.ReactNode;
  state?: "neutral" | "verified" | "heat" | "fail" | "warn";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5",
        "font-mono text-mono whitespace-nowrap",
        state === "neutral" && "border-rule bg-char text-ash",
        state === "verified" && "border-verdant/40 bg-verdant-dim text-verdant-bright",
        state === "heat" && "border-rule-hot bg-transparent text-gilt-bright",
        state === "warn" && "border-warn/40 bg-transparent text-warn",
        state === "fail" && "border-fail/40 bg-transparent text-fail",
        className
      )}
    >
      {children}
    </span>
  );
}
