import { cn } from "@/lib/cn";

/**
 * Copper is ~4.6:1 - AA for large/UI, NOT for small body text (DESIGN.md §1.7).
 * So links are --ember (~7.1:1, AA at all sizes) and go copper only on hover,
 * where they are also underlined so colour is never the sole signal.
 */
export function Link({
  href,
  children,
  className,
  external,
  ...rest
}: React.AnchorHTMLAttributes<HTMLAnchorElement> & {
  href: string;
  children: React.ReactNode;
  external?: boolean;
}) {
  return (
    <a
      href={href}
      className={cn(
        "text-ember underline decoration-rule-hot underline-offset-4",
        "transition-colors duration-150 ease-forge hover:text-copper",
        className
      )}
      {...(external ? { target: "_blank", rel: "noreferrer noopener" } : {})}
      {...rest}
    >
      {children}
    </a>
  );
}
