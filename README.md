# Yucca ROM

自用的全自动定制 ROM 构建项目，基于 GitHub Actions 自动拉取小米、红米和三星的原厂固件，完成精简、补丁、KernelSU 修补、打包并发布到 GitHub Releases。

## 支持机型

| 入口 | 机型 | 说明 | 产物 |
| --- | --- | --- | --- |
| [小米 ROM](https://github.com/yucca-a/Yucca-ROM/actions/workflows/xiaomi.yml) | Xiaomi 17 / 15 / 14 系列，含列表中的 T 系列机型 | 自动拉取正式版 / Beta 固件，可选精简、注入 APK 和 KernelSU。系统三区可选 Official（默认，保留官方格式）、Ext4 或 F2FS。单机型支持自定义 OTA 链接 | Fastbootd / Recovery 两用 ZIP |
| [红米 ROM](https://github.com/yucca-a/Yucca-ROM/actions/workflows/redmi.yml) | REDMI K90 / K80 / Turbo 5 / Turbo 4 系列 | 与小米入口使用相同的构建选项 | Fastbootd / Recovery 两用 ZIP |
| [三星 ROM](https://github.com/yucca-a/Yucca-ROM/actions/workflows/samsung.yml) | Galaxy S23 / S24 / S25 系列 | 自动拉取 CHC 固件，支持 TGY GPI 提取或复用缓存、替换 Kokuban 内核、KernelSU | Odin ZIP（含 AP + CSC） |

## 使用方式

1. 进入 [Actions](https://github.com/yucca-a/Yucca-ROM/actions)，选择 **小米 ROM**、**红米 ROM** 或 **三星 ROM**。
2. 点击 **Run workflow**，按需选择机型和开关（选择系列会并行构建该系列。小米和红米按需勾选「注入预置 APK」，默认关闭）。
3. 构建完成后前往 [Releases](https://github.com/yucca-a/Yucca-ROM/releases) 按机型选择「加密Data」或「解密Data」。达到 2 GiB 的刷机包会自动分卷（`-1.zip`、`-2.zip`…），请按发布页说明依次合并、校验后使用。

## 仓库配置

机型配置、补丁脚本和预置 APK 存放在私有仓库 `YAK-Profiles` 中，运行前需在仓库 Secrets 中配置：

- `PROFILES_TOKEN`（兼容旧名 `YAKIT_TOKEN`）：读取 `YAK-Profiles` 及 Release 资产；勾选更新三星 GPI 缓存时需具备该仓库的 Contents 写权限。

> [!WARNING]
> 本项目生成的 ROM 仅适用于已解锁 Bootloader 的设备，刷机有风险，请提前备份重要数据。

## License

[AGPL-3.0](LICENSE)
