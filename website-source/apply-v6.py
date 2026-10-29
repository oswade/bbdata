"""Apply the tested v6 presentation patch AFTER the v5 UI overlays.
Fails loudly when the base app changes rather than silently dropping features.
"""
from pathlib import Path
import sys
root=Path(sys.argv[1])
def rewrite(path,edit):
    p=root/path; before=p.read_text(); after=edit(before)
    if before==after: raise RuntimeError(f"No changes made: {path}")
    p.write_text(after)

def compare(s):
    before='import { contractIsTou, feeLines, geographyMatches, norm, priceContract, solarRateLines, type Fuel, type Geo, type PriceRequest } from "./pricing";'
    assert before in s
    s=s.replace(before,before+'\nimport { displayTariffRates, type RateGroup } from "./rate-display";')
    assert 'fee_lines: string[]; solar_rates:' in s
    s=s.replace('fee_lines: string[]; solar_rates:', 'fee_lines: string[]; rate_groups: RateGroup[]; solar_rates:')
    assert 'raw_rates: r.raw_usage_rates ?? "", fee_lines:' in s
    return s.replace('raw_rates: r.raw_usage_rates ?? "", fee_lines:', 'raw_rates: r.raw_usage_rates ?? "", rate_groups: displayTariffRates(c, fuel), fee_lines:')

def controls(s):
    assert 'import { useEffect } from "react";' in s
    s=s.replace('import { useEffect } from "react";','import { useEffect, useState } from "react";\nimport { ChevronDown, FileUp } from "lucide-react";')
    assert '  const [p] = useProfile();\n  const gas = fuel === "GAS";' in s
    s=s.replace('  const [p] = useProfile();\n  const gas = fuel === "GAS";','  const [p] = useProfile();\n  const [expanded, setExpanded] = useState(false);\n  const gas = fuel === "GAS";')
    start=s.index('    <section className="border-b pb-6" aria-label={`${gas ? "Gas" : "Electricity"} usage`}>')
    end=s.index('      {source === "BILL" && note',start)
    header='''    <section className="border-b pb-4" aria-label={`${gas ? "Gas" : "Electricity"} usage`}>
      <button type="button" aria-expanded={expanded} aria-controls={`${fuel}-usage-settings`}
        onClick={() => setExpanded((prev) => !prev)}
        className="flex w-full items-center justify-between gap-3 rounded-xl border border-border/70 bg-card/70 px-3.5 py-3 text-left">
        <span className="min-w-0">
          <span className="block text-sm font-semibold">Your {gas ? "gas" : "electricity"} usage</span>
          <span className="mt-0.5 block text-xs text-muted-foreground tabular-nums">{usage.toLocaleString("en-AU")} {gas ? "MJ" : "kWh"}/year · {source === "BILL" ? "From bill" : "Household estimate"}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5 text-xs font-semibold text-primary">
          <FileUp className="h-3.5 w-3.5" /> {expanded ? "Close" : "Edit / upload"}
          <ChevronDown className={`h-4 w-4 transition-transform ${expanded ? "rotate-180" : ""}`} />
        </span>
      </button>
      {expanded && <div id={`${fuel}-usage-settings`} className="pt-4">
'''
    s=s[:start]+header+s[end:]
    assert '    </section>\n  );\n}' in s
    return s.replace('    </section>\n  );\n}', '      </div>}\n    </section>\n  );\n}')

def cards(s):
    assert '  const [open, setOpen] = useState(false);' in s
    s=s.replace('  const [open, setOpen] = useState(false);','  const [open, setOpen] = useState(false);\n  const [feesOpen, setFeesOpen] = useState(false);',1)
    target='''          {plan.raw_rates && <ul className="mt-2 space-y-1">{plan.raw_rates.split(" | ").map((r) => <li key={r} className="tabular">{r.replace(/c\\/(kWh|MJ)/, `c/${unit}`)}</li>)}</ul>}'''
    assert target in s, "Original ambiguous raw rate presentation is missing"
    new='''          {plan.rate_groups.length > 0 ? (
            <div className="mt-3 space-y-3" aria-label="Tariff tiers and time periods">
              {plan.rate_groups.map((group,i) => (
                <div key={group.heading + i}>
                  <p className="mb-1 text-xs font-semibold">{group.heading}</p>
                  <dl className="space-y-1">
                    {group.lines.map((line,j) => (
                      <div key={line.label + j} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-xs">
                        <dt className="min-w-0 flex-1 text-muted-foreground">{line.label}</dt>
                        <dd className="shrink-0 font-semibold tabular-nums">{line.price}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              ))}
              <p className="text-[10px] text-muted-foreground">Rates include GST. Usage tiers reset each stated billing period.</p>
            </div>
          ) : plan.raw_rates ? (
            <p className="mt-2 text-xs text-muted-foreground">Published c/{unit} rates have no reliable tier labels. Check the retailer's tariff document.</p>
          ) : null}'''
    s=s.replace(target,new)
    old='''          {plan.fee_lines.length > 0 && <div className="mt-3"><p className="text-xs font-semibold text-muted-foreground">Fees</p>{plan.fee_lines.map((s) => <p key={s} className="text-xs">{s}</p>)}</div>}'''
    assert old in s
    new='''          {plan.fee_lines.length > 0 && (
            <div className="mt-3 border-t border-border/70 pt-2">
              <button type="button" aria-expanded={feesOpen}
                onClick={() => setFeesOpen((prev) => !prev)}
                className="flex w-full items-center justify-between gap-2 py-2 text-left text-xs font-semibold text-muted-foreground">
                <span>Fees ({plan.fee_lines.length})</span>
                <ChevronDown className={`h-4 w-4 transition-transform ${feesOpen ? "rotate-180" : ""}`} />
              </button>
              {feesOpen && <div className="space-y-1 pb-1">{plan.fee_lines.map((item,i) => <p key={item+i} className="text-xs text-muted-foreground">{item}</p>)}</div>}
            </div>
          )}'''
    return s.replace(old,new)

rewrite('src/lib/billbot/compare.ts',compare)
rewrite('src/components/billbot/EnergyControls.tsx',controls)
rewrite('src/components/billbot/EnergyCompare.tsx',cards)
for name in ('v6-rate-display.ts','v6-rate-display.test.ts'):
    src=Path('website-source')/name
    dest=root/'src/lib/billbot'/name.removeprefix('v6-')
    dest.write_text(src.read_text())
assert (root/'src/lib/billbot/rate-display.ts').exists()
print("Applied usage accordion, nested fees accordion and structured tariff labels")
