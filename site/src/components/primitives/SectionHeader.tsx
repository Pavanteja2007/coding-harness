import { cn } from "@/lib/cn";

/**
 * Fraunces is for statements only (DESIGN.md §2.2): h2 qualifies.
 * Body measure 62-72ch; lead paragraphs 50-58ch. Never full-bleed text.
 */
export function SectionHeader({
  id,
  title,
  lead,
  align = "left",
  className,
}: {
  id?: string;
  title: React.ReactNode;
  lead?: React.ReactNode;
  align?: "left" | "center";
  className?: string;
}) {
  return (
    <header
      className={cn(
        "mb-10 flex flex-col gap-4",
        align === "center" && "items-center text-center",
        className
      )}
    >
      <h2 id={id} className="max-w-[22ch] text-h2 text-quench">
        {title}
      </h2>
      {lead ? (
        <p
          className={cn(
            "max-w-[58ch] text-lead text-ash",
            align === "center" && "mx-auto"
          )}
        >
          {lead}
        </p>
      ) : null}
    </header>
  );
}
