"""Native compilation checks the actual firmware address predicates."""
import ctypes as C
from pathlib import Path
import subprocess,tempfile,unittest
HERE=Path(__file__).resolve().parent
class AddressRangeTests(unittest.TestCase):
    def test_hbm_end_exclusive_does_not_wrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'test.c').write_text('#include "address_range.h"\nint range(unsigned p,unsigned n){return cm_address_range(p,n);}\nint overlap(unsigned a,unsigned na,unsigned b,unsigned nb){return cm_ranges_overlap(a,na,b,nb);}\n')
            for profile in ['ddr','hbm']:
                flags=['-DCM_HBM_PROFILE'] if profile=='hbm' else []
                subprocess.run(['cc','-std=c11','-shared','-fPIC','-Wall','-Wextra','-Werror',*flags,
                                '-iquote'+str(HERE),str(p/'test.c'),'-o',str(p/(profile+'.so'))],check=True)
                lib=C.CDLL(str(p/(profile+'.so')));lib.range.argtypes=[C.c_uint32]*2;lib.overlap.argtypes=[C.c_uint32]*4
                end=0x100000000 if profile=='hbm' else 0xa0000000
                self.assertEqual(lib.range(end-4,4),1)
                self.assertEqual(lib.range(end-4,8),0)
                self.assertEqual(lib.range(end-3,3),0)
                self.assertEqual(lib.range(end-4,0),0)
                self.assertEqual(lib.range(0x10000,4),0)
                self.assertEqual(lib.overlap(end-16,16,end-4,4),1)
                self.assertEqual(lib.overlap(end-16,12,end-4,4),0)
if __name__=='__main__':unittest.main()
