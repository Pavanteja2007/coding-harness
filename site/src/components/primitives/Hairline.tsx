import { cn } from "@/lib/cn";

/** A 1px rule. Depth in this system is hairlines, never shadows. */
export function Hairline({
  tone = "rule",
  className,
  vertical = false,
}: {
  tone?: "rule" | "soft" | "hot";
  className?: string;
  vertical?: boolean;
}) {
  return (
    <div
      role="presentation"
      className={cn(
        vertical ? "w-px self-stretch" : "h-px w-full",
        tone === "rule" && "bg-rule",
        tone === "soft" && "bg-rule-soft",
        tone === "hot" && "bg-rule-hot",
        className
      )}
    />
  );
}
