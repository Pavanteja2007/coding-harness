import { ShaderHeroClient } from "@/components/motion/ShaderHeroClient";
import { SplitText } from "@/components/motion/SplitText";
import { ScrambleText } from "@/components/motion/ScrambleText";
import { MagneticButton } from "@/components/motion/MagneticButton";
import { Reveal } from "@/components/motion/Reveal";
import { SpecPlate } from "@/components/primitives/SpecPlate";
import { ButtonLink } from "@/components/primitives/Button";
import { CommandLine } from "@/components/product/CommandLine";
import { Terminal, type TerminalStep } from "@/components/product/Terminal";
import { REPOS } from "@/lib/content/repos";
import { INSTALL_CLONE } from "@/lib/content/install";

/**
 * §2 Hero — full viewport.
 *
 * The descent fills the frame and the copy sits over its left half, inside the
 * heavy side of a radial scrim. The right stays open so the fractal is never
 * obscured: that half IS the section.
 *
 * The terminal sits just below the fold as the proof object, so a reader who
 * scrolls one notch meets evidence rather than more claims.
 *
 * Decisions carried through:
 *   D3 - lowercase hairline spec plate, not an ALL-CAPS dot-joined kicker.
 *   D4 - the accent word is "real"; verdant is reserved for verified things.
 */

// jaraco/path's real run, read from the content layer so the figures cannot
// drift from README.md:102-106.
const JARACO = REPOS.find((r) => r.name === "jaraco/path")!;
const VERIFIED_LINE = [
  `${JARACO.calls} calls`,
  `$${JARACO.costUsd}`,
  `${JARACO.attempts} attempt`,
].join(" · ");

const STEPS: TerminalStep[] = [
  { kind: "cmd", text: 'vex fix --repo . --issue "mean() returns the sum, not the mean"' },
  { kind: "info", text: "plan", detail: "3 steps" },
  { kind: "info", text: "read mathutil.py", detail: "step 1" },
  { kind: "info", text: "apply fix", detail: "step 2" },
  { kind: "pass", text: "verify  target test", detail: "pass" },
  { kind: "pass", text: "verify  full suite", detail: "no regressions" },
  { kind: "verified", text: `verified  ${VERIFIED_LINE}` },
];

export function Hero() {
  return (
    <>
      <section className="relative isolate flex min-h-[100svh] items-center overflow-hidden">
        <ShaderHeroClient />

        <div className="relative z-10 mx-auto w-full max-w-[1200px] px-6 py-24">
          <div className="max-w-[40rem]">
            <Reveal>
              <SpecPlate
                items={["cli-first", "verifier-gated", "open source"]}
                className="mb-9"
              />
            </Reveal>

            <h1 className="mb-8 max-w-[15ch] text-h1 text-quench">
              <SplitText text="Fix real bugs." highlight="real" />
              <br />
              <SplitText text="Verified, not vibed." delayMs={24} />
            </h1>

            <Reveal delay={220}>
              <p className="mb-10 max-w-[52ch] text-lead text-ash">
                An open-source coding agent that plans a fix, edits inside a
                Docker sandbox, and runs your real test suite. It reports
                success only when the target test passes and nothing else
                regressed.
              </p>
            </Reveal>

            <Reveal delay={300}>
              <div className="flex flex-wrap items-center gap-3">
                <MagneticButton>
                  <ButtonLink variant="gilt" size="lg" href="#install">
                    Get started
                  </ButtonLink>
                </MagneticButton>
                <MagneticButton>
                  <ButtonLink variant="ghost" size="lg" href="#how-it-works">
                    How it works
                  </ButtonLink>
                </MagneticButton>
              </div>
            </Reveal>
          </div>
        </div>

        <div
          aria-hidden="true"
          className="absolute inset-x-0 bottom-8 z-10 flex justify-center"
        >
          <span className="font-mono text-mono text-soot">scroll</span>
        </div>
      </section>

      <section className="relative z-10 border-t border-rule bg-ink">
        <div className="mx-auto max-w-[1200px] px-6 py-16 lg:py-20">
          <div className="grid items-end gap-8 lg:grid-cols-12 lg:gap-10">
            <Reveal className="min-w-0 lg:col-span-7">
              <Terminal steps={STEPS} />
            </Reveal>

            <Reveal delay={120} className="min-w-0 lg:col-span-5">
              <div className="mb-3 flex items-center gap-3 font-mono text-mono text-smoke">
                <ScrambleText text="INSTALL" />
                <span aria-hidden="true" className="h-px flex-1 bg-rule" />
              </div>
              <CommandLine command={INSTALL_CLONE.command} wrap />
              <p className="mt-3 font-mono text-mono text-soot">
                No published package — clone and install editable.
              </p>
            </Reveal>
          </div>
        </div>
      </section>
    </>
  );
}
