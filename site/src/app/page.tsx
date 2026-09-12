import { Hero } from "@/components/sections/Hero";
import { StatBand } from "@/components/sections/StatBand";
import { Thesis } from "@/components/sections/Thesis";
import { HowItWorks } from "@/components/sections/HowItWorks";
import { LayerHarness } from "@/components/sections/LayerHarness";
import { LayerExecution } from "@/components/sections/LayerExecution";
import { LayerRuntime } from "@/components/sections/LayerRuntime";
import { Reliability } from "@/components/sections/Reliability";
import { LayerMemory } from "@/components/sections/LayerMemory";
import { MultiRepo } from "@/components/sections/MultiRepo";
import {
  GetStarted,
  HonestByDesign,
  Closing,
} from "@/components/sections/Closing";

/**
 * The landing page.
 *
 * Section rhythm alternates ink / basalt so adjacent bands read as distinct
 * without a card or a box-shadow anywhere, and the 7/5 splits alternate
 * direction (§6 harness is 7/5, §7 execution mirrors it 5/7) so no two
 * consecutive sections share a rhythm.
 */
export default function Home() {
  return (
    <>
      <Hero />
      <StatBand />
      <Thesis />
      <HowItWorks />
      <LayerHarness />
      <LayerExecution />
      <LayerRuntime />
      <Reliability />
      <LayerMemory />
      <MultiRepo />
      <GetStarted />
      <HonestByDesign />
      <Closing />
    </>
  );
}
