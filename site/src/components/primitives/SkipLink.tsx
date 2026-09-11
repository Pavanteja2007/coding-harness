import { cn } from "@/lib/cn";

/**
 * First element in tab order (DESIGN.md §9). Visually hidden until focused.
 */
export function SkipLink({ href = "#main" }: { href?: string }) {
  return (
    <a
      href={href}
      className={cn(
        "absolute left-4 top-4 z-[100] -translate-y-24 rounded-md",
        "bg-copper px-4 py-2 font-sans font-medium text-ink",
        "transition-transform duration-150 ease-forge",
        "focus-visible:translate-y-0"
      )}
    >
      Skip to content
    </a>
  );
}
