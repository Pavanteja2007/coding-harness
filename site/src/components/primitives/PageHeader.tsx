import { Reveal } from "@/components/motion/Reveal";
import { cn } from "@/lib/cn";

/**
 * Shared header for the non-landing routes.
 *
 * Those pages have no shader, so they need their own way of establishing
 * weight at the top. A single hairline rule plus generous space does it —
 * consistent with the rest of the system, which builds depth from rules
 * rather than from surfaces.
 */
export function PageHeader({
  eyebrow,
  title,
  lead,
  children,
  className,
}: {
  eyebrow: string;
  title: React.ReactNode;
  lead?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn("border-b border-rule bg-basalt", className)}
    >
      <div className="mx-auto max-w-[1200px] px-6 pb-16 pt-20 lg:pb-20 lg:pt-28">
        <Reveal>
          <div className="mb-6 flex items-center gap-3">
            <span className="font-mono text-mono uppercase tracking-[0.16em] text-ox-bright">
              {eyebrow}
            </span>
            <span aria-hidden="true" className="h-px w-16 bg-rule-hot" />
          </div>
        </Reveal>

        <Reveal delay={60}>
          <h1 className="mb-6 max-w-[18ch] text-h1 text-quench">{title}</h1>
        </Reveal>

        {lead ? (
          <Reveal delay={110}>
            <p className="max-w-[62ch] text-lead text-ash">{lead}</p>
          </Reveal>
        ) : null}

        {children ? (
          <Reveal delay={160}>
            <div className="mt-8">{children}</div>
          </Reveal>
        ) : null}
      </div>
    </header>
  );
}
