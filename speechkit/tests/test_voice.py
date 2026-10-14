# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Script contracts are independent of the expensive neural model."""
import importlib.util,tempfile,unittest
from pathlib import Path
sp=importlib.util.spec_from_file_location('voice',Path(__file__).resolve().parents[1]/'src/voice.py');m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
class VoiceTests(unittest.TestCase):
 def base(self):return {'schemaVersion':'1.0.0','segments':[{'id':'a','text':'Read the full text.'}]}
 def test_full_text_unchanged(self):
  x=self.base();self.assertEqual(m.validate_script(x),x)
 def test_empty(self):
  x=self.base();x['segments']=[]
  with self.assertRaises(ValueError):m.validate_script(x)
 def test_schema(self):
  x=self.base();x['schemaVersion']='9'
  with self.assertRaises(ValueError):m.validate_script(x)
 def test_duplicate(self):
  x=self.base();x['segments']*=2
  with self.assertRaises(ValueError):m.validate_script(x)
 def test_id_traversal(self):
  x=self.base();x['segments'][0]['id']='../escape'
  with self.assertRaises(ValueError):m.validate_script(x)
 def test_empty_display(self):
  x=self.base();x['segments'][0]['text']=' '
  with self.assertRaises(ValueError):m.validate_script(x)
 def test_empty_pronunciation(self):
  x=self.base();x['segments'][0]['spokenText']=''
  with self.assertRaises(ValueError):m.validate_script(x)
 def test_pronunciation_is_explicit(self):
  x=self.base();x['segments'][0].update(text='PDF',spokenText='P D F');self.assertEqual(m.validate_script(x)['segments'][0]['text'],'PDF')
 def test_absolute_model(self):
  with self.assertRaises(ValueError):m.inside(Path('.'),'/etc/passwd')
 def test_parent_model(self):
  with self.assertRaises(ValueError):m.inside(Path('.'),'../model')
 def test_atomic_json(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'r.json';m.save(p,{'ready':False});self.assertTrue(p.is_file());self.assertFalse(p.with_suffix('.json.tmp').exists())
if __name__=='__main__':unittest.main()
