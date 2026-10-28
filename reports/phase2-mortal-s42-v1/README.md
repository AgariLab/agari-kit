# Mortal-S42：固定运行包与协议资格

2026-10-29 已完成本机 CPU 接入及协议资格。这里公开小型回执与统计；第三方权重、
源码、隔离环境和原生对局轨迹保留在外置存储。棋力资格由后续独立评测决定。

- 固定发布：`haor/Mortal-S42@524981f21a929e3c3b269f65af79b84163bd02ca`。
  43,670,035-byte 权重 SHA-256 为
  `e5f6955b6f19007fd65315e53291b36994493046bc118b991b959e9dc596d660`。
- 固定 kit：`78719f35ea17d68e3a6fcfd04a2aa969a95142cc`；源码指纹
  `c3a791b966a688a211bbb7bf1b87dcb908a80b474b75105ae90052d2d9a5cfb7`。
- [协议资格原报告副本](protocol-qualification.json) 与外置原件逐字节相同，SHA-256：
  `a8e1256a0ce4110a0226f0f899fe8134c0cf0ecc5b45eeed7b1f1cca1c30504c`。
  `checks` 中 16 项证据路径均相对于 `/Volumes/新加卷/AgariLab`。
- [构建、来源及性能摘要](summary.json) 记录原件路径、大小、哈希、七项原生窗口结果及逐场计时；
  [Python 包版本](requirements-cpu.txt) 对应实际隔离环境。完整环境清单绑定 15,793 个文件。

权重为 Apache-2.0；随附 Mortal runtime、发布者加载及构建脚本为 AGPL-3.0-or-later；
原创 Agari 适配器为 Apache-2.0。完整来源、许可及新目录复建命令见
[接入文档](../../docs/mortal-s42.md)。包版本相同不代表文件内容相同，复建后应生成新的运行身份。

实际构建保留了两个失败及修正记录：发布包遗漏 workspace 引用的 `exe-wrapper`，
从其声明的 Mortal 固定提交 `0cff2b52982be5b1163aa9a62fb01f03ce91e0d2` 补齐两个文件；
随后在 macOS 上改用 `cargo build --release --locked --lib -p libriichi`，避开 Python
扩展配置下不可链接的工具二进制。模型、编码器和规则逻辑未修改。最终编译步骤约14.44秒，
不包含前面依赖下载、失败尝试及整个环境安装耗时。

四场真实半庄采用 seed `20261030..20261033`、候选依次坐0..3席，另外三席为 efficiency；
这是四个不同种子的协议探针，不是同种子四座配对的棋力实验。共1,672次决策，其中教师427次，
零非法，四场全部 `engine-verified`；七项定向窗口包括赤五吃碰、立直后弃牌、暗杠、延后宝牌
抢杠、双 Ron 后新局及九种九牌。22项单测、2项原生规则预设检查另行通过。

| 计时项目 | 实测范围 |
| --- | --- |
| 每场构造及完整依赖内容核验 | 1.465–1.515 秒 |
| 每场 reset，包含启动 worker | 1.068–1.076 秒 |
| 每场对局运行 | 0.432–1.315 秒 |
| 每场含收尾、原生复放的完整耗时 | 3.069–3.979 秒 |
| 每场教师动作 p95 | 10.12–15.92 ms |
| 所有动作的最大延迟 | 25.29 ms |

四场合计13.894秒。各场 p95 范围不是合并样本的 p95；上述延迟包含适配器和进程通信，
不含网络，也不能代替四教师采集或强对手大样本的实际吞吐测量。

## 在冻结环境复现协议检查

以下使用已保留的运行包和冻结 worktree；输出写入新的 `repro-1` 目录，不覆盖原件。
再次执行时更换 `AGARI_REPRO_RUN`。代码、运行包或路径不同会产生不同身份，应另存报告。

```sh
AGARI_PHASE2_KIT=/Users/zhou/MyCodes/AgariLab/phase2-runtime-20261029-v1/agari-kit
AGARI_PHASE2_PYTHON=/Users/zhou/MyCodes/AgariLab/agari-research/.venv/bin/python
AGARI_PHASE2_STORAGE='/Volumes/新加卷/AgariLab'
AGARI_REPRO_RUN=phase2-mortal-s42-repro-1
export AGARILAB_STORAGE_ROOT="$AGARI_PHASE2_STORAGE"
export PYTHONPATH="$AGARI_PHASE2_KIT/src"
export PYTHONDONTWRITEBYTECODE=1

"$AGARI_PHASE2_PYTHON" -B "$AGARI_PHASE2_KIT/scripts/probe_mortal_s42.py" \
  --agent "$AGARI_PHASE2_STORAGE/runs/phase2-mortal-s42-v1/agent.json" \
  --output "runs/$AGARI_REPRO_RUN" --games 4 --seed 20261030

env AGARILAB_MORTAL_DESCRIPTOR="$AGARI_PHASE2_STORAGE/runs/phase2-mortal-s42-v1/agent.json" \
  AGARILAB_MORTAL_PROBE_REPORT="$AGARI_PHASE2_STORAGE/runs/$AGARI_REPRO_RUN/native.json" \
  "$AGARI_PHASE2_PYTHON" -B -m unittest discover \
  -s "$AGARI_PHASE2_KIT/tests/integration" -p test_mortal_runtime_native.py -v

"$AGARI_PHASE2_PYTHON" -B "$AGARI_PHASE2_KIT/scripts/build_mortal_qualification.py" \
  --agent "$AGARI_PHASE2_STORAGE/runs/phase2-mortal-s42-v1/agent.json" \
  --native-report "runs/$AGARI_REPRO_RUN/native.json" \
  --games-report "runs/$AGARI_REPRO_RUN/report.json" \
  --output "runs/$AGARI_REPRO_RUN/qualification"
```

这些命令只运行本地对局与文件验证，不连接 riichi.dev，也不重复 Bot validation。
