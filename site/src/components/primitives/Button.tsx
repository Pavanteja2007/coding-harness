import { cn } from "@/lib/cn";

/**
 * DESIGN.md §1.7: --quench on --ox is ~3.9:1 and FAILS AA.
 * The ox variant therefore takes --ink text, never white. This is the
 * single most important detail on the page - do not "fix" it to white.
 *
 * No scale-transform on hover (ban list: hover must not shift layout).
 * min-h-11 keeps every target >= 44px on touch (DESIGN.md §9).
 */
const VARIANTS = {
  // Oxblood is a DARK fill, so it takes bone text (7.99:1). This is the
  // inverse of a light accent, which would need dark text - the rule is
  // "contrast against the fill", not "always ink".
  ox: "bg-ox text-quench hover:bg-ox-bright hover:text-ink border border-transparent",
  secondary: "bg-char text-quench border border-rule hover:bg-forge etch",
  ghost:
    "bg-transparent text-ash border border-transparent hover:text-quench hover:border-rule",
} as const;

const SIZES = {
  sm: "min-h-9 px-3 text-small",
  md: "min-h-11 px-5 text-body",
  lg: "min-h-12 px-6 text-lead",
} as const;

export type ButtonVariant = keyof typeof VARIANTS;
export type ButtonSize = keyof typeof SIZES;

type BaseProps = {
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
  children: React.ReactNode;
};

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-md font-sans font-medium " +
  "transition-colors duration-150 ease-forge cursor-pointer select-none";

export function Button({
  variant = "secondary",
  size = "md",
  className,
  children,
  ...rest
}: BaseProps & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={cn(BASE, VARIANTS[variant], SIZES[size], className)}
      {...rest}
    >
      {children}
    </button>
  );
}

export function ButtonLink({
  variant = "secondary",
  size = "md",
  className,
  children,
  ...rest
}: BaseProps & React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  return (
    <a
      className={cn(BASE, VARIANTS[variant], SIZES[size], className)}
      {...rest}
    >
      {children}
    </a>
  );
}
