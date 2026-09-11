import { cn } from "@/lib/cn";

/**
 * A raised surface. Depth = --slab + a 1px --rule hairline + the --etch bevel.
 * There is no box-shadow here and there must never be one (DESIGN.md §1.2).
 */
export function Panel({
  children,
  className,
  tone = "slab",
  as: Tag = "div",
}: {
  children: React.ReactNode;
  className?: string;
  tone?: "slab" | "char" | "basalt";
  as?: React.ElementType;
}) {
  return (
    <Tag
      className={cn(
        "rounded-lg border border-rule etch",
        tone === "slab" && "bg-slab",
        tone === "char" && "bg-char",
        tone === "basalt" && "bg-basalt",
        className
      )}
    >
      {children}
    </Tag>
  );
}

export function Card({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <Panel className={cn("p-5", className)}>{children}</Panel>;
}
