import { ArrowRight, Check, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/primitives/Badge";
import { Button, ButtonLink } from "@/components/primitives/Button";
import { Hairline } from "@/components/primitives/Hairline";
import { Icon } from "@/components/primitives/Icon";
import { Link } from "@/components/primitives/Link";
import { Card, Panel } from "@/components/primitives/Panel";
import { Prose } from "@/components/primitives/Prose";
import { SectionHeader } from "@/components/primitives/SectionHeader";
import { SkipLink } from "@/components/primitives/SkipLink";
import { SpecPlate } from "@/components/primitives/SpecPlate";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="mb-10 flex flex-col gap-4">
      <h3 className="font-mono text-mono uppercase tracking-[0.18em] text-smoke">
        {label}
      </h3>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </section>
  );
}

export default function Gallery() {
  return (
    <main id="main" className="mx-auto max-w-[1200px] px-6 py-20">
      <SkipLink />
      <h1 className="mb-10 text-h1">Primitives</h1>

      <Row label="Spec plate — D3, lowercase, hairline-separated">
        <SpecPlate items={["cli-first", "verifier-gated", "open source"]} />
      </Row>

      <Row label="Button — sizes">
        <Button variant="copper" size="sm">Copper sm</Button>
        <Button variant="copper" size="md">Copper md</Button>
        <Button variant="copper" size="lg">Copper lg</Button>
      </Row>

      <Row label="Button — variants">
        <Button variant="copper">Copper</Button>
        <Button variant="secondary">Secondary</Button>
        <Button variant="ghost">Ghost</Button>
        <ButtonLink variant="copper" href="#x">
          Link + icon <Icon as={ArrowRight} />
        </ButtonLink>
      </Row>

      <Row label="Button — focus ring (Tab to one)">
        <Button variant="copper">Focusable copper</Button>
        <Button variant="secondary">Focusable secondary</Button>
      </Row>

      <Row label="Badge — states (patina = genuinely verified only)">
        <Badge>neutral</Badge>
        <Badge state="verified">
          <Icon as={Check} size={20} className="text-patina-bright" />
          verified
        </Badge>
        <Badge state="heat">escalated</Badge>
        <Badge state="warn">
          <Icon as={TriangleAlert} size={20} className="text-warn" />
          caveat
        </Badge>
        <Badge state="fail">failed</Badge>
      </Row>

      <Row label="Hairline — tone">
        <div className="w-full">
          <Hairline />
          <div className="h-3" />
          <Hairline tone="soft" />
          <div className="h-3" />
          <Hairline tone="hot" />
        </div>
      </Row>

      <Row label="Panel / Card — etch bevel, no box-shadow">
        <Panel className="p-5">
          <p className="text-body text-ash">Panel on slab</p>
        </Panel>
        <Panel tone="char" className="p-5">
          <p className="text-body text-ash">Panel on char</p>
        </Panel>
        <Card>
          <p className="text-body text-ash">Card</p>
        </Card>
      </Row>

      <Row label="Link — ember at rest, copper on hover, always underlined">
        <Link href="#x">An inline documentation link</Link>
        <Link href="https://example.com" external>External link</Link>
      </Row>

      <section className="mb-10">
        <SectionHeader
          title="A section header renders in Fraunces."
          lead="The lead paragraph sits at 50-58ch so it never runs full-bleed, and uses Archivo rather than the display face."
        />
      </section>

      <section className="mb-10">
        <Prose>
          <p>
            Prose holds body copy at 62-72 characters. Copper is expensive
            because it is rare, so most of this page stays quiet and the accent
            lands where work is actually happening.
          </p>
        </Prose>
      </section>
    </main>
  );
}
