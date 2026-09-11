/* TEMPORARY token-proof page - replaced in Task 7. */
const GROUND = ["ink", "basalt", "slab", "char", "forge"];
const HEAT = ["copper", "ember", "flare", "scorch"];
const PATINA = ["patina", "patina-bright"];
const TYPE = ["quench", "ash", "smoke", "soot"];
const SIGNAL = ["warn", "fail"];
const RULE = ["rule"];

function Swatch({ name }: { name: string }) {
  return (
    <div className="flex items-center gap-3">
      <div
        className="h-12 w-12 rounded-md border border-rule etch"
        style={{ background: `var(--color-${name})` }}
      />
      <code className="font-mono text-mono text-ash">--color-{name}</code>
    </div>
  );
}

function Group({ label, names }: { label: string; names: string[] }) {
  return (
    <section className="mb-10">
      <h3 className="mb-4 font-sans text-h3">{label}</h3>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {names.map((n) => (
          <Swatch key={n} name={n} />
        ))}
      </div>
    </section>
  );
}

export default function Page() {
  return (
    <main className="mx-auto max-w-[1200px] px-6 py-20">
      <h1 className="mb-4 text-h1">Fix real bugs.</h1>
      <p className="mb-3 max-w-[58ch] text-lead text-ash">
        Archivo lead paragraph. The quick brown fox jumps over the lazy dog.
      </p>
      <p className="mb-12 font-mono text-monolg text-ash">
        $ vex fix --repo . --issue &quot;mean() returns the sum&quot;
      </p>

      <Group label="Ground &amp; surfaces" names={GROUND} />
      <Group label="Heat" names={HEAT} />
      <Group label="Patina" names={PATINA} />
      <Group label="Type" names={TYPE} />
      <Group label="Signal" names={SIGNAL} />
      <Group label="Rules" names={RULE} />

      <section className="mb-10">
        <h3 className="mb-4 font-sans text-h3">Focus ring</h3>
        <button className="min-h-11 rounded-md bg-copper px-5 font-sans font-medium text-ink transition-colors duration-150 ease-forge hover:bg-ember">
          Tab to me
        </button>
      </section>
    </main>
  );
}
