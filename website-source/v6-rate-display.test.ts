import { describe, expect, it } from "vitest";
import { displayTariffRates } from "./rate-display";

describe("tariff tier labels use the original structured CDR data", () => {
  it("explains GloBird's daily 25 kWh steps", () => {
    const c={tariffPeriod:[{startDate:"01-01",endDate:"12-31",displayName:"Period",
      singleRate:{period:"P1D",rates:[{volume:25,unitPrice:"0.181"},{unitPrice:"0.216"}]}}]};
    expect(displayTariffRates(c,"ELECTRICITY")).toEqual([{heading:"Usage rates",lines:[
      {label:"First 25 kWh/day",price:"19.910c/kWh"},
      {label:"Above 25 kWh/day",price:"23.760c/kWh"},
    ]}]);
  });
  it("explains Alinta's two gas seasons and three daily price blocks", () => {
    const c={tariffPeriod:[
      {startDate:"05-01",endDate:"10-31",displayName:"Peak",singleRate:{period:"P1D",rates:[{volume:50,unitPrice:"0.0291"},{volume:50,unitPrice:"0.0258"},{unitPrice:"0.0202"}]}},
      {startDate:"11-01",endDate:"04-30",displayName:"Off Peak",singleRate:{period:"P1D",rates:[{volume:50,unitPrice:"0.028"},{volume:50,unitPrice:"0.0246"},{unitPrice:"0.0202"}]}}
    ]};
    expect(displayTariffRates(c,"GAS")).toEqual([
      {heading:"Peak (May–Oct)",lines:[
        {label:"First 50 MJ/day",price:"3.201c/MJ"},
        {label:"Next 50 MJ/day",price:"2.838c/MJ"},
        {label:"Above 100 MJ/day",price:"2.222c/MJ"}]},
      {heading:"Off Peak (Nov–Apr)",lines:[
        {label:"First 50 MJ/day",price:"3.080c/MJ"},
        {label:"Next 50 MJ/day",price:"2.706c/MJ"},
        {label:"Above 100 MJ/day",price:"2.222c/MJ"}]},
    ]);
  });
  it("labels actual TOU time windows, even if retailer calls all periods shoulder", () => {
    const c={tariffPeriod:[{startDate:"01-01",endDate:"12-31",rateBlockUType:"timeOfUseRates",timeOfUseRates:[
      {type:"SHOULDER",displayName:"Tariff 1",rates:[{unitPrice:"0.277"}],
       timeOfUse:[{days:["MON","TUE","WED","THU","FRI","SAT","SUN"],startTime:"16:00",endTime:"21:00"}]}
    ]}]};
    expect(displayTariffRates(c,"ELECTRICITY")[0]?.lines[0]).toEqual({label:"Tariff 1 · 16:00–21:00 daily",price:"30.470c/kWh"});
  });
});
