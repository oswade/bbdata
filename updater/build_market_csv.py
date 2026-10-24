import sys, json, sqlite3, csv, re, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import billbot_core as core
import billbot_market as market

csv.field_size_limit(10**9)
load = Path(os.environ.get("BILLBOT_OUTPUT", "/tmp/load")); load.mkdir(parents=True, exist_ok=True)
src = sqlite3.connect(os.environ.get("BILLBOT_DB", "/tmp/latest.db")); src.row_factory = sqlite3.Row
retailers = {}
retailers.update(dict([('1st-energy','1st Energy'),('actewagl','ActewAGL'),('agl','AGL'),('alinta','Alinta Energy'),('amber','Amber Electric'),('blue-nrg','Blue NRG'),('covau','CovaU'),('diamond','Diamond Energy'),('dodo','Dodo Power & Gas'),('energy-locals','Energy Locals'),('energy-locals-urban','Energy Locals Urban'),('energyaustralia','EnergyAustralia'),('engie','ENGIE'),('erc-energy','ERC Energy'),('ergon','Ergon Energy'),('flipped','Flipped Energy'),('flow-power','Flow Power'),('future-x','Future X Power'),('gee-energy','GEE Energy'),('globird','GloBird Energy'),('io-energy','iO Energy'),('kogan','Kogan Energy'),('lumo','Lumo Energy'),('momentum','Momentum Energy'),('nectr','Nectr'),('next-business','Next Business Energy'),('origin','Origin Energy'),('ovo-energy','OVO Energy'),('perpetual','Perpetual Energy'),('powershop','Powershop'),('real-utilities','Real Utilities'),('red-energy','Red Energy'),('solstice','Solstice Energy'),('sumo-power','Sumo'),('tango','Tango Energy')]))

def expand(tokens):
    out = set()
    for t in tokens:
        if isinstance(t, dict):
            a = str(t.get("start") or t.get("from") or t.get("minimum") or ""); b = str(t.get("end") or t.get("to") or t.get("maximum") or "")
            if a.isdigit() and b.isdigit(): out.update(range(int(a), int(b) + 1)); continue
            t = t.get("postcode") or t.get("value") or ""
        s = str(t or "").strip()
        if s.isdigit(): out.add(int(s))
        elif "-" in s:
            a, b = (x.strip() for x in s.split("-", 1))
            if a.isdigit() and b.isdigit() and int(b) - int(a) < 10000: out.update(range(int(a), int(b) + 1))
    return sorted(out)

def pgarr(vals, text=False):
    if text:
        return "{" + ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in vals) + "}"
    return "{" + ",".join(str(v) for v in vals) + "}"

total = 0
with open(load / "energy.csv", "w", newline="") as f:
    w = csv.writer(f)
    for r in src.execute("select * from energy_plans where coalesce(is_active,1)=1"):
        summary, detail = core._plan_from_row(r)
        fuel = (r["fuel_type"] or "").upper()
        geo_src = detail if detail.get("geography") else summary
        if not core._fuel_matches(summary or detail, fuel): continue
        geo = core._flatten_geography(geo_src)
        stale = not core._detail_fresh_for_summary(summary, detail)
        key = "gasContract" if fuel == "GAS" else "electricityContract"
        elig = detail.get("eligibility") or summary.get("eligibility") or []
        slim = {"c": detail.get(key) or {}, "eligibility": elig, "fees": detail.get("fees") or [],
                "meteringCharges": detail.get("meteringCharges") or [], "displayName": detail.get("displayName")}
        ct = str((detail or summary).get("customerType") or "").upper()
        w.writerow([r["retailer_key"] + "|" + r["plan_id"], r["source_plan_id"] or r["plan_id"], fuel, retailers.get(r["retailer_name"], r["retailer_name"]) or "Energy retailer",
                    r["plan_name"] or "Unnamed plan", ct, r["application_uri"] or "", r["last_updated"] or "", r["source"] or "CDR",
                    r["tariff_type"] or "", bool(r["is_tou"]), bool(r["has_solar"]), bool(r["has_controlled_load"]),
                    r["daily_supply_cents"] if r["daily_supply_cents"] is not None else "", r["avg_usage_cents"] if r["avg_usage_cents"] is not None else "",
                    r["raw_usage_rates"] or "", pgarr(expand(geo["included_postcodes"])),
                    pgarr(sorted({core._compact(x) for x in geo["included_distributors"] if core._compact(x)}), True),
                    json.dumps(geo, separators=(",", ":")), json.dumps(slim, separators=(",", ":")), stale])
        total += 1
print("energy", total)

n = 0
with open(load / "savings.csv", "w", newline="") as f:
    w = csv.writer(f)
    seen = set()
    for r in src.execute("select raw_json from savings_products"):
        try: d = json.loads(r[0])
        except Exception: continue
        a = market.analyse_savings_product(d)
        if not a or not a["product_id"] or a["product_id"] in seen: continue
        seen.add(a["product_id"]); n += 1
        w.writerow([a["product_id"], a["brand"], a["name"], json.dumps(a, separators=(",", ":"))])
print("savings", n)

providers, plans = market.fetch_nbntracker_data()
n = 0
with open(load / "nbn.csv", "w", newline="") as f:
    w = csv.writer(f)
    for p in plans:
        if not market.is_consumer_nbn_plan(p) or str(p.get("technology") or "nbn").lower() != "nbn": continue
        tier = str(p.get("speed_tier") or "").upper()
        if tier not in market.NBN_VALID_SPEED_TIERS or p.get("monthly_price") is None or not p.get("name"): continue
        prov = providers.get(str(p.get("provider_slug") or ""), {})
        name = prov.get("name") or p.get("provider_slug")
        if not name: continue
        months = max(0, min(12, int(p.get("promo_months") or 0)))
        w.writerow([name, p["name"], tier, p["monthly_price"], p.get("promo_price") if p.get("promo_price") is not None else "", months, p.get("cis_url") or prov.get("website_url") or ""])
        n += 1
print("nbn", n)

state = {k: v for k, v in src.execute("select key, value from sync_state")}
man = json.load(open(os.environ.get("BILLBOT_MANIFEST", "/tmp/manifest.json")))
meta = {"generated_at": man.get("generated_at"), "counts": man.get("counts"), "snapshot_id": man.get("snapshot_id"), "nbn_source": "NBN Tracker (CC BY-SA 4.0)"}
json.dump(meta, open(load / "meta.json", "w"))
