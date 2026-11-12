import sys, os, sqlite3
sys.path.insert(0, r'C:\Users\Lenovo\Documents\Trae-CN\temp_20261110_01\archivebox_v17_windows_adapt_20261110\ArchiveBox_V1.7_Windows_Handoff_20261110\归档盒源码')
root = r'C:\Users\Lenovo\AppData\Local\Temp\_abx_full_round\runs\20261110_144732_c50af1\root'
db = os.path.join(root, '.organizer', 'history.db')
print('db exists:', os.path.exists(db))
conn = sqlite3.connect(db)
for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"):
    print(r[0])
print('---batches rows:---')
for r in conn.execute('SELECT * FROM batches'):
    print(r)
conn.close()
print('---via Storage.list_batches:---')
from storage import Storage
st = Storage(root)
bs = st.list_batches()
print('count:', len(bs))
for b in bs:
    print(' ', b)
st.close()
