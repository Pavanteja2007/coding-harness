import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { ButtonLink } from "@/components/primitives/Button";
import { InstallTabs } from "@/components/product/InstallTabs";
import {
  INSTALL_PIP,
  INSTALL_CLONE,
  INSTALL_NO_INSTALL,
  INSTALL_ALIAS,
  PACKAGE_NAME_NOTE,
} from "@/lib/content/install";
import { site } from "@/lib/site";
import { ADVERSARIAL } from "@/lib/content/reliability";

/**
 * §13 Get started, §14 Honest by design, §15 Closing.
 *
 * The closing is one of the at-most-three full-width centred blocks DESIGN.md
 * §6 permits - the hero is another, and nothing else on the page centres.
 */
const STACK = [
  "Python 3.10",
  "litellm",
  "Docker",
  "tree-sitter",
  "MCP Python SDK",
  "argparse",
  "stdlib dashboard",
];

export function GetStarted() {
  return (
    <Section id="install" labelledBy="install-h" tone="ink">
      <div className="grid gap-12 lg:grid-cols-12 lg:gap-14">
        <div className="min-w-0 lg:col-span-5">
          <Reveal>
            <h2 id="install-h" className="mb-5 max-w-[14ch] text-h2 text-quench">
              Point it at a failing test.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="mb-6 max-w-[52ch] text-lead text-ash">
              Bring your own endpoint and key. The offline demo needs neither,
              and runs the real loop, the real gate, and the real git output
              against a scripted model.
            </p>
          </Reveal>
          <Reveal delay={110}>
            <p className="mb-8 max-w-[52ch] text-small text-smoke">
              {PACKAGE_NAME_NOTE.text}
            </p>
          </Reveal>
          <Reveal delay={150}>
            <div className="flex flex-wrap gap-3">
              <ButtonLink
                variant="ox"
                size="lg"
                href={site.repo}
                target="_blank"
                rel="noreferrer noopener"
              >
                View the source
              </ButtonLink>
              <ButtonLink variant="secondary" size="lg" href="/docs/quickstart">
                Read the quickstart
              </ButtonLink>
            </div>
          </Reveal>
        </div>

        <div className="min-w-0 lg:col-span-7">
          <Reveal delay={80}>
            <InstallTabs
              tabs={[
                {
                  id: "pip",
                  label: "pip",
                  command: INSTALL_PIP.command,
                  note: INSTALL_PIP.note,
                },
                {
                  id: "clone",
                  label: "from source",
                  command: INSTALL_CLONE.command,
                  note: INSTALL_CLONE.note,
                },
                {
                  id: "norun",
                  label: "run without installing",
                  command: INSTALL_NO_INSTALL.command,
                  note: INSTALL_NO_INSTALL.note,
                },
                {
                  id: "alias",
                  label: "legacy alias",
                  command: INSTALL_ALIAS.command,
                  note: INSTALL_ALIAS.note,
                },
              ]}
            />
          </Reveal>
        </div>
      </div>
    </Section>
  );
}

export function HonestByDesign() {
  return (
    <Section id="honest" labelledBy="honest-h" tone="basalt">
      <div className="grid gap-12 lg:grid-cols-12 lg:gap-14">
        <div className="min-w-0 lg:col-span-6">
          <Reveal>
            <h2 id="honest-h" className="mb-5 max-w-[16ch] text-h2 text-quench">
              The discipline is the product.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="mb-5 max-w-[56ch] text-lead text-ash">
              A harness that reports its own success is worth exactly as much as
              its willingness to report failure. So the verifier gate is
              absolute, the negative results stay on the page, and every number
              here carries the sample size it was measured at.
            </p>
          </Reveal>
          <Reveal delay={110}>
            <p className="max-w-[56ch] text-body text-ash">
              {ADVERSARIAL.text} That is on the page for the same reason the
              cost wins are.
            </p>
          </Reveal>
        </div>

        <div className="min-w-0 lg:col-span-6">
          <Reveal delay={80}>
            <blockquote className="mb-8 border-l-2 border-rule-hot pl-6">
              <p className="font-display text-h3 leading-snug text-quench">
                No logos. No testimonials. The credibility is the numbers.
              </p>
            </blockquote>
          </Reveal>

          <Reveal delay={130}>
            <div className="mb-3 font-mono text-mono text-smoke">built with</div>
            <div className="flex flex-wrap gap-2">
              {STACK.map((s) => (
                <span
                  key={s}
                  className="rounded-sm border border-rule bg-slab px-3 py-1.5 font-mono text-mono text-ash"
                >
                  {s}
                </span>
              ))}
            </div>
          </Reveal>

          <Reveal delay={170}>
            <p className="mt-6 max-w-[56ch] text-small text-smoke">
              Deferred, and said plainly rather than implied: SWE-bench numbers,
              multi-language support, and a published package. None of those
              exist yet.
            </p>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}

export function Closing() {
  return (
    <Section id="closing" tone="ink" className="text-center">
      <Reveal>
        <h2 className="mx-auto mb-6 max-w-[18ch] text-h2 text-quench">
          Fix a real bug with it.
        </h2>
      </Reveal>
      <Reveal delay={60}>
        <p className="mx-auto mb-9 max-w-[52ch] text-lead text-ash">
          Clone it, point it at a failing test, and read the rationale it writes
          when the suite goes green.
        </p>
      </Reveal>
      <Reveal delay={110}>
        <div className="flex flex-wrap justify-center gap-3">
          <ButtonLink
            variant="ox"
            size="lg"
            href={site.repo}
            target="_blank"
            rel="noreferrer noopener"
          >
            Get the source
          </ButtonLink>
          <ButtonLink variant="ghost" size="lg" href="/docs">
            Read the docs
          </ButtonLink>
        </div>
      </Reveal>
    </Section>
  );
}
