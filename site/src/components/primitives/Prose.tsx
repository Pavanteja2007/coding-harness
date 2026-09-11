import { cn } from "@/lib/cn";

/** Body copy at the sanctioned measure. 62-72ch (DESIGN.md §2.4). */
export function Prose({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("max-w-[68ch] text-body text-ash", className)}>
      {children}
    </div>
  );
}
