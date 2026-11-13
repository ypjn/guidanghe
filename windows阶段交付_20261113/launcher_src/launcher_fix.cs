// 归档盒 Windows 免安装包 —— 修复版启动器源码
//
// 背景：原「归档盒.exe」（Go 编写）启动子进程时使用了隐藏窗口的方式，
// Windows 会把子进程第一次 ShowWindow 的显示请求覆盖成隐藏，
// 导致 runtime\pythonw.exe 里运行的归档盒主窗口一直不可见
// （进程存在、CPU 正常、桌面没有窗口）。
//
// 本启动器做三件事：
//   1. 以正常可见窗口方式启动 runtime\pythonw.exe app\main.py（不隐藏窗口）；
//   2. 校验 app\main.py 与 runtime\pythonw.exe 是否存在，缺失时弹窗说明；
//   3. 在包根目录写「启动日志.txt」（仅启动器信息，写不了也不影响启动）。
//
// 编译：build-launcher.ps1（使用 Windows 自带的 .NET Framework csc.exe）
// 验证：verify-window-visible.ps1
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

static class ArchiveBoxLauncher
{
    [STAThread]
    static int Main(string[] args)
    {
        string dir;
        try
        {
            dir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        }
        catch
        {
            dir = Path.GetDirectoryName(typeof(ArchiveBoxLauncher).Assembly.Location);
        }

        string pyw = Path.Combine(dir, @"runtime\pythonw.exe");
        string main = Path.Combine(dir, @"app\main.py");

        if (!File.Exists(main))
        {
            return Fail(dir, "程序不完整：缺少 app\\main.py。\n请重新解压整个文件夹后再打开。", 2);
        }
        if (!File.Exists(pyw))
        {
            return Fail(dir, "程序不完整：缺少 runtime\\pythonw.exe。\n请重新解压整个文件夹后再打开。", 2);
        }

        var psi = new ProcessStartInfo();
        psi.FileName = pyw;
        psi.Arguments = "\"" + main + "\"";
        psi.WorkingDirectory = Path.Combine(dir, "app");
        psi.UseShellExecute = false;
        try
        {
            psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        }
        catch
        {
            // 环境变量设置失败不阻止启动
        }

        // 关键：不设置 WindowStyle、不设置 CreateNoWindow、不使用
        // STARTF_USESHOWWINDOW + SW_HIDE —— 让 Qt 主窗口的第一次 ShowWindow 正常生效。
        // pythonw.exe 本身没有控制台窗口，不需要「隐藏窗口」来避免黑框。
        try
        {
            Process.Start(psi);
        }
        catch (Exception ex)
        {
            return Fail(dir, "归档盒启动失败。\n" + ex.Message, 1);
        }

        WriteLaunchLog(dir, pyw, main);
        return 0;
    }

    // 启动失败时：先写日志（含退出码），再弹窗。
    // 避免出现「0 字节日志 + 无窗口」这种无法区分原因的状态。
    static int Fail(string dir, string message, int code)
    {
        try
        {
            string log = Path.Combine(dir, "启动日志.txt");
            using (var fs = new FileStream(log, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
            using (var sw = new StreamWriter(fs, new UTF8Encoding(false)))
            {
                sw.Write("启动失败: " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss")
                    + " 退出码=" + code + "\r\n" + message.Replace("\n", "\r\n") + "\r\n");
            }
        }
        catch
        {
            // 日志写不了也要把错误显示给用户
        }
        MessageBox.Show(message, "归档盒启动失败",
            MessageBoxButtons.OK, MessageBoxIcon.Error);
        return code;
    }

    static void WriteLaunchLog(string dir, string pyw, string main)
    {
        try
        {
            string log = Path.Combine(dir, "启动日志.txt");
            string text =
                "启动时间: " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "\r\n" +
                "启动器: 启动归档盒.exe（修复版，正常显示窗口）\r\n" +
                "Python: " + pyw + "\r\n" +
                "程序: " + main + "\r\n";
            // 用 ReadWrite 共享方式打开：即使别的进程（如旧的隐藏实例）还占着这个文件，
            // 也不会因为共享冲突而写失败。
            using (var fs = new FileStream(log, FileMode.Create, FileAccess.Write, FileShare.ReadWrite))
            using (var sw = new StreamWriter(fs, new UTF8Encoding(false)))
            {
                sw.Write(text);
            }
        }
        catch
        {
            // 日志写不了不影响启动
        }
    }
}
