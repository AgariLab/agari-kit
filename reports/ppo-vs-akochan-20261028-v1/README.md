# 自有 PPO 对原版 Akochan 的接入检查

2026-10-28，`baseline-1000-v1` 导出 PPO 对固定原版 Akochan、基础牌效、摸切，
使用 seed 400000 轮换四座，完成 4 个四人赤牌半庄、36 局、2,485 次决策，零非法动作。
PPO 共有 626 次决策，四场均为第三名，平均得失点 −5,950。

这是一个种子组的工程检查，没有置信区间，远低于 10,000 场晋级样本量；不支持棋力提升或强基线复现的结论。
PPO 仅在 1,000 场数据集上进行有限 GRP/CQL/PPO 更新，未遍历全部训练样本。
该组对局也不能代表已完成线上验证或排位。

清单绑定两个模型的 factory 源码、描述、实际权重／二进制／参数清单，以及原版 RiichiEnv 动态库哈希。
原版 Akochan 源码提交为 `53188a0b926fbab38177f88c3cd87d554cf412af`，使用 2 线程；
出处与使用条件见 [对手说明](../../docs/external-opponents.md)。这里只发布统计，不发布其源码、参数或二进制。

```sh
python3 ../agari-kit/scripts/with_storage.py uv run --frozen agari evaluate \
  --candidate '@/Volumes/新加卷/AgariLab/models/baseline-1000-v1/ppo-agent.json' \
  --opponents '@/Volumes/新加卷/AgariLab/runs/opponent-akochan-v1/agent.json' efficiency tsumogiri \
  --seed-start 400000 --seed-count 1 --run ppo-1000-vs-akochan-reproduction
```

重新执行需要已核验的本地对手产物与模型；原报告对应的源码指纹见 manifest，后续修正不会回写历史报告。
