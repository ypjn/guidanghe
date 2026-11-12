
import sys
sys.path.insert(0, r"C:\Users\Lenovo\Documents\Trae-CN\temp_20261110_01\archivebox_v17_windows_adapt_20261110\ArchiveBox_V1.7_Windows_Handoff_20261110\归档盒源码\tests")
from windows_harness import Results
r = Results("injected_false_success")
r.rec("I4", "故意失败但退出 0", "FAIL", "injected false success")
r.finalize(r"C:\Users\Lenovo\Documents\Trae-CN\temp_20261110_01\archivebox_v17_windows_adapt_20261110\ArchiveBox_V1.7_Windows_Handoff_20261110\归档盒源码\tests\_abx_selftest\runs\selftest_20261110_155206_1e484c\children\inj4_results.json")
print("EXIT_CODE=0")
sys.exit(0)
