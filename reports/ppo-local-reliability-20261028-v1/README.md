# 2026-10-28 本地 PPO 模型可靠性验收

`agari-research` 的 `baseline-local-v1` 导出模型完成 250 个固定种子 × 四座轮换，
共 1,000 个四人赤牌半庄，707,625 次决策（候选模型 185,272 次），零非法动作和未处理异常。
模型使用 20 场规则 Agent 自博弈数据、CPU GRP 100 / CQL 200 / PPO 4 次更新。
数据摘要为 `8848a4fcc1522526130b4d86f383bb0f2a75fe31a59b977056d39b7f73049760`。

候选对手固定为 `efficiency`、`random`、`tsumogiri`，种子 200000–200249，与训练自博弈种子分离。
模型平均顺位 2.558（按种子组 bootstrap 95% 区间 2.506–2.610），平均得失点 −7,214.6。
这是导出模型工程可靠性证据；对手池较弱，尚未达到 10,000 场晋级样本量，也不证明强基线棋力。

仅候选 `Agent.act` 延迟：平均 0.116 ms、p95 0.172 ms、最大 1.838 ms。
不包含加载模型、进程通信和网络。游戏循环累计 64.89 秒，不包含文件写入、模型加载和统计开销。

权重 SHA-256：`5f8da7022420933526cec3429ac32e73c7914a6f69fde57b047dacc76a94fed6`。
`manifest.json` 保存模型描述、实际权重哈希、研究包和公共工具源码指纹；`games.jsonl` 保存逐场结果。
权重、训练检查点及完整数据保存在外置卷，没有提交到此目录。

在已按研究仓库说明安装的环境中，保留既有产物并选择新运行名：

```sh
python3 ../agari-kit/scripts/with_storage.py uv run --frozen agari evaluate \
  --candidate '@/Volumes/新加卷/AgariLab/models/baseline-local-v1/ppo-agent.json' \
  --opponents efficiency random tsumogiri --seed-start 200000 --seed-count 250 \
  --run ppo-local-reliability-reproduction
```
