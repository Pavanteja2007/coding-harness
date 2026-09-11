import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * Lucide wrapper. 1.5px stroke, 20/24px only (DESIGN.md §8).
 * Icons are --color-smoke at rest, --color-copper when the parent is active.
 * Never an emoji.
 */
export function Icon({
  as: Glyph,
  size = 20,
  className,
}: {
  as: LucideIcon;
  size?: 20 | 24;
  className?: string;
}) {
  return (
    <Glyph
      size={size}
      strokeWidth={1.5}
      aria-hidden="true"
      className={cn("shrink-0 text-smoke", className)}
    />
  );
}
