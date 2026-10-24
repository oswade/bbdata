"""Run with: python -m unittest updater.test_export_web_data"""
from __future__ import annotations
import csv
import json
from pathlib import Path
import tempfile
import unittest

from updater.export_web_data import write_energy


class ExportDataTests(unittest.TestCase):
    def row(self, plan_id, pc, dist, *, fuel='ELECTRICITY', stale=False, customer='RESIDENTIAL'):
        return [plan_id, plan_id, fuel, 'Example Energy', 'Starter', customer,
            'https://example.com', '2026-10-24', 'CDR', 'SINGLE_RATE', 'False','False','False',
            '100.0','20.0','20c/kWh', '{'+str(pc)+'}' if pc else '{}',
            '{"'+dist+'"}' if dist else '{}', json.dumps({
                'included_postcodes':[str(pc)] if pc else [],
                'excluded_postcodes':[], 'included_distributors':[dist] if dist else [], 'excluded_distributors':[]}),
            json.dumps({'c': {'tariffPeriod':[]},'eligibility':[]}), 'True' if stale else 'False']

    def test_indexes_and_filters_match_expected_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            csv_file=root/'energy.csv'
            rows=[self.row('local',3168,'United Energy'),self.row('network',None,'Ausgrid'),
                self.row('national',None,None),self.row('interstate',2000,'Ausgrid'),
                self.row('stale',3168,None,stale=True),
                self.row('business',3168,None,customer='BUSINESS'),
                self.row('gas',3168,None,fuel='GAS')]
            with csv_file.open('w',newline='') as file: csv.writer(file).writerows(rows)
            count,indexpath=write_energy(csv_file,root,'ELECTRICITY')
            self.assertEqual(count,4)
            idx=json.loads((root/indexpath).read_text())
            self.assertEqual(idx['format'],1)
            self.assertIn('3168',idx['postcodes'])
            self.assertIn('2000',idx['postcodes'])
            self.assertEqual(len(idx['global']),1)
            self.assertIn('ausgrid',idx['distributors'])
            assert all((root/p).exists() for p in idx['chunks'])

if __name__=='__main__': unittest.main()
