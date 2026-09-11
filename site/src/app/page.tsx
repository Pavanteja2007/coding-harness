import { ShaderHeroClient } from "@/components/motion/ShaderHeroClient";
import { SpecPlate } from "@/components/primitives/SpecPlate";
import { ButtonLink } from "@/components/primitives/Button";

/* Shell only - the real hero, stat band, and 13 further sections are Tasks 7-12. */
export default function Home() {
  return (
    <>
      <section className="relative isolate flex min-h-[86vh] flex-col justify-center overflow-hidden">
        <ShaderHeroClient />

        <div className="relative z-10 mx-auto w-full max-w-[1200px] px-6 py-32">
          <SpecPlate
            items={["cli-first", "verifier-gated", "open source"]}
            className="mb-8"
          />

          <h1 className="mb-6 max-w-[16ch] text-h1 text-quench">
            Fix <span className="text-copper">real</span> bugs.
            <br />
            Verified, not vibed.
          </h1>

          <p className="mb-10 max-w-[54ch] text-lead text-ash">
            An open-source coding agent that plans a fix, edits inside a Docker
            sandbox, and runs your real test suite. It reports success only when
            the target test passes and nothing else regressed.
          </p>

          <div className="flex flex-wrap gap-3">
            <ButtonLink variant="copper" size="lg" href="#install">
              Install
            </ButtonLink>
            <ButtonLink variant="ghost" size="lg" href="#how-it-works">
              How it works
            </ButtonLink>
          </div>
        </div>
      </section>
    </>
  );
}
