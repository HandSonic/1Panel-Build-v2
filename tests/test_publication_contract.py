"""Active publication payload-name contracts, without historical log dependencies."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import publication_contract as contracts


class PublicationPayloadNames(unittest.TestCase):
 def setUp(self):
  # A synthetic per-run contract isolates receipt tests from checkout state.
  self.runtime_patch=patch('resolved_contract.runtime_contract',return_value={'frontend_lock':{'kind':'source'}})
  self.runtime_patch.start();self.addCleanup(self.runtime_patch.stop)
 def test_expected_names_follow_accepted_inventory(self):
  names=contracts.expected_names('upstream-matrix','v2.99.0',accepted=['amd64'])
  self.assertEqual(sum(n.endswith('.tar.gz') for n in names),1)
  self.assertIn('build-manifest.json',names);self.assertIn('resolved-source.json',names)
 def test_resolved_lock_sidecar_is_part_of_exact_receipt_inventory(self):
  with patch('resolved_contract.runtime_contract',return_value={'frontend_lock':{'kind':'resolved'}}):
   names=contracts.expected_names('upstream-matrix','v2.99.0',accepted=['amd64'])
   self.assertIn('resolved-frontend-package-lock.json',names)
