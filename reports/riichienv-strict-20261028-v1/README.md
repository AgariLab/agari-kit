# 2026-10-28 strict-tsumogiri 实验引擎验证

组织分支 [agari/strict-tsumogiri](https://github.com/AgariLab/RiichiEnv/tree/agari/strict-tsumogiri) 已推送。
补丁提交为 `4ff32a698ac1d0cd51342dd8a02c4202cbdb1105`，上游基点为
`479c1faeb33d082965eef8198f63261a79c0fce3`。补丁要求显式 MJAI `tsumogiri` 标记与实际出牌一致。

隔离 wheel 完成 1,000 个四人赤牌半庄：种子 0–249，各轮换四座，
阵容为 `efficiency / random / tsumogiri / random`。累计 700,448 次决策，
零非法动作和未处理异常；候选动作耗时 p95 为 0.1893 ms，仅计 `agent.act`。
此前相关验证为 246 项 Python 测试及 72 个子用例、35 项 Rust 测试、47 项 kit 兼容测试通过。
kit 兼容测试在测试进程内将已修复的预期失败转为正常断言，没有修改原基线的测试标记。

与 [原基线可靠性报告](../reliability-20261028-v1/README.md) 按 `(seed, rotation)` 对齐的
1,000 局中，`scores`、`ranks`、`steps`、`decisions`、`event_counts` 全部逐项一致。
两个运行的 kit 源码指纹不同，因此这是记录结果的比较，不能作为同源码实验对引擎补丁作因果归因；
耗时字段不参与一致性比较。完整比较结果及输入哈希见 [baseline-comparison.json](baseline-comparison.json)。

- [manifest.json](manifest.json)：运行配置、源码指纹，以及引擎提交、wheel 和实际动态库哈希。
- [report.json](report.json) 与 [games.jsonl](games.jsonl)：汇总与逐局统计，不含完整牌谱。
- [build-manifest.json](build-manifest.json)：构建环境、测试和产物哈希；其中 `published: false` 是源码分支推送前的构建快照。
- [kit-compatibility.json](kit-compatibility.json)：隔离 wheel 的 kit 兼容验证结果。

实际动态库在评测前后 SHA256 一致，且与 wheel 内对应文件一致。wheel、隔离环境和工具链仅保存在
`/Volumes/新加卷/AgariLab/runs/riichienv-strict-tsumogiri-20261028` 及外置缓存，未复制到仓库。
macOS 27 构建使用 `RUSTFLAGS='-C strip=none'` 规避 [Rust 符号表对齐问题](https://github.com/rust-lang/rust/issues/157750)。

**该 fork 尚未采用为正式运行基线，尚未通过远端真实 Observation 验证。**
这些本地结果不能代表线上接入验收或强对手棋力表现；现有 kit/research 仍使用原版 `riichienv==0.4.10`。
