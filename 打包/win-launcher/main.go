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

func appendLog(path, text string) {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
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
	logPath := filepath.Join(root, "启动日志.txt")

	fail := func(text string, code int) {
		appendLog(logPath, fmt.Sprintf(
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

	appendLog(logPath, fmt.Sprintf(
		"启动时间: %s\r\n启动器: 归档盒.exe（正常显示窗口）\r\nPython: %s\r\n程序: %s\r\n",
		time.Now().Format("2006-01-02 15:04:05"), pythonw, script))

	cmd := exec.Command(pythonw, script)
	cmd.Dir = workdir
	cmd.Env = append(os.Environ(),
		"PYTHONUTF8=1",
		"PYTHONDONTWRITEBYTECODE=1",
		"PYTHONIOENCODING=utf-8",
	)
	// 不要 HideWindow，也不要 CREATE_NO_WINDOW。
	// pythonw 本身没有控制台。隐藏子进程会让 Qt 第一次 ShowWindow 一直不可见。
	if err := cmd.Run(); err != nil {
		code := 1
		if exitErr, ok := err.(*exec.ExitError); ok {
			code = exitErr.ExitCode()
		}
		fail("归档盒启动失败。\n同一文件夹里的「启动日志.txt」里有具体原因。", code)
	}
}
