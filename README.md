# Yucca ROM

通过 GitHub Actions 云端自动化构建定制 Android ROM.

## 构建流程

通过调用私有 [YAKitchen](https://github.com/yucca-a/YAKit) 完成 ROM 解包、精简、修改后打包，上传 GoFile 并创建 Release。

## 修改内容

- 内置 KernelSU (LKM)
- 精简系统应用
- DATA 解密，去除 AVB 验证
- product / system / system_ext 打包为 ext4 格式，获取读写能力
- 线刷卡刷一体包，可直接进入 fastbootd 模式双击 flash.bat 刷机，也可通过 TWRP 直接安装

## 致谢

- OTA 接口参考: [Updater-KMP](https://github.com/YuKongA/Updater-KMP) by [YuKongA](https://github.com/YuKongA)
- payload 解包: [payload-dumper](https://github.com/5ec1cff/payload-dumper) by [5ec1cff](https://github.com/5ec1cff)

## 许可证

[GPL-3.0](LICENSE)
