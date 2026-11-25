# AGENTS.md

本项目是个人自用的**全自动定制 ROM 构建仓库**，面向通过 GitHub Actions 触发构建、在 Releases 下载刷机包的使用场景。

## README.md 维护规范（必须遵守）

1. **不要主动改写 `README.md`**
   - 仅在用户明确要求更新文档，或增删了面向用户的 GitHub Actions 入口/关键选项时，才最小化更新 `README.md`。
   - 功能改动、调试过程、技术细节和真机验证记录，只写在 commit message、代码注释或对话回复中，**不要写进 `README.md`**。

2. **严禁在 `README.md` 中写入以下内容**
   - **本地构建说明**：不要写 `local_build.sh` 用法、CLI 参数（如 `--system-format`、`--keep-encryption` 等）、宿主机/WSL2 要求或 `libguestfs`/appliance 等本地环境依赖。
   - **真机测试与验证记录**：不要写某台设备某版本号的开机验证过程、测试步骤或 `tests/smoke.py` 验证命令。
   - **底层镜像处理细节**：不要写 `lpmake`、`resize2fs`、OverlayFS、空间分配算法、SELinux 标签机制、`fstab`/`attributes` 实现细节、`avbtool`、`payload-dumper-go`、Git tag 特殊字符编码等内部实现。
   - **冗长的外部教程与科普**：不要写分卷合并/SHA256 校验的详细命令行教程，也不要科普 GitHub Releases 的官方配额与限制。

3. **文风与篇幅**
   - 保持简洁、自然、克制的个人项目风格，避免 AI 说明文腔和堆砌术语。
   - `README.md` 总篇幅保持精简（约 30–40 行以内），只保留：项目简介、支持机型与 Actions 入口、简明使用方式、必要的 Secrets 配置、解锁风险提示和 License。
