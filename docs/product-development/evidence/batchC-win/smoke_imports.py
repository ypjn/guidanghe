import json, os, sys, tempfile
root = r'''C:\Users\Lenovo\Desktop\归档盒-Windows-免安装\guidanghe\打包\_tools\gotmp\ArchiveBox Accept f5582929\归档盒-windows'''
sys.path.insert(0, os.path.join(root, 'app'))
os.environ['ARCHIVEBOX_APP_DIR'] = tempfile.mkdtemp(prefix='archivebox_accept_')
os.environ['ARCHIVEBOX_LOG_DIR'] = os.environ['ARCHIVEBOX_APP_DIR']
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import archivebox_dllpath
import backup_dialog, backup_service, volume_identity, main
from app_metadata import APP_VERSION
print(json.dumps({'imports_ok': True, 'version': APP_VERSION}, ensure_ascii=False))
