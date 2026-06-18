Windows 看板打包说明

目标：
生成一个可发给同事的「RealtimeBoardViewer.exe」。

步骤：
1. 在 Windows 电脑上安装 Python 3.10+。
2. 解压 realtime_board_windows_viewer_build.zip。
3. 双击或右键 PowerShell 运行 build_windows_exe.ps1。
4. 生成文件在 dist/RealtimeBoardViewer_windows.zip。
5. 把 dist/RealtimeBoardViewer_windows.zip 发给同事。

同事使用：
1. 解压实时资金流看板_windows.zip。
2. 双击 RealtimeBoardViewer.exe。

默认连接地址：
http://192.168.1.15:8787/latest

如果你的 Mac 内网 IP 改了，修改 viewer_config_windows.json 里的 base_url 后重新打包。
