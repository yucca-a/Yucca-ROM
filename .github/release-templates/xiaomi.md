### 刷入方式

**线刷 (Fastbootd):**
1. 解压 ZIP
2. 手机打开开发者选项，打开USB调试，始终允许PC调试
3. 运行 `flash_all.bat` (清除数据) 或 `flash_all_except_storage.bat` (保留数据)
⚠️务必在Fastbootd模式下刷入，ROM内置的flash.bat脚本已加入条件判断

**卡刷 (Sideload):**
1. 进入 Recovery (TWRP/OrangeFox)
2. 直接点击安装，选择YuccaROM_xxx.zip后滑动滑块进行卡刷，也可使用`adb sideload YuccaROM_xxx.zip`

**工具:**
- `Tool.bat` — 进入 fastbootd / 安装 KernelSU.apk

> ⚠️ 自动构建产物，请自行测试。KernelSU LKM 需安装官方 APK 激活 root。
