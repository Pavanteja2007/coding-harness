import { cn } from "@/lib/cn";

/**
 * Section rhythm wrapper. DESIGN.md §6: vertical rhythm clamp(96px,14vh,200px),
 * and it must VARY - dense sections get less, statements get more. `tone`
 * alternates the ground between --ink and --basalt so adjacent sections read as
 * distinct bands without a box-shadow or a card in sight.
 */
export function Section({
  id,
  children,
  tone = "ink",
  bleed = false,
  className,
  labelledBy,
}: {
  id?: string;
  children: React.ReactNode;
  tone?: "ink" | "basalt";
  bleed?: boolean;
  className?: string;
  labelledBy?: string;
}) {
  return (
    <section
      id={id}
      aria-labelledby={labelledBy}
      className={cn(
        "scroll-mt-20 py-[clamp(72px,11vh,150px)]",
        tone === "basalt" && "bg-basalt",
        className
      )}
    >
      <div
        className={cn(
          "mx-auto px-6",
          bleed ? "max-w-[1400px]" : "max-w-[1200px]"
        )}
      >
        {children}
      </div>
    </section>
  );
}
