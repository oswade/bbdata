"""BillBot v8: compact energy header and accessible controlled-load switch."""
from pathlib import Path
import sys
root=Path(sys.argv[1])
controls=root/'src/components/billbot/EnergyControls.tsx'
s=controls.read_text()
anchor='import { isValidPostcode } from "@/lib/billbot/postcode";'
assert anchor in s
s=s.replace(anchor, anchor+'\nimport { controlledLoadTogglePatch, DEFAULT_CONTROLLED_LOAD_KWH } from "@/lib/billbot/controlled-load";')
start=s.index('      {!gas && <>\n        <div className="mt-5 flex flex-wrap items-baseline justify-between gap-2">')
end=s.index('      <NetworkPicker fuel={fuel} />',start)
replacement='''      {!gas && (
        <div className="mt-5 flex items-center justify-between gap-4">
          <div className="min-w-0 flex-1">
            <p id="controlled-load-label" className="text-sm font-semibold">Controlled load</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {p.controlledLoad > 0
                ? <>On · {p.controlledLoad.toLocaleString("en-AU")} kWh/year</>
                : "Off · no separately metered usage"}
            </p>
            {p.controlledLoad > 0 && (
              <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
                {p.controlledLoad === DEFAULT_CONTROLLED_LOAD_KWH
                  ? "Uses an estimated 1,500 kWh/year. Upload your bill for an accurate figure."
                  : "Using the saved annual controlled-load figure. You can upload a bill to update it."}
              </p>
            )}
          </div>
          <button
            type="button"
            role="switch"
            aria-labelledby="controlled-load-label"
            aria-checked={p.controlledLoad > 0}
            onClick={() => updateProfile(controlledLoadTogglePatch(p, p.controlledLoad === 0))}
            className={"relative inline-flex h-7 w-12 shrink-0 items-center rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 " + (p.controlledLoad > 0 ? "bg-primary" : "bg-muted-foreground/40")}
          >
            <span aria-hidden="true" className={"pointer-events-none h-5 w-5 rounded-full bg-white shadow-sm transition-transform " + (p.controlledLoad > 0 ? "translate-x-5" : "translate-x-0.5")} />
          </button>
        </div>
      )}
'''
s=s[:start]+replacement+s[end:]
assert 'id="controlled-load"' not in s
controls.write_text(s)
header=root/'src/components/billbot/EnergyCompare.tsx'
s=header.read_text()
best='  const best = q.data ? [...q.data.verified, ...q.data.modelled].sort((a, b) => a.annual_cost - b.annual_cost)[0] : null;\n'
assert best in s
s=s.replace(best,'')
start=s.index('        {best && (')
end=s.index('      </Masthead>',start)
s=s[:start]+s[end:]
assert 'Cheapest for you' not in s
header.write_text(s)
profile=root/'src/lib/billbot/profile.ts'
s=profile.read_text()
assert '  controlledLoad: number; // kWh / year' in s
assert '  elecUsage: 4600, controlledLoad: 0, elecCurrentCost: null' in s
s=s.replace('  controlledLoad: number; // kWh / year','  controlledLoad: number; // kWh / year\n  controlledLoadLast: number; // preserve bill-based value when switched off')
s=s.replace('  elecUsage: 4600, controlledLoad: 0, elecCurrentCost: null','  elecUsage: 4600, controlledLoad: 0, controlledLoadLast: 0, elecCurrentCost: null')
profile.write_text(s)
(root/'src/lib/billbot/controlled-load.ts').write_text('''import type { Profile } from "./profile";
export const DEFAULT_CONTROLLED_LOAD_KWH = 1500;
export function controlledLoadTogglePatch(
  profile: Pick<Profile, "controlledLoad" | "controlledLoadLast">,
  checked: boolean,
): Pick<Profile, "controlledLoad" | "controlledLoadLast"> {
  if (checked) {
    const restored = profile.controlledLoad > 0
      ? profile.controlledLoad
      : profile.controlledLoadLast > 0 ? profile.controlledLoadLast : DEFAULT_CONTROLLED_LOAD_KWH;
    return { controlledLoad: restored, controlledLoadLast: restored };
  }
  return {
    controlledLoad: 0,
    controlledLoadLast: profile.controlledLoad > 0 ? profile.controlledLoad : profile.controlledLoadLast,
  };
}
''')
(root/'src/lib/billbot/controlled-load.test.ts').write_text('''import { describe, expect, it } from "vitest";
import { controlledLoadTogglePatch, DEFAULT_CONTROLLED_LOAD_KWH } from "./controlled-load";
describe("controlled load switch", () => {
  it("uses a labelled initial estimate", () => {
    expect(controlledLoadTogglePatch({controlledLoad:0,controlledLoadLast:0},true))
      .toEqual({controlledLoad:DEFAULT_CONTROLLED_LOAD_KWH,controlledLoadLast:DEFAULT_CONTROLLED_LOAD_KWH});
  });
  it("restores a bill-supplied figure after switching off and back on", () => {
    const off = controlledLoadTogglePatch({controlledLoad:2350,controlledLoadLast:1500},false);
    expect(off).toEqual({controlledLoad:0,controlledLoadLast:2350});
    expect(controlledLoadTogglePatch(off,true)).toEqual({controlledLoad:2350,controlledLoadLast:2350});
  });
  it("does not charge controlled load while off", () => {
    expect(controlledLoadTogglePatch({controlledLoad:0,controlledLoadLast:1500},false).controlledLoad).toBe(0);
  });
});
''')
print('Applied v8 compact masthead, controlled-load switch and tests')
