# Mortal-S42 外部基线与教师接入

本适配器运行社区发布者 `haor` 的 Mortal-S42；它不是 Equim-chan 官方发布的权重。
权重许可为 Apache-2.0；随附的 Mortal runtime、加载及构建脚本为 AGPL-3.0-or-later。
原创进程适配器不包含这些第三方源码或参数，运行包保存在配置的外置存储。
来源：[固定发布说明](https://huggingface.co/haor/Mortal-S42/blob/524981f21a929e3c3b269f65af79b84163bd02ca/README.md)、[身份](https://huggingface.co/haor/Mortal-S42/blob/524981f21a929e3c3b269f65af79b84163bd02ca/model-identity.json)、[许可分别说明](https://huggingface.co/haor/Mortal-S42/blob/524981f21a929e3c3b269f65af79b84163bd02ca/NOTICE)、[原版推理说明](https://huggingface.co/haor/Mortal-S42/blob/524981f21a929e3c3b269f65af79b84163bd02ca/INFERENCE.zh-CN.md)。

已完成的固定版本构建、规则窗口及四座位验收见 [阶段二薄报告](../reports/phase2-mortal-s42-v1/README.md)。

固定版本与运行接口：

- Hugging Face revision：`524981f21a929e3c3b269f65af79b84163bd02ca`。
- `weights/s42-inference.pth`：43,670,035 bytes，SHA-256
  `e5f6955b6f19007fd65315e53291b36994493046bc118b991b959e9dc596d660`。
- 随附 Mortal 源码基点：`0cff2b52982be5b1163aa9a62fb01f03ce91e0d2`。
- CPU、单个 Torch 线程、贪心策略；保持发布者启用的 agari guard、完整 Q 输出等设置。
- 公共入口仍为 `@agent.json`，factory 是 `agari_kit.opponents.mortal:MortalAgent`；
  descriptor 绑定 `runtime-manifest.json` 的哈希，不引入新的公共 Agent API。
- 只支持 `RiichiEnv 0.4.10 / default_tenhou / 4p-red-half`。实际 Tenhou 规则关闭
  国士抢暗杠和庄家首弃强制手切；雀魂／自定义规则属于不同契约，未宣称支持。

## 构建独立环境

示例路径以 `/Volumes/新加卷/AgariLab` 为根。新建其他运行版本时同步更换目录，
不要覆盖已经绑定到报告的运行包。下载及构建前检查卷挂载、空间和写权限。

```sh
env -u HF_TOKEN -u HF_USERNAME \
  HTTPS_PROXY=http://127.0.0.1:10800 HTTP_PROXY=http://127.0.0.1:10800 \
  https_proxy=http://127.0.0.1:10800 http_proxy=http://127.0.0.1:10800 \
  HF_ENDPOINT=https://huggingface.co \
  hfd.sh haor/Mortal-S42 --revision 524981f21a929e3c3b269f65af79b84163bd02ca \
  --local-dir '/Volumes/新加卷/AgariLab/runs/phase2-mortal-s42-rebuild-v1/source' --tool wget
```

本机的 `hfd.sh` 支持固定 revision 和外置路径，但进度统计使用 GNU `stat/find` 选项；
macOS 上进度曾持续显示 0%，实际 80 个文件仍成功下载。构建脚本会独立核验作者的
79 项 SHA-256、权重大小及固定哈希，不把进度条当作文件完整性证据。

创建外置隔离 venv，使用项目 Python 3.12.16、清华索引、外置 uv 缓存和 copy 模式。
本机已实测 Torch 2.9.1、NumPy 2.4.1；不需要升级主研究环境，也不需要改 conda base。
完整安装文件内容记录在 `environment-files.json`，不能只用包版本代表运行身份。
以下命令在 `agari-kit` 目录执行，使用新的复建目录；包版本来自实际环境的
[完整版本列表](../reports/phase2-mortal-s42-v1/requirements-cpu.txt)：

```sh
python3 scripts/with_storage.py uv venv --python ../agari-research/.venv/bin/python \
  '/Volumes/新加卷/AgariLab/runs/phase2-mortal-s42-rebuild-v1/venv'
env UV_DEFAULT_INDEX=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple \
  python3 scripts/with_storage.py uv pip install \
  --python '/Volumes/新加卷/AgariLab/runs/phase2-mortal-s42-rebuild-v1/venv/bin/python' \
  -r reports/phase2-mortal-s42-v1/requirements-cpu.txt
```

发布包原构建有两个需要记录的环境适配：`runtime/Cargo.toml` 引用未打包的
`exe-wrapper`，从上述固定 Mortal 提交补齐两个文件并记录哈希；macOS 的 PyO3
extension-module 无法链接同包的命令行工具，所以只构建 `--lib`。
未修改 AI、模型、编码或规则源代码。使用现有外置 Rust 1.92.0，例如：

```sh
env HTTPS_PROXY=http://127.0.0.1:10800 HTTP_PROXY=http://127.0.0.1:10800 \
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B scripts/build_mortal_s42.py \
  --bundle '/Volumes/新加卷/AgariLab/runs/phase2-mortal-s42-rebuild-v1' \
  --cargo '/Volumes/新加卷/AgariLab/cache/riichienv-strict/rustup/toolchains/1.92.0-aarch64-apple-darwin/bin/cargo' \
  --complete-workspace
```

该命令要求 `bundle/venv/bin/python` 及所需依赖已经安装。新 Cargo 缓存／target 位于
`cache/phase2-mortal-s42/`，不会向全局安装 Rust。已有 manifest 时构建脚本拒绝覆盖。
构建失败保留独立日志；源码补全回执、编译命令、原生库哈希及来源许可均留在运行包中。

## 决策与身份检查

适配器使用独立 Python 进程，双方以带递增请求 ID 的 JSONL 通信。只发送重新投影后的
玩家可见增量事件，逐条按原顺序交给原版 `libriichi.mjai.Bot`；仅当前决策事件允许响应。
立直和吃碰后的弃牌由实际后续事件触发；延后宝牌保留顺序。返回动作逐字段与原生合法
动作核对，包括赤牌消费和实体摸切，不依赖引擎宽松 MJAI mapper。

原版 Hora 不带 `pai`、九种九牌不带 `actor`，按其明确协议与原生窗口匹配；这些省略
不会放宽显式提供的牌或玩家信息。只有原生唯一合法动作为 PASS 时，才允许模型不返回动作。
其他不支持的窗口、超时、退出、非法动作、额外输出或错误 ID 均使该场失败，不替换弱代理。

首次构造完整核验发布包、解释器和隔离依赖内容；每场 reset 与 close 核验发布包内容及
全部依赖文件的 stat；每个动作检查发布包和解释器的 stat。运行结束发现依赖变化也会使
该场失败。**不是每个动作重新哈希完整 Torch 环境**。worker 复用仅发生于同一 Agent
实例的 reset；现有评测／采集每场重建 Agent 时仍包含完整构造与启动成本。
stderr 留在外置 `worker-logs/`，工作进程不继承 Bot、HF 或 GitHub Token 环境变量。

## 验证及门禁

标准库单测覆盖进程协议、隐私投影、动作匹配、故障清理、内容变化与证据报告拒绝条件。
原生规则测试核对 Tenhou 与雀魂的实际差异。真实 S42 测试为显式选择的本地集成检查，
CI 不自动下载模型；设置 `AGARILAB_MORTAL_DESCRIPTOR` 后运行
`tests/integration/test_mortal_runtime_native.py`，可用 `AGARILAB_MORTAL_PROBE_REPORT`
输出含运行身份及七项窗口结果的 JSON。

`scripts/probe_mortal_s42.py` 完成四座位半庄、原始动作轨迹和原生重放校验，分别记录
构造校验、worker 初始化、对局、动作分位延迟及包含收尾的总耗时。
`scripts/build_mortal_qualification.py` 将这些真实报告与原件 SHA 绑定到同一个教师身份、
源码、引擎和规则；缺失窗口、错误座位覆盖、文件变化或源码漂移会拒绝发布资格报告。
`checks[].evidence_path` 始终相对于配置存储根，`evidence_sha256` 是对应实际文件的哈希。

2026-10-29 已在冻结 kit `78719f35ea17d68e3a6fcfd04a2aa969a95142cc` 下完成正式协议资格：
四个真实半庄、1,672 次总决策／427 次教师决策、零非法，全部通过原生轨迹重放；七项
真实 S42 规则窗口全部通过。四场含构造、初始化、收尾及复放共 13.894 秒，各场动作
p95 为 10.12–15.92 ms。这是本机单线程、一个 S42 对三个 efficiency 的小样本测量，
不外推四教师采集或正式棋力评测吞吐。资格报告与复现命令见上述薄报告；
它不证明该社区模型已胜过 Akochan，也不代替 1,000 场教师筛选或学生晋级评测。
