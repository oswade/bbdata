/** Display tariff structure without throwing away the CDR tiers, seasons or TOU windows.
 * Amounts in CDR are ex-GST; prices in the app are inclusive of 10% GST.
 */
import { GST } from "./pricing";
/* eslint-disable @typescript-eslint/no-explicit-any */
type Obj = any;
export interface RateGroup { heading: string; lines: { label: string; price: string }[] }
const arr = (value: any): Obj[] => (Array.isArray(value) ? value : value && typeof value === "object" ? [value] : []).filter((r): r is Obj => !!r && typeof r === "object" && !Array.isArray(r));
const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function season(period: Obj): string {
  const a = String(period.startDate || "").match(/^(\d{1,2})-(\d{1,2})$/), b = String(period.endDate || "").match(/^(\d{1,2})-(\d{1,2})$/);
  if (!a || !b || (a[0] === "01-01" && b[0] === "12-31")) return "";
  const start = +a[1]!, end = +b[1]!;
  return start > 0 && start <= 12 && end > 0 && end <= 12 ? `${months[start-1]}–${months[end-1]}` : "";
}
const number = (v: any): number | null => { if (v == null || v === "") return null; const n=Number(v); return Number.isFinite(n) ? n : null; };
const fmt = (n: number) => Number.isInteger(n) ? String(n) : String(Number(n.toFixed(3)));
function interval(raw: any): string {
  const p = String(raw || "").toUpperCase();
  return p === "P1D" ? "day" : p === "P1W" ? "week" : p === "P1M" ? "month" : p === "P1Y" ? "year" : p ? `billing period (${p})` : "billing period";
}
function lines(raw: any, unit: string, period: any, base="Usage"): RateGroup["lines"] {
  let used = 0; const rates = arr(raw);
  return rates.flatMap((r,i) => {
    const v = number(r.unitPrice);
    if (v == null || v < 0) return [];
    const vol = number(r.volume);
    let label=base;
    if (rates.length > 1) {
      if (vol != null && vol > 0) { label = `${i ? "Next" : "First"} ${fmt(vol)} ${unit}/${interval(period)}`; used += vol; }
      else label = used > 0 ? `Above ${fmt(used)} ${unit}/${interval(period)}` : `Remaining ${unit}/${interval(period)}`;
    }
    return [{ label, price: `${(v*100*GST).toFixed(3)}c/${unit}` }];
  });
}
function timeWindows(value: any): string {
  return arr(value).map((x) => {
    if (!x.startTime || !x.endTime) return "";
    const days = Array.isArray(x.days) ? x.days : [];
    return `${x.startTime}–${x.endTime}${days.length === 7 ? " daily" : days.length ? " " + days.join(", ") : ""}`;
  }).filter(Boolean).join("; ");
}
export function displayTariffRates(contract: any, fuel: "ELECTRICITY" | "GAS"): RateGroup[] {
  if (!contract || typeof contract !== "object") return [];
  const unit=fuel === "GAS" ? "MJ" : "kWh"; const output: RateGroup[]=[];
  for(const p of arr(contract.tariffPeriod)) {
    const name=String(p.displayName || "").trim(), date=season(p);
    const base=name && name.toLowerCase()!=="period" ? name : "Usage rates";
    const heading=`${base}${date ? " ("+date+")" : ""}`;
    if(p.singleRate) {
      const line=lines(p.singleRate.rates, unit, p.singleRate.period || "P1Y");
      if(line.length) output.push({heading,lines:line});
    }
    const tou=arr(p.timeOfUseRates);
    if(tou.length) {
      const ls=tou.flatMap((band) => {
        const label=String(band.displayName || band.type || "Time of use").replace(/_/g," ");
        const window=timeWindows(band.timeOfUse || band.timeVariations);
        return lines(band.rates, unit, band.period || "P1Y", label).map((r) => ({
          ...r, label: [r.label === label ? label : label+" · "+r.label,window].filter(Boolean).join(" · ")
        }));
      });
      if(ls.length) output.push({heading:base==="Usage rates" ? `Time of use${date ? " ("+date+")" : ""}` : heading,lines:ls});
    }
  }
  for(const cl of arr(contract.controlledLoad)){
    if(cl.singleRate) { const ls=lines(cl.singleRate.rates,"kWh",cl.singleRate.period||"P1Y","Controlled load"); if(ls.length) output.push({heading:"Controlled load",lines:ls}); }
    const tou=arr(cl.timeOfUseRates);
    if(tou.length) {
      const ls=tou.flatMap(b=>lines(b.rates,"kWh",b.period||"P1Y",String(b.displayName||b.type||"Controlled load")));
      if(ls.length) output.push({heading:"Controlled load",lines:ls});
    }
  }
  return output;
}
