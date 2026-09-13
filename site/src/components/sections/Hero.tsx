import { ShaderHeroClient } from "@/components/motion/ShaderHeroClient";
import { SplitText } from "@/components/motion/SplitText";
import { ScrambleText } from "@/components/motion/ScrambleText";
import { MagneticButton } from "@/components/motion/MagneticButton";
import { Reveal } from "@/components/motion/Reveal";
import { ButtonLink } from "@/components/primitives/Button";
import { CommandLine } from "@/components/product/CommandLine";
import { Terminal, type TerminalStep } from "@/components/product/Terminal";
import { REPOS } from "@/lib/content/repos";
import { INSTALL_PIP, PACKAGE_NAME_NOTE } from "@/lib/content/install";

/**
 * §2 Hero.
 *
 * The install command is the hero's centre of gravity, not an afterthought
 * below the CTAs: this is a CLI tool, so the single most useful thing the page
 * can do is hand you the line you need. It sits in its own framed slab,
 * centred, at the largest mono size on the site.
 *
 * Behind it, the verification field — a live graph whose nodes flip from
 * oxblood to bone as verification waves sweep through, then decay. The command
 * is the one static object in a moving field, which is the whole point: the
 * graph churns, the guarantee expires, the command is what you actually hold.
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
          <div className="mx-auto flex max-w-[46rem] flex-col items-center text-center">
            <Reveal>
              <div className="mb-8 flex items-center gap-3 font-mono text-mono text-smoke">
                <span aria-hidden="true" className="h-px w-8 bg-rule-hot" />
                <ScrambleText text="cli-first" />
                <span aria-hidden="true" className="text-smoke">/</span>
                <ScrambleText text="verifier-gated" />
                <span aria-hidden="true" className="text-smoke">/</span>
                <ScrambleText text="open source" />
                <span aria-hidden="true" className="h-px w-8 bg-rule-hot" />
              </div>
            </Reveal>

            <h1 className="mb-7 max-w-[16ch] text-h1 text-quench">
              <SplitText text="Fix real bugs." highlight="real" />
              <br />
              <SplitText text="Verified, not vibed." delayMs={24} />
            </h1>

            <Reveal delay={220}>
              <p className="mb-11 max-w-[54ch] text-lead text-ash">
                An open-source coding agent that plans a fix, edits inside a
                Docker sandbox, and runs your real test suite. It reports
                success only when the target test passes and nothing else
                regressed.
              </p>
            </Reveal>

            {/* The install command: the hero's actual payload. */}
            <Reveal delay={300} className="w-full">
              <div className="mb-4 flex items-center justify-center gap-3">
                <span aria-hidden="true" className="h-px w-10 bg-rule" />
                <span className="font-mono text-mono text-smoke">
                  {PACKAGE_NAME_NOTE.text}
                </span>
                <span aria-hidden="true" className="h-px w-10 bg-rule" />
              </div>
              <CommandLine
                command={INSTALL_PIP.command}
                wrap
                prominent
                className="mx-auto w-full max-w-[44rem]"
              />
            </Reveal>

            <Reveal delay={380}>
              <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
                <MagneticButton>
                  <ButtonLink variant="ox" size="lg" href="#how-it-works">
                    How it works
                  </ButtonLink>
                </MagneticButton>
                <MagneticButton>
                  <ButtonLink variant="ghost" size="lg" href="/docs/quickstart">
                    Read the quickstart
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
          <span className="font-mono text-mono text-smoke">scroll</span>
        </div>
      </section>

      {/* The proof object, immediately below the fold. */}
      <section className="relative z-10 border-t border-rule bg-ink">
        <div className="mx-auto max-w-[1200px] px-6 py-16 lg:py-20">
          <div className="grid items-center gap-10 lg:grid-cols-12 lg:gap-12">
            <Reveal className="min-w-0 lg:col-span-5">
              <h2 className="mb-4 max-w-[16ch] text-h2 text-quench">
                One real run, start to finish.
              </h2>
              <p className="max-w-[46ch] text-body text-ash">
                This is jaraco/path — a repository vex had never seen. The
                figures are from the actual run, not an illustration.
              </p>
            </Reveal>
            <Reveal delay={120} className="min-w-0 lg:col-span-7">
              <Terminal steps={STEPS} />
            </Reveal>
          </div>
        </div>
      </section>
    </>
  );
}
