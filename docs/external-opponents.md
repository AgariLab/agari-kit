# 外部对手与天凤／雀魂接入调研

核查日期：2026-10-28。已核对原作者仓库、源代码和官方平台说明，并在本机 CPU 编译原版 Akochan、运行玩家可见事件探针及完整半庄。原版源码、参数、编译缓存和二进制均保存在外置卷；没有登录天凤或雀魂。Mortal 目前仍为源码与权重来源调研。

首期外部对手优先选 **Akochan 的原版独立进程**：官方仓库已经包含推理参数，并提供 Apple Silicon 构建配置。Mortal 的代码可研究，但目前没有核实到作者公开发布的训练权重，不能将来路不明的第三方 `mortal.pth` 当作官方强基线。真实平台接入先保留为独立适配层；现有 riichi.dev 客户端不能直接连接天凤或雀魂。

| 项目 | 核查到的固定版本 | 可取得的参数／权重 | Mac mini CPU 结论 |
| --- | --- | --- | --- |
| [critter-mj/akochan](https://github.com/critter-mj/akochan) | [`53188a0b926fbab38177f88c3cd87d554cf412af`](https://github.com/critter-mj/akochan/commit/53188a0b926fbab38177f88c3cd87d554cf412af)，提交日期 2022-07-05 | `params/` 下 709 个普通 Git 文件，合计 **86,405 bytes**，约 84.4 KiB；整个提交的文件内容合计 924,922 bytes | Apple clang 21 + Boost 1.85 headers + LLVM OpenMP 18.1.8 已编译成功，2 线程原版 CPU 决策及完整半庄已通过；单初始决策进程峰值约 16.1 MiB，完整对局延迟另按评测报告统计 |
| [Equim-chan/Mortal](https://github.com/Equim-chan/Mortal) | [`0cff2b52982be5b1163aa9a62fb01f03ce91e0d2`](https://github.com/Equim-chan/Mortal/commit/0cff2b52982be5b1163aa9a62fb01f03ce91e0d2)，提交日期 2025-09-28 | **未核实到官方模型下载地址，权重大小与权重许可均不能填写为已知**。源码文件合计 2,378,469 bytes，并非模型大小 | Python 推理引擎默认 `torch.device('cpu')`；Python 3.12、PyTorch、Rust 扩展是可行方向，但官方构建文档只给出 Linux/Windows 的扩展复制示例，Apple Silicon 构建尚未实测 |

表中大小由官方 GitHub tree API 的 blob `size` 求和得到，是未压缩文件内容，不是压缩包大小、Git 历史体积或运行内存。可复核 [Akochan 固定树](https://api.github.com/repos/critter-mj/akochan/git/trees/53188a0b926fbab38177f88c3cd87d554cf412af?recursive=1) 与 [Mortal 固定树](https://api.github.com/repos/Equim-chan/Mortal/git/trees/0cff2b52982be5b1163aa9a62fb01f03ce91e0d2?recursive=1)。CPU 判断依据为 [Akochan Mac Makefile](https://github.com/critter-mj/akochan/blob/53188a0b926fbab38177f88c3cd87d554cf412af/Makefile_MacOS)、[Mortal engine.py](https://github.com/Equim-chan/Mortal/blob/0cff2b52982be5b1163aa9a62fb01f03ce91e0d2/mortal/engine.py) 和 [Mortal 构建说明](https://github.com/Equim-chan/Mortal/blob/0cff2b52982be5b1163aa9a62fb01f03ce91e0d2/docs/src/user/build.md)。

Akochan 的参数可以直接从[固定版本 params 目录](https://github.com/critter-mj/akochan/tree/53188a0b926fbab38177f88c3cd87d554cf412af/params)取得，或下载包含它们的[官方固定提交源码归档](https://codeload.github.com/critter-mj/akochan/tar.gz/53188a0b926fbab38177f88c3cd87d554cf412af)。例如 [agari_para1.txt](https://raw.githubusercontent.com/critter-mj/akochan/53188a0b926fbab38177f88c3cd87d554cf412af/params/agari_prob/ako/agari_para1.txt) 为 102 bytes。参数包括和牌概率、放铳概率、听牌概率、排名估计等文本系数；不需要另外寻找神经网络 checkpoint。实际取得后仍应生成 SHA-256 清单，把二进制、`params/`、战术配置和编译器版本一起登记；Git commit／blob SHA 不能冒充已实测的文件 SHA-256。

Akochan 的 [LICENSE](https://github.com/critter-mj/akochan/blob/53188a0b926fbab38177f88c3cd87d554cf412af/LICENSE) 是自定义使用条款，覆盖仓库全部文件，因此也覆盖参数。它允许非公开研究和检讨使用；非营利公开输出需要注明使用 Akochan；部分再分发需要保留署名与条款，修改 `ai_src` 后的再分发原则上禁止，其他使用需作者许可。它不是 MIT、BSD 或 GPL 项目。适合本项目当前的方式是保存许可文本，在本地独立进程运行原版，不把 AI 源码或参数复制进 AgariLab 自有许可的发布包。若计划公开组织 fork、修改 AI 部分或商业使用，需先核实作者许可，而不能仅凭“GitHub 上可以下载”作结论。

输出的使用条件与程序再分发条件应分别登记。第 1 条为非公开研究、分析提供一般使用依据；
未专项解释离线训练或蒸馏，也没有明文禁止这些研究方法。第 3 条主张程序及分析结果的知识产权，
但没有定义训练所得模型的范围，因此不能从一般研究条款或日志公开条件推出派生权重可任意公开或商用。
当前 Akochan 对照实验仅用于独立评测，不进入训练／验证集；工程训练覆盖使用本项目基础 Agent 生成的数据。
后续如用 Akochan 输出训练，应记录该一般研究依据及尚未明确的发布条件，不将输出或模型默认标为自有 Apache-2.0 数据。

Mortal 的[代码许可](https://github.com/Equim-chan/Mortal/blob/0cff2b52982be5b1163aa9a62fb01f03ce91e0d2/README.md#license)为 AGPL-3.0-or-later。原作者 README 的权重入口仍链接到[作者说明](https://gist.github.com/Equim-chan/cf3f01735d5d98f1e7be02e94b288c56)：2022-08-19 的更新表示目前无发布训练模型的计划。该结论来自作者声明，并不等于互联网上不存在第三方自行训练的模型；第三方模型必须分别核实训练者、下载地址、SHA-256、架构版本和许可证。代码许可不能代替未发布权重的许可。其[源码归档](https://codeload.github.com/Equim-chan/Mortal/tar.gz/0cff2b52982be5b1163aa9a62fb01f03ce91e0d2)可取得，但不是可直接比赛的模型包。

Akochan 的命令行入口如下；来自固定提交的 [main.cpp](https://github.com/critter-mj/akochan/blob/53188a0b926fbab38177f88c3cd87d554cf412af/main.cpp)。其中 `pipe` 已在本机实际执行，其余入口仅完成源码核查。

| 命令 | 输入／输出语义 | 接入注意点 |
| --- | --- | --- |
| `./system.exe pipe setup_mjai.json 0` | stdin 每行一个 MJAI 事件；需要决策时 stdout 输出一个 JSON **动作数组** | 不是每输入一行都返回；对自身摸牌、他家弃牌／加杠触发。`can_act:false` 抑制计算。立直／鸣牌后的弃牌可在数组中，适配层要排队消费 |
| `./system.exe pipe_detailed setup_mjai.json 0` | 输出局面评估详情 | 适合分析，不应当作单动作接口 |
| `./system.exe mjai_client 11600 setup_mjai.json` | 连接本机 TCP MJAI server，处理 `hello/join` 和事件响应 | 端口默认 11600；它是客户端，不是监听服务；stdout 还包含调试信息，不能当纯动作通道 |
| `./system.exe mjai_log game.jsonl 0` | 针对牌谱末尾局面做评估；可追加零起始行号 | 会打印输入事件和说明文字，不是持久对局协议 |

官方 [mjai.sh](https://github.com/critter-mj/akochan/blob/53188a0b926fbab38177f88c3cd87d554cf412af/mjai.sh) 展示了 `mjai server` 启动 TCP 客户端的用法，不能直接套到 riichi.dev WebSocket。战术配置入口是 [setup_mjai.json](https://github.com/critter-mj/akochan/blob/53188a0b926fbab38177f88c3cd87d554cf412af/setup_mjai.json)，包含 `jun_pt` 等目标参数；不同配置应视为不同对手版本。

本项目的适配与验收要求是：

1. 以独立进程运行 `pipe`；固定工作目录，让相对参数路径可解析，限制 OpenMP 线程数并记录实际线程配置。源码／编译产物位于外置存储，仓库仅保存自有适配代码和来源清单。
2. 从当前玩家 `Observation` 的可见事件驱动子进程，保证先有 `start_game`；只发送允许该玩家看到的起手牌、摸牌和公开副露。该接口会在 `start_kyoku` 保留第一条事件并清空其余历史。
3. 按当前决策窗口标记 `can_act`，缓存动作数组中后续的立直弃牌或鸣牌后弃牌；以 RiichiEnv 当前合法动作、赤牌、`consumed`、物理摸切关系严格匹配。不能仅凭宽松 MJAI mapper 返回非空就接受动作。
4. 子进程超时／退出／非法输出应使评测失败并记录原因，不能悄悄替换成弱代理后仍把成绩记在 Akochan 名下。
5. 完成规则场景及至少一个四座位轮转 seed 组后，再运行正式强基线评测；记录候选与对手各自延迟、完整规则和参数哈希。四场工程验收不能证明对手强度，也达不到 10,000 场晋级评估门槛。

官方 Mac Makefile 依赖 Homebrew LLVM、Boost、OpenMP 及旧 Polly 选项，生成 `libai.so` 和名称为 `system.exe` 的程序。本机实际使用已有 Apple clang 21，外置 conda-forge 包缓存中的 Boost 1.85 headers、LLVM OpenMP 18.1.8，以及自有 [build_akochan.py](../scripts/build_akochan.py) 构建：39 个原始翻译单元统一链接为一个 arm64 Mach-O 程序，关闭对独立 `libai.so` 的依赖，没有修改任何原版 AI 源文件。源码内容清单在构建前逐项校验，构建命令、依赖包哈希和最终二进制哈希均保留。该构建只改变编译／链接配置，未运行 Homebrew 安装，也没有修改 conda base。[官方 AI 构建配置](https://github.com/critter-mj/akochan/blob/53188a0b926fbab38177f88c3cd87d554cf412af/ai_src/Makefile_MacOS)

本机执行证据位于 `/Volumes/新加卷/AgariLab/runs/opponent-akochan-v1/`：

| 产物 | 绑定与含义 |
| --- | --- |
| `source-manifest.json`、`LICENSE.upstream.txt` | 官方固定提交归档 188,017 bytes，SHA-256 `18cdb32872fcf90f9eb1c5b5dca87eb57abe8fa1b6954d0d81e4b9be2c70a910`；许可文本 SHA-256 `73d32d37160ecee5893b00ada91a3ca63eaa2f550ca5bb827c571043c0af910a` |
| `dependencies-manifest.json`、`build-manifest.json` | 完整编译器版本、依赖原始包来源／哈希、构建命令；binary SHA-256 `2a257e14973093810caf1752823e0a79b51560a1a48d309caa2d908cf8038c5d` |
| `runtime-manifest.json` | 绑定 709 个参数、二进制、战术配置、许可和 OpenMP runtime，共 713 文件；SHA-256 `c52066260f2fd5de799711cd44da8e84bc9997136210bbbf15c2fb3d044210bb` |
| `agent.json` | `agari_kit.opponents.akochan:AkochanAgent` 的公开 `@descriptor` 入口，绑定 binary/config/manifest 内容哈希，2 线程、单决策最长 30 秒；SHA-256 `67dda758e7f1942e5d33983ebd406f63247400b5a2a428ce30a19d57c6a98aca` |
| `probe-seed2026-half-v2/report.json` | 真实初始玩家观察、半庄规则、严格合法弃牌；该次含启动耗时约 91.9 ms，进程峰值 16,875,520 bytes。早期 `probe-seed2026` 将 `kyoku_first` 错设为东风，仅保留为错误假设记录，不能作为半庄验收证据 |
| `fullgame-seed2026-v1/report.json` | 对三个 efficiency 代理的一场半庄：9 局、540 决策、约 46.76 秒，分数 `[38500,21600,26100,13800]`；候选实际走通 4 次立直、2 次碰牌、3 次和牌。原生 trace 重放结果 `engine-verified`，canonical MJAI SHA-256 `09ef2885858199605f59806600e321e9b897048d690b03ee800100cf7a180e59` |
| `evaluation-seed2026-v1/report.json` | 公开 evaluate 四座位轮转全部完成：37 局、2,190 次总决策、0 非法动作；候选 549 次决策 mean 381.96 ms、p95 1,084.36 ms、max 1,859.30 ms。仅 1 个 seed 组，CI 为 null、未达到晋级样本门槛；约 210 秒运行时间 |
| `chankan-delayed-dora-v2/report.json` | 连续开杠的原生合法 136 牌墙 fixture，原生观察末尾确为 `kakan,dora`；原版 AI 实际返回合法抢杠和（实体牌 99），赢家 25,000→30,200、加杠者 25,000→19,800，约 183.8 ms |
| `fullgame-seed2027-v2/report.json` | 定向修复后的真实半庄，8 局、464 决策、约 43.55 秒；分数 `[30600,23700,29200,16500]`，`engine-verified` 且运行期间代码指纹未变。canonical MJAI SHA-256 `5ba86d1b1bfe04479c687c4a83c9d514561076e86b2318196cd4ed7bc4302e4e` |

规则映射严格采用上游 `mjai_client.cpp`：`kyoku_first=0` 表示半庄，`4` 表示东风，`aka_flag=true`。适配器只消费玩家 `new_events()`，再次投影隐藏牌；固定 0.4.10 的实际 Rust 实现中它与 `events` 是同一原生增量，旧 `.pyi` 的历史游标注释不能作为行为依据。所有参数会在构造、reset 和每次决策前重新核验，因为上游会在计算过程中按需读取参数文件。玩家 deadline 包括核验、启动与读写，不能因首次初始化而重置。原版 pipe 不会对他家暗杠触发计算；只有原生唯一合法动作为 pass 时可以直接跳过，有国士抢暗杠机会时明确失败，不会替换动作或冒记为 Akochan 决策。

19 项仅依赖标准库的单元测试和 1 项原生引擎集成测试覆盖可见事件边界、东／半庄映射、立直与碰牌队列、竞争鸣牌失效、赤牌与摸切、参数篡改、严格超时、额外 stdout、延后宝牌、进程退出和后代清理。外部原版进程不会继承 `RIICHI_BOT_TOKEN`。在线 worker 拥有整个进程组时适配器继承该组，独立使用时另开组并在 `close()` 终止整个子进程组；公共 Agent 生命周期负责显式关闭。

上述完整四座位轮转只验证接口，不能据此宣称已战胜某种强度或已满足真实平台时限。连续开杠后 `kakan,dora` 结尾的原生窗口另有定向修复和真实原版验收：仅允许决策事件后出现纯 `dora` 通知，保持原顺序、只在 `kakan` 标记 `can_act:true`；原版 AI 会在 `kakan` 处决定，然后消费末尾宝牌，故该次决定不使用这条延后宝牌。任何其他后续 gameplay 事件都明确拒绝，不能重排或重复事件来伪造状态。四座位报告保留修复前的真实源码指纹，修复后另外留存 native fixture 和 seed 2027 半庄报告。可分享的薄报告见 [opponent-akochan-v1](../reports/opponent-akochan-v1/README.md)，不含原版源码或参数。

真实平台的官方边界与技术差异如下。平台条款与协议实现来源分开列出；社区客户端源码只能证明一种技术实现存在，不能证明平台授权。

| 平台 | 当前官方依据 | 原型建议 |
| --- | --- | --- |
| 天凤 | 官方手册仍列 AI 条件：目前不受理 AI 专用 ID；禁止多个 ID 同时打、超人类数量刷局、凤凰桌 AI 对战；上级以上需要专用 ID，并要求充分验证及公开牌谱链接 | 先做本地协议回放、规则差异和状态恢复测试；需要在线时先确认运营方允许的账号／测试房间，不把一般账号当作专用 AI ID |
| 雀魂 | 已核实的日服条款禁止服务改造／逆向工程及妨碍服务等行为。此次未查到公开、面向任意 AI 的官方 bot SDK 或明确的研究机器人接入许可 | 先做有权取得的离线牌谱解析；任何自动在线原型都先确认目标区服规则与运营方允许的入口。朋友房／大会房存在，不等于自动化客户端获准 |

天凤条件来自[官方手册 AI 小节](https://tenhou.net/man/#AI)；雀魂依据为[日服当前条款](https://dmg.mahjongsoul.com/terms_of_service)第 11 条，尤其第 17、18 项。这里没有把日服条款推断成国服／国际服的同一份协议，也没有声称已查到明确适用于所有区服的机器人许可。

技术上，历史的 [tenhou-python-bot 客户端源码](https://github.com/MahjongRepository/tenhou-python-bot/blob/master/project/tenhou/client.py)采用 TCP socket 和标签式消息，包含连接认证、心跳、牌局消息与重连处理；项目 [README](https://github.com/MahjongRepository/tenhou-python-bot)明确说明已不再维护。因此可参考消息状态机和恢复测试设计，但不能把历史 endpoint／认证方式当成已经验证的当前接入接口。

雀魂社区 [mahjong_soul_api 的 RPC 通道源码](https://github.com/MahjongRepository/mahjong_soul_api/blob/master/ms/base.py)使用 WebSocket、Protobuf Wrapper 和 request ID 分发；[示例](https://github.com/MahjongRepository/mahjong_soul_api/blob/master/example.py)还涉及客户端版本、区域服务发现及登录后的牌谱请求。其 [README](https://github.com/MahjongRepository/mahjong_soul_api)区分国服账号密码与日／国际服邮箱码或第三方登录。这意味着适配层需要维护 schema、版本与区域登录差异；它并不是把 MJAI JSON 发向一个 WebSocket 地址即可工作。上述均是社区项目一手实现资料，不是雀魂官方提供的开放 API 承诺。

三个原型路径应分别验收：

| 路径 | 能验证什么 | 仍不能证明什么 |
| --- | --- | --- |
| 离线牌谱转换与回放 | 事件顺序、牌编码、副露、终局分数、规则差异 | 实时认证、网络时限、当前服务端协议与在线许可 |
| 获准测试入口的协议适配器 | 请求幂等、可见状态、断线恢复、候选动作转换、实时预算 | 仅凭社区客户端可运行，不能推断所有房间／账号都允许 |
| UI／截图原型 | 屏幕识别、动作定位和界面恢复 | UI 自动化不会自动豁免平台规则；动画、分辨率、遮挡还会增加失败点 |

离线数据获取也有独立条件。天凤[官方日志索引](https://tenhou.net/sc/raw/)要求自动更新间隔不少于 20 分钟、最多一个下载会话、检查文件大小避免重复下载、使用 `.log.gz` 和官方重定向入口；页面还限制不依赖天凤对战的服务应用、竞争产品及面向不特定多数的牌谱下载服务。公开可读取不等于可用于任意训练或再分发。现有本地自博弈数据避免了把这些条件误当作已经获得的外部牌谱授权。

首期调研、本机原版 Akochan 编译和完整半庄适配验收已完成；正式大样本强基线评测及任一真实平台的获准在线验收尚未完成。Mortal 官方强权重当前仍缺少可核实来源，不能作为已接入的强对手记账。
