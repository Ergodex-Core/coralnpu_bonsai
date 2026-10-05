import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec=importlib.util.spec_from_file_location('gen',Path(__file__).with_name('generate_cl.py'))
gen=importlib.util.module_from_spec(spec);spec.loader.exec_module(gen)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'root';self.root.mkdir()
        self.repo=self.base/'repo';(self.repo/'hdl/chisel/src/coralnpu').mkdir(parents=True)
        (self.repo/'hdl/chisel/src/coralnpu/BUILD').write_text('fixture')
        self.rtl=self.base/'bin/hdl/chisel/src/coralnpu';self.rtl.mkdir(parents=True)
        (self.rtl/'RvvCoreMiniAxi.sv').write_text('`include "defs.svh"\nmodule sync; endmodule\nmodule RvvCoreMiniAxi; sync u(); endmodule\n')
        with zipfile.ZipFile(self.rtl/'RvvCoreMiniAxi.zip','w') as z:z.writestr('defs.svh','// fixture')
        self.script=self.base/'ci/generate_cl.py';t=self.script.parent/'templates';t.mkdir(parents=True)
        (t/'hdk-links.json').write_text('{}')
        (t/'hbm_300mhz.tcl').write_text('@@CORAL_ROOT@@ @@HDK_ROOT@@')
        (self.root/'cl_coralnpu_hbm').mkdir();(self.root/'cl_coralnpu_hbm/previous').write_text('preserve')

    def fake_output(self,command,**kwargs):
        if command[1:]==['info','bazel-bin']:return str(self.base/'bin')+'\n'
        if 'rev-parse' in command:return '012345\n'
        return ''

    def generate(self,rtl=True):
        with patch.object(gen,'__file__',str(self.script)),patch.object(gen.subprocess,'check_output',side_effect=self.fake_output),patch.object(gen.shutil,'which',return_value='/fake/bazel'),patch.object(gen.subprocess,'run') as build:
            receipt=gen.generate(self.root,self.repo,self.base/'hdk','tag','bazel',self.rtl if rtl else None)
            return receipt,build.call_args_list

    def test_assembly_backup_and_rendering(self):
        receipt,calls=self.generate()
        self.assertFalse(calls)
        self.assertEqual(receipt['rtl_source'],'pre-generated override')
        self.assertTrue((self.root/'ci-cl-backups/tag/cl_coralnpu_hbm/previous').exists())
        self.assertIn('coral_private_sync u()', (self.root/'cl_coralnpu_hbm/rtl/RvvCoreMiniAxi.sv').read_text())
        self.assertNotIn('@@', (self.root/'hbm_300mhz.tcl').read_text())

    def test_bazel_is_called_before_assembly(self):
        receipt,calls=self.generate(False)
        self.assertEqual(calls[0].args[0],['bazel','build',gen.TARGET])
        self.assertEqual(receipt['rtl_source'],'Bazel')

    def test_invalid_zip_does_not_replace_previous_cl(self):
        with zipfile.ZipFile(self.rtl/'RvvCoreMiniAxi.zip','w') as z:z.writestr('../escape.sv','bad')
        with self.assertRaisesRegex(RuntimeError,'Unsafe RTL archive'):self.generate()
        self.assertTrue((self.root/'cl_coralnpu_hbm/previous').exists())
        self.assertFalse((self.root/'ci-cl-backups').exists())

    def test_missing_include_does_not_replace_previous_cl(self):
        with zipfile.ZipFile(self.rtl/'RvvCoreMiniAxi.zip','w') as z:z.writestr('different.svh','bad')
        with self.assertRaisesRegex(RuntimeError,'Missing generated include'):self.generate()
        self.assertTrue((self.root/'cl_coralnpu_hbm/previous').exists())


if __name__=='__main__':unittest.main()
