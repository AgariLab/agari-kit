# 原版 Akochan 本机接口验收

日期：2026-10-28。此目录只保存自有构建与运行元数据，不包含第三方源码、参数、二进制或完整牌谱。原始文件位于 `/Volumes/新加卷/AgariLab/runs/opponent-akochan-v1/`，[evidence.json](evidence.json) 记录全部证据文件的大小和 SHA-256。

固定上游提交：`critter-mj/akochan@53188a0b926fbab38177f88c3cd87d554cf412af`。原版 AI 文件逐一通过内容哈希检查；已有 Apple clang 21 和外置 Boost/OpenMP 编译 39 个翻译单元，仅改变链接配置。2 线程 CPU 原版程序通过 `@descriptor` 接入，参数包、配置、二进制、runtime 都有哈希绑定。源码许可与真实平台边界见 [调研文档](../../docs/external-opponents.md)。

| 验收 | 结果 |
| --- | --- |
| 适配器测试 | 19 项纯标准库单元测试与 1 项原生集成测试通过；覆盖可见增量、连续动作、严格合法匹配、篡改拒绝、延后宝牌、令牌环境隔离、时限和进程组清理 |
| 真实 worker | ProcessRunner 实际调用原版程序返回合法摸切并关闭，首次决策约 265 ms |
| 单完整半庄强验证 | seed 2026、9 局、540 次总决策，原生 trace 重放 `engine-verified`；候选实际立直 4 次、碰牌 2 次、和牌 3 次 |
| 公开 evaluate 接口 | seed 2026 全部四座位轮转完成，37 局、2,190 次总决策、0 非法动作，约 210 秒 |
| 候选独立延迟 | 549 次决策；mean 381.96 ms、p95 1,084.36 ms、max 1,859.30 ms；包含 act 内参数核验与首次原版进程启动，不包含 factory/reset 与外部传输 |
| 样本限制 | 只有 1 个 seed 组，bootstrap CI 不可估，晋级样本门槛未达标；平均名次 2.0 不构成棋力结论 |
| 修复后原生边界 | 完整合法牌墙构造 `kakan,dora` 抢杠窗口，原版 AI 实际选择合法 Ron、赢家加 5,200 点，约 184 ms |
| 修复后半庄复测 | seed 2027、8 局、464 决策、43.55 秒；`engine-verified`、运行期间代码指纹未变 |

单观察探针使用正确半庄映射 `kyoku_first=0`；原始错误映射探针 `probe-seed2026` 仅留在外置目录作为历史记录，没有复制为验收结果。东风对应 `kyoku_first=4`。

原版 pipe 不响应他家暗杠触发的国士抢杠机会，适配器会明确失败；只有原生唯一合法动作为 pass 才可直接跳过。连续开杠后 `kakan,dora` 窗口已定向修复：原线序不变、kakan 可决策、尾部 dora 不触发第二次决策；原版 AI 在 kakan 处决定后才消费这条宝牌，因而该决定没有使用延后宝牌。触发事件后任何非 dora 事件仍明确拒绝。

四座位 smoke 先于该边界修复与令牌环境隔离改动，报告保留其实际代码指纹。修复后另跑真实原版定向 fixture 和 seed 2027 完整半庄，没有将旧四座位报告冒充成新代码重跑结果。

文件入口：

- [构建记录](build-manifest.json)、[依赖记录](dependencies-manifest.json)
- [修正后的半庄单观察探针](probe-seed2026-half-v2/report.json)
- [完整半庄与强验证回执](fullgame-seed2026-v1/report.json)
- [四座位评测配置和完整 provenance](evaluation-seed2026-v1/manifest.json)
- [逐场指标](evaluation-seed2026-v1/games.jsonl)、[汇总报告](evaluation-seed2026-v1/report.json)
- [修复后真实抢杠窗口](chankan-delayed-dora-v2/report.json)、[修复后完整半庄](fullgame-seed2027-v2/report.json)

构建脚本为 [build_akochan.py](../../scripts/build_akochan.py)，单观察复测脚本为 [probe_akochan.py](../../scripts/probe_akochan.py)。实际可加载描述符是外置根目录的 `agent.json`；本报告没有复制成相对路径失效的另一个描述符。
