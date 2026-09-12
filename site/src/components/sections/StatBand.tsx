import { CountUp } from "@/components/motion/CountUp";
import { Reveal } from "@/components/motion/Reveal";
import { STATS } from "@/lib/content/stats";

/**
 * §3 Stat band.
 *
 * Four tiles, hairline dividers, NO cards and no box-shadow (DESIGN.md §5).
 * The big number is Fraunces - §2.2 sanctions the display face for "the big
 * number in a stat". Everything else here is Archivo or mono.
 *
 * Every figure carries its n, per §11 ("every number carries its n"), and the
 * asterisk links to the honesty note rather than hiding a caveat in a footer.
 */
export function StatBand() {
  return (
    <section
      aria-label="Measured results"
      className="border-y border-rule bg-basalt"
    >
      <div className="mx-auto max-w-[1200px] px-6">
        <div className="grid divide-y divide-rule sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4">
          {STATS.map((s, i) => (
            <Reveal
              key={s.label}
              delay={i * 60}
              className={[
                "px-0 py-10 sm:px-7",
                i > 0 ? "lg:border-l lg:border-rule" : "",
                i === 1 ? "sm:border-l sm:border-rule" : "",
                i === 3 ? "sm:border-l sm:border-rule" : "",
                i >= 2 ? "sm:border-t sm:border-rule lg:border-t-0" : "",
              ].join(" ")}
            >
              <div className="tnum mb-3 font-display text-[clamp(2.4rem,3.4vw,3.4rem)] leading-none text-quench">
                <CountUp
                  value={s.value}
                  decimals={s.decimals ?? 0}
                  prefix={s.prefix ?? ""}
                  suffix={s.suffix ?? ""}
                />
                {s.asterisk ? (
                  <a
                    href="#honesty"
                    aria-label="See the honesty note on how costs were measured"
                    className="align-super text-[0.42em] text-ox-bright transition-colors duration-150 ease-forge hover:text-ox-bright"
                  >
                    *
                  </a>
                ) : null}
              </div>

              <div className="mb-1.5 max-w-[24ch] text-body text-quench">
                {s.label}
              </div>
              <div className="max-w-[28ch] font-mono text-mono text-smoke">
                {s.sub}
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}
