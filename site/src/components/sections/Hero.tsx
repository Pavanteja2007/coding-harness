import { ShaderHeroClient } from "@/components/motion/ShaderHeroClient";
import { SplitText } from "@/components/motion/SplitText";
import { SpecPlate } from "@/components/primitives/SpecPlate";
import { ButtonLink } from "@/components/primitives/Button";
import { CommandLine } from "@/components/product/CommandLine";
import { Terminal, type TerminalStep } from "@/components/product/Terminal";
import { REPOS } from "@/lib/content/repos";
import { INSTALL_CLONE } from "@/lib/content/install";

/**
 * §2 Hero.
 *
 * Layout is asymmetric (7/5 at lg), NOT centred - DESIGN.md §6 reserves
 * full-width centred blocks for genuine statements and caps them at 3 per page.
 * The terminal is the proof object and sits to the right, per §4 §2.
 *
 * Two decisions from the brainstorming pass are baked in here:
 *   D3 - the eyebrow is a lowercase, hairline-separated spec plate, not an
 *        ALL-CAPS dot-joined kicker (frontend-design names that combination as
 *        one of the commonest tells of a generated page).
 *   D4 - the copper word is "real", not "Verified". Patina owns "verified".
 */

// The terminal's closing line quotes jaraco/path's REAL run. The figures are
// read from the content layer rather than typed here, so they stay traceable
// to README.md:102-106 and cannot drift from the source of truth.
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
    <section className="relative isolate overflow-hidden">
      <ShaderHeroClient />

      <div className="relative z-10 mx-auto max-w-[1200px] px-6 pb-24 pt-28 lg:pb-32 lg:pt-36">
        <div className="grid items-start gap-14 lg:grid-cols-12 lg:gap-10">
          {/* 7 columns - the statement */}
          <div className="lg:col-span-7">
            <SpecPlate
              items={["cli-first", "verifier-gated", "open source"]}
              className="mb-8"
            />

            <h1 className="mb-7 text-h1 text-quench">
              <SplitText text="Fix real bugs." highlight="real" />
              <br />
              <SplitText text="Verified, not vibed." delayMs={26} />
            </h1>

            <p className="mb-9 max-w-[54ch] text-lead text-ash">
              An open-source coding agent that plans a fix, edits inside a Docker
              sandbox, and runs your real test suite. It reports success only
              when the target test passes and nothing else regressed.
            </p>

            <CommandLine
              command={INSTALL_CLONE.command}
              wrap
              className="mb-8 max-w-[62ch]"
            />

            <div className="flex flex-wrap gap-3">
              <ButtonLink variant="copper" size="lg" href="#install">
                Get started
              </ButtonLink>
              <ButtonLink variant="ghost" size="lg" href="#how-it-works">
                How it works
              </ButtonLink>
            </div>
          </div>

          {/* 5 columns - the proof object */}
          <div className="lg:col-span-5 lg:pt-2">
            <Terminal steps={STEPS} />
          </div>
        </div>
      </div>
    </section>
  );
}
