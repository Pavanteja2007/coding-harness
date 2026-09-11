/**
 * Stat-band figures. Each carries its `n` per DESIGN.md §11 ("every number
 * carries its n") and the repo file it was verified against.
 */
export type Stat = {
  value: number;
  prefix?: string;
  suffix?: string;
  decimals?: number;
  label: string;
  sub: string;
  source: string;
  /** Links to the HonestyNote. Only for figures the note actually qualifies. */
  asterisk?: boolean;
};

export const STATS: Stat[] = [
  {
    value: 2.59,
    decimals: 2,
    suffix: "×",
    label: "cheaper, same success",
    sub: "n=16, 100% in both arms",
    source: "RESULTS.md:83 — v4: OFF $0.1505 vs ON $0.0581",
    asterisk: true,
  },
  {
    value: 45,
    label: "concurrent tasks, 8 hard kills",
    sub: "45/45 success, 8/8 genuine resumes",
    source: "README.md:124-127",
  },
  {
    value: 3600,
    label: "tasks through the soak run",
    sub: "120 mid-run kills, 15/15 checks",
    source: "RESULTS.md:139-148",
  },
  {
    value: 24,
    suffix: "/24",
    label: "sandbox escape attacks held",
    sub: "plus 78 concurrent hostile runs, 0 findings",
    source: "CHANGELOG.md:69-71; INTERFACES.md:508-527",
  },
];
