package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"
)

func messageBox(text string) {
	user32 := syscall.NewLazyDLL("user32.dll")
	proc := user32.NewProc("MessageBoxW")
	title, _ := syscall.UTF16PtrFromString("归档盒")
	body, _ := syscall.UTF16PtrFromString(text)
	proc.Call(0, uintptr(unsafe.Pointer(body)), uintptr(unsafe.Pointer(title)), 0x10)
}

func userLogPath() string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, "AppData", "Local")
	}
	dir := filepath.Join(base, "归档盒")
	_ = os.MkdirAll(dir, 0755)
	return filepath.Join(dir, "启动日志.txt")
}

func appendLog(text string) {
	f, err := os.OpenFile(userLogPath(), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.WriteString(text)
}

func main() {
	exe, err := os.Executable()
	if err != nil {
		messageBox("找不到程序所在的文件夹。")
		os.Exit(1)
	}
	root := filepath.Dir(exe)
	pythonw := filepath.Join(root, "runtime", "pythonw.exe")
	script := filepath.Join(root, "app", "main.py")
	workdir := filepath.Join(root, "app")

	fail := func(text string, code int) {
		appendLog(fmt.Sprintf(
			"启动失败: %s 退出码=%d\r\n%s\r\n",
			time.Now().Format("2006-01-02 15:04:05"), code, text))
		messageBox(text)
		os.Exit(code)
	}

	if _, err := os.Stat(pythonw); err != nil {
		fail("程序不完整，缺少 runtime\\pythonw.exe。\n请重新解压整个文件夹后再打开。", 2)
	}
	if _, err := os.Stat(script); err != nil {
		fail("程序不完整，缺少 app\\main.py。\n请重新解压整个文件夹后再打开。", 2)
	}

	appendLog(fmt.Sprintf(
		"启动时间: %s\r\n启动器: 归档盒.exe\r\nPython: %s\r\n程序: %s\r\n参数: %v\r\n",
		time.Now().Format("2006-01-02 15:04:05"), pythonw, script, os.Args[1:]))

	args := append([]string{script}, os.Args[1:]...)
	cmd := exec.Command(pythonw, args...)
	cmd.Dir = workdir
	cmd.Env = append(os.Environ(),
		"PYTHONUTF8=1",
		"PYTHONDONTWRITEBYTECODE=1",
		"PYTHONIOENCODING=utf-8",
		"ARCHIVEBOX_PORTABLE_ROOT="+root,
	)
	// 不要 HideWindow / CREATE_NO_WINDOW：隐藏子进程会让 Qt 窗口不可见。
	if err := cmd.Run(); err != nil {
		code := 1
		if exitErr, ok := err.(*exec.ExitError); ok {
			code = exitErr.ExitCode()
		}
		fail("归档盒启动失败。\n请查看 %LOCALAPPDATA%\\归档盒\\启动日志.txt。", code)
	}
	os.Exit(0)
}
