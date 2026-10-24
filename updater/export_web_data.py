"""Publish browser-consumable, indexed JSON from the already validated GitHub release.

Run after run_in_cloud.py --publish. Does not change energy pricing rules.
No Supabase, Lovable, backend server, or third-party Python packages required.
"""
from __future__ import annotations
import argparse
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / '.cloud' / 'work' / 'snapshot.db.gz'
SOURCE_MANIFEST = ROOT / '.cloud' / 'work' / 'manifest.json'
OUTPUT = ROOT / 'public-data'


def write_json(root: Path, relative: str, payload: object):
    dest = root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')


def parse_array(value: str, is_text=False):
    if not value or value == '{}':
        return []
    if is_text:
        return next(csv.reader([value[1:-1]], escapechar='\\'))
    return [int(x) for x in value[1:-1].split(',') if x]


def number(v):
    return float(v) if v != '' else None


ENERGY_KEYS = ['id','source_plan_id','fuel','retailer','plan_name','customer_type',
    'application_uri','last_updated','source','tariff_type','is_tou','has_solar',
    'has_controlled_load','daily_supply_cents','avg_usage_cents','raw_usage_rates',
    'postcodes','distributors','geo','detail','stale']


def energy_row(values):
    d = dict(zip(ENERGY_KEYS, values, strict=True))
    for key in ('is_tou','has_solar','has_controlled_load','stale'):
        d[key] = d[key] == 'True'
    for key in ('daily_supply_cents','avg_usage_cents'):
        d[key] = number(d[key])
    d['postcodes'] = parse_array(d['postcodes'])
    d['distributors'] = parse_array(d['distributors'], True)
    for key in ('geo','detail'):
        d[key] = json.loads(d[key])
    return d


def chunk_sort(row):
    pcs = row['postcodes']
    return (0 if not pcs and not row['distributors'] else 1,
            min(pcs) // 100 if pcs else 100,
            row['distributors'][0].lower() if row['distributors'] else '', row['id'])


def write_energy(csv_path: Path, root: Path, fuel: str):
    csv.field_size_limit(10**9)
    rows = []
    with csv_path.open(newline='', encoding='utf-8') as source:
        for values in csv.reader(source):
            row = energy_row(values)
            if row['fuel'] == fuel and not row['stale'] and row['customer_type'] in ('RESIDENTIAL', ''):
                rows.append(row)
    if not rows:
        raise RuntimeError(f'No qualifying {fuel} plans')
    rows.sort(key=chunk_sort)
    index = {'format': 1, 'fuel': fuel, 'count': len(rows), 'chunks': [],
             'global': [], 'postcodes': defaultdict(set), 'distributors': defaultdict(set)}
    path = f'energy/{fuel}'
    chunks = []
    size = 0
    def flush():
        nonlocal chunks, size
        if not chunks: return
        i = len(index['chunks'])
        name = f'{path}/chunk-{i:04d}.json'
        write_json(root, name, chunks)
        index['chunks'].append(name)
        for row in chunks:
            pcs, ds = row['postcodes'], row['distributors']
            if not pcs and not ds: index['global'].append(i)
            for postcode in pcs: index['postcodes'][str(postcode).zfill(4)].add(i)
            for network in ds: index['distributors'][network.lower()].add(i)
        chunks, size = [], 0
    for row in rows:
        encoded = json.dumps(row, separators=(',', ':'), ensure_ascii=False)
        if chunks and (len(chunks) >= 75 or size + len(encoded) > 650_000): flush()
        chunks.append(row)
        size += len(encoded)
    flush()
    index['global'] = sorted(set(index['global']))
    index['postcodes'] = {k: sorted(v) for k,v in sorted(index['postcodes'].items())}
    index['distributors'] = {k: sorted(v) for k,v in sorted(index['distributors'].items())}
    write_json(root, f'{path}/index.json', index)
    return len(rows), f'{path}/index.json'


def export(database: Path, source_manifest: Path, target: Path):
    if not database.is_file() or not source_manifest.is_file():
        raise RuntimeError('Missing updater snapshot. Run the updater first.')
    source_meta = json.loads(source_manifest.read_text())
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    if digest != source_meta.get('sha256'):
        raise RuntimeError('Source database checksum mismatch; refusing to publish')
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        db = work / 'snapshot.db'
        with gzip.open(database, 'rb') as input_file, db.open('wb') as output_file:
            shutil.copyfileobj(input_file, output_file)
        with sqlite3.connect(db) as conn:
            if conn.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite integrity check failed')
        env = {**os.environ, 'BILLBOT_DB':str(db), 'BILLBOT_OUTPUT':str(work),
               'BILLBOT_MANIFEST':str(source_manifest)}
        subprocess.run([sys.executable, str(ROOT/'updater'/'build_market_csv.py')],
                       check=True, env=env)
        out = work / 'public-data'
        out.mkdir()
        counts = {}
        counts['energy_electricity'], electricity = write_energy(work/'energy.csv', out, 'ELECTRICITY')
        counts['energy_gas'], gas = write_energy(work/'energy.csv', out, 'GAS')
        nbn = []
        with (work/'nbn.csv').open(newline='', encoding='utf-8') as f:
            for p in csv.reader(f):
                nbn.append({'provider':p[0], 'plan':p[1], 'speed_tier':p[2],
                  'monthly_price':number(p[3]), 'promo_monthly_price':number(p[4]),
                  'promo_months':int(p[5]), 'url':p[6]})
        savings = []
        with (work/'savings.csv').open(newline='', encoding='utf-8') as f:
            for p in csv.reader(f): savings.append(json.loads(p[3]))
        counts['nbn'] = len(nbn)
        counts['savings'] = len(savings)
        if sum(counts[k] for k in ('energy_electricity','energy_gas')) < 1000 or counts['savings'] < 10 or counts['nbn'] < 10:
            raise RuntimeError(f'Incomplete export {counts}; previous public files preserved')
        write_json(out,'nbn.json', nbn)
        write_json(out,'savings.json', savings)
        m = {'format':1,'snapshot_id':source_meta['snapshot_id'],
             'generated_at':source_meta['generated_at'], 'counts':counts,
             'files':{'energy_electricity':electricity,'energy_gas':gas,
                      'nbn':'nbn.json','savings':'savings.json'}}
        write_json(out,'manifest.json',m)
        if target.exists(): shutil.rmtree(target)
        shutil.copytree(out,target)
        print(json.dumps({'ok':True,**counts,'chunks_electricity':len(list((target/'energy'/'ELECTRICITY').glob('chunk-*.json')))},indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,default=SOURCE)
    parser.add_argument('--manifest',type=Path,default=SOURCE_MANIFEST)
    parser.add_argument('--output',type=Path,default=OUTPUT)
    args=parser.parse_args()
    export(args.db,args.manifest,args.output)
