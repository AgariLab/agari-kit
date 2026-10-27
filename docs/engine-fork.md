# RiichiEnv 组织 fork

已建立 [AgariLab/RiichiEnv](https://github.com/AgariLab/RiichiEnv)，保留 `smly/RiichiEnv` 的 fork 关系、
Apache-2.0 许可证和第三方来源说明。2026-10-28 已通过 GitHub API 确认组织权限和 fork parent。

当前基准是清华镜像取得的 `riichienv==0.4.10`，wheel 下载地址和校验值位于 `uv.lock`。
上游 [v0.4.10 发布页](https://github.com/smly/RiichiEnv/releases/tag/v0.4.10) 指向完整提交
[`479c1faeb33d082965eef8198f63261a79c0fce3`](https://github.com/smly/RiichiEnv/commit/479c1faeb33d082965eef8198f63261a79c0fce3)。
已用 Git 校验 tag 和本地检出 commit 一致。

本地 `agari/strict-tsumogiri` 分支修复 MJAI 显式 `tsumogiri` 标记与物理摸牌不一致时仍被宽松接受的问题。
新增四人／三人、赤五／普通五、同种不同物理牌及鸣牌后强制手切回归；补丁在独立构建环境验证。
当前 kit/research 仍使用锁定的上游 0.4.10，分支存在不代表已经晋级运行依赖。

## 维护方式

- `origin` 指向组织 fork，`upstream` 指向原仓库。`main` 保持上游跟踪用途，`agari/*` 保存补丁和性能实验。
- 每个补丁附复现用例、修复前后结果及上游基点。可通用的修复向上游提交，避免长期积累重复补丁。
- 模型、特征策略、数据流水线和线上客户端仍由 `agari-kit`／`agari-research` 维护；引擎分支聚焦环境问题。
- fork 的创建不改变当前安装依赖。采用补丁构建时，为实验清单记录完整 commit、规则、wheel 哈希、构建平台和上游基点。
- 本机源码构建需要 Rust。大缓存、构建中间产物落在项目外置存储；包索引仍遵循用户指定的清华源。

## 版本晋级检查

1. 上游原有 Rust／Python 测试通过；新增修复用例证明问题被解决。
2. 赤五、吃碰杠、立直两段、振听、抢杠、多家和了、流局和终局的明确场景通过。
3. 玩家视角不包含隐藏牌或未来信息，Observation 序列化与 MJAI 动作往返兼容。
4. 固定种子和规则下重复运行一致；若规则或游戏轨迹有意变化，明确记录，单独分组评测。
5. 公共 Agent 和本地 1,000 半庄可靠性测试通过，再验证线上 Observation／协议兼容。
6. 冻结候选 commit／构建哈希，更新依赖锁和实验记录后再用于正式训练或晋级评测。

已完成的 1,000 场报告只验证当前上游 0.4.10 与本地基线，不能为未来 fork 版本自动背书。
