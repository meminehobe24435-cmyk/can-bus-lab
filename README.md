# can-bus-lab

**一句话定位**：一个**纯 Python 标准库**实现的 CAN / CAN FD 总线仿真器 + 类 CAPL 脚本化测试框架 —— 把一个汽车电子测试工程师每天在做的事（周期报文监控、诊断流程、故障码读写、总线仲裁验证、故障注入与恢复），用**可复现、可验证、每一行代码都能讲清楚**的方式实现出来。

它的目标不是替代任何商业工具，而是把「CAN 总线到底是怎么工作的」和「测试脚本到底是怎么驱动总线的」这两件事讲透：**从位级编码开始，到总线仲裁、错误状态机、ISO-TP 传输层、UDS 诊断、再到自己写的脚本引擎与自动化测试报告**，全部自研，全部有测试。

---

## 目录

- [为什么做这个](#为什么做这个)
- [它是什么 / 不是什么](#它是什么--不是什么)
- [架构](#架构)
- [快速开始](#快速开始)
- [模块说明](#模块说明)
- [真实跑出来的数字](#真实跑出来的数字)
- [测试与 CI](#测试与-ci)
- [确定性：连跑两次逐字节一致](#确定性连跑两次逐字节一致)
- [已知简化与未实现](#已知简化与未实现)
- [不能声称什么](#不能声称什么)
- [开发过程中踩到的坑](#开发过程中踩到的坑)
- [许可证](#许可证)

---

## 为什么做这个

招聘 JD 里常见的一条要求是：

> 了解嵌入式 C 语言编程，了解 CANoe 或同星等调试工具的脚本语言编程

CANoe 和 TSMaster（同星）都是商业工具，需要许可证。这个项目做的事情是：
**只用 Python 标准库**，从零实现一个 CAN 总线仿真器，再在上面自己写一个类 CAPL 的脚本引擎，然后用它把车载网络里最典型的几类测试场景跑出来，并生成 JUnit XML 报告。

于是「CAN 总线协议理解」「测试脚本引擎实现」「自动化测试」这三件能力，可以用**能跑、能读、能被追问**的代码呈现，而不是只写在简历上。

---

## 它是什么 / 不是什么

**是什么**

- 一个**位级** CAN / CAN FD 数据链路层仿真器（帧结构、位填充、CRC15/17/21、逐位仲裁）
- 一个 ISO 15765-2 (ISO-TP) 传输层实现（SF/FF/CF/FC、块大小、STmin、六类超时）
- 一个 ISO 14229-1 (UDS) 诊断服务子集（9 个服务 + 15 个 NRC，服务端与客户端都有）
- 一个**自研的类 CAPL 脚本引擎**（词法分析 / 递归下降语法分析 / 解释器 / 虚拟时间调度器）
- 一套 1065 个 pytest 用例 + 6 个用脚本写的真实测试场景 + JUnit XML/文本报告

**不是什么**

- 不是 CANoe / TSMaster（同星）的替代品，也**从未使用过**这两款工具
- 不建模物理层（收发器、终端电阻、线束反射、EMI、位定时与采样点、晶振容差都没建模）
- 不是工业级、不是生产可用，定位是**教学 + 自研仿真 + 脚本化测试框架**
- 没有真实 CAN 硬件、没有真实 ECU、没有整车或台架环境

---

## 架构

```
                     ┌─────────────────────────────────────────────┐
   用户写的脚本       │  demo/scripts/*.capl                        │
   （类 CAPL 语法）   │  周期报文 · 诊断流程 · DTC · 仲裁 · bus-off │
                     └──────────────────┬──────────────────────────┘
                                        │ 词法 / 语法 / 解释
                     ┌──────────────────▼──────────────────────────┐
   canlab.capl       │  lexer.py → parser.py(AST) → interp.py      │
                     │  · 虚拟时间调度器（定时器 / 等待 / 事件派发）│
                     │  · 测试原语 testWaitFor* / testAssert*      │
                     └──────────────────┬──────────────────────────┘
                                        │ output() / on message
                     ┌──────────────────▼──────────────────────────┐
   canlab.bus        │  CanBus：多节点 + **逐位非破坏性仲裁**       │
                     │  统计：仲裁失败 / 重发 / 负载率 / 帧间隔     │
                     └───────┬──────────────────────┬──────────────┘
                             │                      │
              ┌──────────────▼──────────┐  ┌────────▼─────────────────┐
   canlab.errors│ 五类错误检测            │  │ canlab.isotp             │
              │ TEC/REC 计数            │  │ SF/FF/CF/FC · BS/STmin   │
              │ error-active→passive→   │  │ 6 个超时 · 拼包/拆包      │
              │ bus-off → 128×11 恢复   │  └────────┬─────────────────┘
              └──────────────┬──────────┘           │
                             │                      │
                     ┌───────▼──────────────────────▼──────────────┐
   canlab.frame      │  位级帧编解码                                │
                     │  SOF·仲裁场·控制场·数据场·CRC·ACK·EOF·IFS   │
                     │  位填充 / 去填充 · CRC15 · CRC17 · CRC21     │
                     └─────────────────────────────────────────────┘
                                        │
                     ┌──────────────────▼──────────────────────────┐
   canlab.report     │  JUnit XML 报告 + 文本报告                   │
   canlab.cli        │  python -m canlab run/selftest/replay       │
                     └─────────────────────────────────────────────┘
                                        │
   canlab.uds        │  ISO 14229-1：服务端 ECU + 客户端诊断仪      │
                     │  9 个服务 · 15 个 NRC · P2/P2* · 安全访问    │
                     └─────────────────────────────────────────────┘
```

数据流：`脚本 output(msg)` → `canlab.bus` 逐位仲裁 → 上车 → `on message` 事件回填 →
`canlab.frame` 位级编解码 → 校验 CRC / 填充 / 格式 → `canlab.errors` 更新 TEC/REC → 报告。

---

## 快速开始

环境要求：**Python 3.8+**，生产代码**零第三方依赖**，测试需要 `pytest`。

```bash
# 1. 跑内置自检（不依赖 pytest）
python -m canlab selftest

# 2. 跑一个脚本，看报告
python -m canlab run demo/scripts/01_periodic_jitter.capl

# 3. 跑脚本并生成 JUnit XML 报告 + 总线 trace
python -m canlab run demo/scripts/02_diag_flow.capl \
    --report out/report.xml --trace --dump-trace out/trace.json

# 4. 只输出一行结论（适合 CI）
python -m canlab run demo/scripts/04_arbitration.capl --quiet

# 5. 重放录制好的 JSON trace 作为总线激励
python -m canlab replay out/trace.json --json-out out/stats.json

# 6. 跑全部测试
python -m pytest tests/ -q
```

### 写一个自己的脚本

```text
/* my_test.capl —— 验证 ECU 每 10 ms 发一帧，且抖动不超过 2 ms */
variables
{
    msTimer tTick;
    message 0x123 status;
    int     sent;
    int     maxDeviation;
}

on start
{
    testCaseBegin("periodic-check");
    setInterval(tTick, 10);          /* 周期定时器，自动重装 */
}

on timer tTick
{
    sent = sent + 1;
    status.byte(0) = sent & 0xFF;
    output(status);                  /* 立刻发上总线 */
    if (sent >= 100) { cancelInterval(tTick); }
}

on stopMeasurement
{
    testAssertEquals(sent, 100, "发了 100 帧");
    testCaseEnd();
}
```

```bash
python -m canlab run my_test.capl --report my_test.xml
```

脚本引擎支持的语法见 [`canlab/capl/parser.py`](canlab/capl/parser.py) 的模块文档；
可用内建函数见 [`canlab/capl/interp.py`](canlab/capl/interp.py) 末尾的 `BUILTINS` 表。

---

## 模块说明

| 模块 | 文件 | 职责 |
|------|------|------|
| 帧层 | `canlab/frame.py` | 位级编解码、位填充/去填充、CRC15/17/21、DLC 映射、标准帧/扩展帧/远程帧 |
| 总线 | `canlab/bus.py` | 多节点逐位非破坏性仲裁、自动重发、错误帧影响、负载率与统计、trace 导出/载入 |
| 错误处理 | `canlab/errors.py` | 五类错误检测、TEC/REC 计数规则、error-active→error-passive→bus-off 状态机、128×11 恢复、错误注入 |
| ISO-TP | `canlab/isotp.py` | SF/FF/CF/FC、12 位与 32 位长度、块大小、STmin（毫秒/微秒编码）、N_As/N_Ar/N_Bs/N_Br/N_Cs/N_Cr 六类超时、拼包与异常上报 |
| UDS | `canlab/uds.py` | 9 个服务（0x10/0x11/0x14/0x19/0x22/0x27/0x2E/0x31/0x3E）、15 个 NRC、P2/P2\* 定时、抑制正响应位、安全访问先决条件与失败锁定 |
| 脚本引擎 | `canlab/capl/` | `lexer.py` 词法、`parser.py` 递归下降 AST、`interp.py` 解释器 + 虚拟时间调度器 + 测试原语 |
| 报告 | `canlab/report.py` | JUnit XML（`testsuites/testsuite/testcase/failure`）与文本报告 |
| 命令行 | `canlab/cli.py` | `run` / `selftest` / `replay` 三个子命令 |

---

## 真实跑出来的数字

> 下面每一个数字都是在本机（Windows + Python 3.8.0）实际跑出来的，可以用仓库里的
> [`tools/collect_numbers.py`](tools/collect_numbers.py) 与
> [`tools/collect_uds.py`](tools/collect_uds.py) 一键复现：
>
> ```bash
> python tools/collect_numbers.py   # CRC 向量 / 仲裁 / bus-off / demo 汇总 / 规模
> python tools/collect_uds.py       # UDS 服务与 NRC 覆盖
> ```

### 1. 测试

```
1065 passed in 18.04s
```

（截图式原文来自 `python -m pytest tests/ -q`；不含"跳过/警告"以外的任何隐藏项。）

### 2. CRC 校验向量

CRC 用移位寄存器逐位实现（多项式 0x4599 / 0x1685B / 0x102899，初值 0，不做输出反转）。
因为没有可以抄的官方测试向量文件，这些数值是**本实现自算并锁定为回归基线**的（README 的
"已知简化"里会说明这一点）：

| 输入 | 输出 |
|------|------|
| `crc15([])` | `0x0000` |
| `crc15([1])` | `0x380A` |
| `crc15(0x123456, 24 bit)` | `0x20F0` |
| `crc15(0xDEADBEEF, 32 bit)` | `0x7A20` |
| `crc17(0x123456, 24 bit)` | `0x9D39` |
| `crc21(0x123456, 24 bit)` | `0x13C611` |

整帧的黄金样例（`encode` → `decode` 往返一致）：

| 帧 | CRC | 位流长度 | 填充位数 | 数据字节 |
|----|-----|---------|---------|---------|
| std 0x123 dlc=4 `11 22 33 44` | CRC15 `0x669E` | 82 位 | 2 | 4 |
| std 0x000 dlc=0 | CRC15 `0x0000` | 55 位 | 7 | 0 |
| std 0x7FF dlc=8 `FF×8` | CRC15 `0x56BD` | 127 位 | 15 | 8 |
| ext 0x1ABCDEF dlc=8 `00..07` | CRC15 `0x2975` | 141 位 | 10 | 8 |
| rtr 0x321 dlc=8 | CRC15 `0x776F` | 48 位 | 0 | 0 |
| fd 0x100 dlc=12 | CRC17 `0x14185` | 162 位 | 14 | 12 |
| fd 0x100 dlc=15 `00..3F` | CRC21 `0x1C0A` | 596 位 | 28 | 64 |

错误检测能力（测试里逐位验证）：**单比特翻转全部被检出**（对 16/24/32 位位流逐位翻转，
无一漏检）；**双比特翻转**在 16 位位流的 120 种组合中全部被检出。

### 3. 仲裁实验

两节点同时开始发送 `0x100` 与 `0x200`（均为 4 字节数据帧，500 kbit/s）：

```
ECU_A(0x100): sent=True  仲裁获胜=True   失败位=-   重发=0
ECU_B(0x200): sent=True  仲裁获胜=False  失败位=2   重发=1
总线发送顺序: 0x100 → 0x200
仲裁场消耗: 3 位（在第 2 位分出胜负）
```

- **ID 小的先成功**，ID 大的节点在第 2 位读到显性位后立即退出仲裁并**自动重发 1 次**
- 两帧都完整上了总线，**没有丢帧**（这正是"非破坏性仲裁"的含义）
- 背靠背发送时总线负载率 100.00%（用 `--gap N` 可以插入最小空闲位来改变这个值）

其他仲裁规则（都在测试里逐条验证）：数据帧胜过同 ID 的远程帧（在 RTR 位分出胜负）；
基本 ID 相同时扩展帧在 SRR/IDE 位置取胜。

### 4. bus-off 与恢复

对一个节点持续注入位错误（每帧一次）：

```
注入位错误次数            : 32
首次进入 error-passive    : 第 16 次注入, TEC = 128
TEC 峰值                  : 256
最终状态                  : bus-off
恢复到 error-active       : 需要 1408 个隐性位 = 128 × 11
恢复后 TEC / REC / 状态   : 0 / 0 / error-active
折算虚拟时间              : 2.816 ms（500 kbit/s）
```

TEC 每检测到一次发送错误 +8，所以 32 次错误把 TEC 推到 256（> 255 → bus-off），
恢复条件严格是 128 次"连续 11 个隐性位"（任何一次读到显性位都会把计数清零重来）。

### 5. ISO-TP：多帧传输与 STmin

60 字节报文在经典 CAN 上的分帧：

```
payload 60 字节 -> 9 帧
帧类型: FF, CF, CF, CF, CF, CF, CF, CF, CF
每帧长度: [8, 8, 8, 8, 8, 8, 8, 8, 6]
```

流控与 STmin 生效验证（接收方在 FC 里给出 STmin = 5 ms）：

```
STmin = 5 ms 时发送 8 个连续帧耗时 : 40.0 ms（正好 8 个 STmin 时隙）
发送帧数 / 拼回字节数              : 9 / 60
STmin = 0 时耗时（对照）           : 0.0 ms
```

六类超时（N_As / N_Ar / N_Bs / N_Br / N_Cs / N_Cr）都在实现里，并且都可以独立配置与触发，
测试里逐个验证了"到点触发 / 未到点不触发 / 触发后中止传输并记录错误"。

60 字节在 CAN FD 上只需 2 帧（FFFF 携带 60 字节数据 + 一个 CF）。

### 6. UDS：服务与 NRC 覆盖

服务覆盖（全部返回正响应，共 11 次往返）：

| 服务 | 请求 | 响应（hex） |
|------|------|------------|
| 0x10 会话控制 | `10 03` | `50 03 00 32 13 88` |
| 0x22 读数据 | `22 F1 90` | `62 F1 90 56 49 4E 30 ...` |
| 0x27 安全访问 | `27 01` → `27 02` | `67 02` |
| 0x2E 写数据 | `2E 12 34 5A` | `6E 12 34` |
| 0x22 读回 | `22 12 34` | `62 12 34 5A` |
| 0x19 读 DTC | `19 01 FF` | `59 01 FF BD 00 01` |
| 0x31 例程控制 | `31 01 FF 00` | `71 01 FF 00` |
| 0x3E TesterPresent | `3E 00` | `7E 00` |
| 0x14 清 DTC | `14 FF FF FF` | `54` |
| 0x19 再读 DTC | `19 01 FF` | `59 01 FF BD 00 00` |
| 0x11 ECU 复位 | `11 01` | `51 01` |

负响应覆盖了 **10 种 NRC**：

```
0x11 serviceNotSupported              0x31 requestOutOfRange
0x12 subFunctionNotSupported          0x33 securityAccessDenied（含例程透传）
0x13 incorrectMessageLengthOrInvalidFormat
0x22 conditionsNotCorrect             0x35 invalidKey
0x24 requestSequenceError             0x36 exceedNumberOfAttempts
                                      0x37 requiredTimeDelayNotExpired
```

另外还实现了 `0x78 requestCorrectlyReceivedResponsePending`（非负响应，用于延长 P2\*）与
`0x14 / 0x7E / 0x7F`。P2/P2\* 定时用虚拟时钟验证：响应迟于 P2（默认 150 ms）会抛超时，
收到 `0x78` 之后改用 P2\*（默认 5100 ms）判定。

### 7. 脚本引擎与 6 个 demo

| 脚本 | 验证的协议行为 | 断言 | 事件回调 | 定时器回调 | 报文 发/收 | 虚拟时间 |
|------|---------------|------|---------|-----------|-----------|---------|
| `01_periodic_jitter.capl` | 周期报文与抖动检测 | 5/5 | 102 | 100 | 100/100 | 951.000 ms |
| `02_diag_flow.capl` | UDS 完整诊断流程 | 29/29 | 17 | 0 | 16/16 | 800.000 ms |
| `03_dtc_read_clear.capl` | DTC 读取与清除（含状态掩码） | 29/29 | 21 | 0 | 20/20 | 1000.000 ms |
| `04_arbitration.capl` | 逐位仲裁优先级与重发 | 16/16 | 1 | 0 | 0/7 | 0.000 ms |
| `05_error_injection_busoff.capl` | 错误注入与 bus-off 恢复 | 21/21 | 1 | 0 | 0/1 | 3.000 ms |
| `06_isotp_multiframe.capl` | ISO-TP 多帧传输与 STmin | 14/14 | 13 | 0 | 12/12 | 43.000 ms |
| **合计** | | **114/114** | **155** | **100** | | |

（"报文 发/收"里的"收"统计的是总线上观察到的帧数，包含其它节点发出的帧。）

抖动实测：ECU 标称 10 ms 周期 + ±0.5 ms 确定性伪随机抖动，100 帧里
**最大周期偏差 = 1.0 ms**，**平均偏差 = 0.4 ms**，断言阈值 2.0 ms。

---

## 测试与 CI

```
$ python -m pytest tests/ -q
1065 passed in 18.04s
```

测试分布（9 个文件、5464 行）：

| 文件 | 覆盖内容 |
|------|---------|
| `tests/test_frame.py` | 位级编解码黄金样例、位填充/去填充、CRC15/17/21、单/双比特错误检测、DLC 边界、帧结构非法组合、错误帧/过载帧 |
| `tests/test_bus.py` | 逐位仲裁（含 RTR、扩展帧、三节点、同 ID）、重发与统计、负载率、错误注入、trace 导出/载入 |
| `tests/test_errors.py` | 五类错误检测、TEC/REC 计数规则与边界、三种状态迁移、128×11 恢复、显性位打断恢复计数 |
| `tests/test_isotp.py` | STmin 编解码、SF/FF/CF/FC、12/32 位长度、歧义检测、块大小、六个超时、乱序/丢包/重复包、端到端耗时 |
| `tests/test_uds.py` | 9 个服务 × 正常/异常路径、10+ 种 NRC、P2/P2\* 超时、安全访问先决条件与锁定、会话切换复位安全状态 |
| `tests/test_capl.py` | 词法（含错误）、语法（含错误）、运算符全覆盖、控制流、定宽整型语义、事件派发、定时器、测试原语、确定性 |
| `tests/test_report.py` | JUnit XML 结构与可解析性、文本报告、文件写出、报告逐字节可复现 |
| `tests/test_e2e.py` | 6 个 demo 全部跑通、报告生成、CLI 各子命令、跨进程确定性、全栈串联（位级→总线→ISO-TP→UDS） |
| `tests/test_cli_encoding.py` | CLI 的 UTF-8 输出自适配（Windows CI 上 stdout 是 cp1252 时会撞 `UnicodeEncodeError`，这个用例锁住修复） |

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) 是**双平台 × 双版本**矩阵：

- 平台：`ubuntu-latest` + `windows-latest`
- Python：3.8 + 3.12
- 步骤：安装 pytest → 跑全部测试 → 逐个跑 6 个 demo 脚本（断言必须全过）→ 校验 JUnit 报告生成且可解析

---

## 确定性：连跑两次逐字节一致

脚本引擎使用**虚拟时间**：定时器、超时、事件派发全部按虚拟毫秒推进，所有随机性都来自
一个可指定种子的确定性伪随机函数（`jitterMs`），因此：

**同一个脚本、同一个 `--seed`，连跑两次的产物逐字节一致。**

校验方式（三条命令，任何一条不通过就说明确定性被破坏了）：

```bash
# 方式 1：跑两次，比较报告文件的 SHA256
python -m canlab run demo/scripts/02_diag_flow.capl --text-report /tmp/a.txt --quiet
python -m canlab run demo/scripts/02_diag_flow.capl --text-report /tmp/b.txt --quiet
sha256sum /tmp/a.txt /tmp/b.txt        # 两个摘要必须相同

# 方式 2：直接用 Python 比较（等价于测试里的断言）
python - <<'PY'
import hashlib, subprocess, sys
digests = []
for i in range(2):
    subprocess.run([sys.executable, "-m", "canlab", "run",
                    "demo/scripts/02_diag_flow.capl",
                    "--text-report", "out_%d.txt" % i, "--quiet"], check=True)
    digests.append(hashlib.sha256(open("out_%d.txt" % i, "rb").read()).hexdigest())
print(digests[0]); print(digests[1]); assert digests[0] == digests[1]
PY

# 方式 3：跑测试（tests/test_e2e.py 里对 6 个 demo 都做了这件事）
python -m pytest tests/test_e2e.py -q -k deterministic
```

本机实测的 SHA256（文本报告，`--seed 1`）：

| 脚本 | SHA256（文本报告） |
|------|------------------|
| `01_periodic_jitter.capl` | `44a17257be98a2e3be6f7887c92cef608617c34498fcf3f89d58f93044f05cd8` |
| `02_diag_flow.capl` | `2f33890547d7f5aa082c381c38859e25efb60c8afe472b2f42a0f1aa93acd2a7` |
| `03_dtc_read_clear.capl` | `cb74b725d61900d8e79021bf1def5399854880f65f81a4379d3bd870ddaab3ac` |
| `04_arbitration.capl` | `2593ab827d1c48521412317d51e960bcf148612b4e3261efed74374091164c45` |
| `05_error_injection_busoff.capl` | `0b0cd664ae99192533e6e1beeb6aff96930792c84452d39202506600a6d40bef` |
| `06_isotp_multiframe.capl` | `f5e03034d99bd64132f25f934eb76532f3cab603e6d2bfa39080e272f43fd692` |

换成别的 `--seed` 会得到不同的抖动序列（测试里也有断言验证这一点），
所以"一致"来自种子可控，而不是输出恰好与随机无关。

---

## 已知简化与未实现

这一节如实列出**没有做到**或**刻意简化**的地方，不做美化。

### 协议层

1. **CAN FD 的位定时切换未建模**。BRS 位被正确编解码，但不模拟"仲裁段用 500 kbit/s、
   数据段切到 2 Mbit/s"的实际时序变化；所有位统一按同一个位时间计时。
2. **CAN FD 的 CRC 与填充细节做了简化**：FDF 帧的固定填充位（stuff count）没有单独建模，
   统一按经典 CAN 的位填充规则处理。
3. **经典 CAN 的 CRC15 测试向量是自算的**。我没有 ISO 11898-1 的官方测试向量文件，
   因此 README 里给出的 CRC 数值是"本实现自算并锁定为回归基线"——它能证明实现是稳定的、
   单/双比特错误都能检出，但**不能**证明它与标准的参考向量逐位一致。
4. **TEC/REC 的少数细节规则未实现**：例如 error-passive 节点检测到错误时标准的
   "额外 +8" 附加规则、bus-off 之后可选的快速恢复路径。核心的 ±8 / ±1 / 127 截断 /
   255 阈值 / 128×11 恢复都实现了。
5. **错误帧的位级构造是固定形式**：错误标志 6 位显性 + 8 位错误界定符，按标准不做填充；
   但没有建模多个节点同时发错误标志时的叠加细节。
6. **总线仿真中接收与发送是同步的**：一帧刚"上线"，接收方的事件处理器就会执行。
   真实总线有传播延迟与位定时，这里没有。
7. **一个脚本节点不会把自己 output() 出去的帧作为"收到的报文"**（等价于关闭 loopback 的
   控制器）。`on message` 处理器仍会收到本节点发出的帧（这样单脚本就能同时扮演被测 ECU 与
   测试仪，见 `demo/02`、`demo/03`），但 `testWaitForMessage(同 ID)` 不会被自己刚发的帧满足。

### 工程层

8. **DBC 文件导入未实现**。`getSignalValue` / `setSignalValue` 只按"起始位 + 位长"从数据场
   取小端位段，不解析 DBC、不做信号打包/缩放/偏移、不做多路复用信号。
9. **没有 ODX/PDX 工程支持**。DID、DTC、例程都是代码里注册的，不是从 ODX 工程导入的。
10. **UDS 服务子集**：只实现了 9 个服务。`0x23/0x24/0x28/0x2F/0x34/0x35/0x36/0x37/0x3D/0x85`
    等没有实现（其中 0x34/0x35/0x36 是刷写必需的）。
11. **S3 定时只做了"超时回落默认会话"**，没有做 S3 定时器的完整 keep-alive 行为。
12. **脚本引擎不是 CAPL 的超集**：没有 `struct`、没有 `enum`、没有指针、没有可变参数、
    没有 `dowhile` 之外的循环形式差异处理、没有 CAPL 的数据库信号事件 `on signal`、
    没有诊断事件 `on diagResponse`；字符串支持很弱（拼接与比较，没有格式化）。
13. **浮点数按整数截断**：脚本里 `float` / `double` 声明会被当作整数处理。
14. **trace 重放器是极简的**：只按时间戳把帧重新发上总线，不校验时序、不做回放一致性断言。

### 验证层

15. **没有与真实硬件或商业工具做过交叉验证**。所有行为都只与"我自己按标准文本实现的模型"
    一致，没有第三方参照。
16. **没有做过性能测试**。1000 多个用例都在 20 秒内跑完，但没有测过百万帧级的吞吐。
17. **没有做过模糊测试**。位流解码的健壮性靠手写的边界用例覆盖，没有随机输入压力测试。

---

## 不能声称什么

以下是这个项目的**诚实边界**，原话保留、不做美化：

- **我没有 CANoe / TSMaster（同星）的许可证，也从未使用过这两款工具。本项目不是它们的替代品**，
  只在脚本语法上借鉴了 CAPL 的书写习惯（`variables {}` 块、`on message 0x123`、`msTimer`、
  `output()`、`write()`）。词法分析器、语法分析器、解释器全部自研。
- **我没有真实 CAN 硬件**（没有 PCAN、没有 Vector 接口卡、没有周立功适配器），
  **没有真实 ECU**，**没有整车或台架环境**。所有的总线行为都是软件逐位仿真出来的。
- **不建模物理层**：不模拟收发器、终端电阻、线束反射、EMI、位定时与采样点、晶振容差。
  它不会告诉你在真实线束上会不会出错。
- 这个项目**不是工业级的、不是生产可用的**，它的定位是教学与技术验证。
- 里面的每一个数字都是本机实跑的结果；跑不出来的东西，上面已经列在"已知简化与未实现"里了。

---

## 开发过程中踩到的坑

这部分是真实调试记录，写下来是因为它们比"最终能跑"更有参考价值。

### 1. 用**已填充**的位流做仲裁 —— 协议理解错误

一开始我拿 `encode(frame)` 的输出（已经做过位填充）逐位比较来判仲裁。结果是
`std 0x123` 会输给 `ext 0x123`，甚至两个不同 ID 的节点也会出现反直觉的结果。

根因：位填充是**发送方各自根据自己的数据独立施加**的。两个节点在同一个位序号上可能
位于不同的字段（一个还在 ID 里，另一个已经到控制场），直接比较填充后的位流毫无意义。

修法：仲裁必须在**未填充的逻辑位**上比较，发送时才用填好的位流，并且要把
"前 N 个逻辑位对应到已填充位流的第几位"算出来，避免重复计入总线负载。

### 2. 解码器按位扫描已填充位流 —— 一个把自己绕进去的 bug

解码时我原本是"边走位流边推进下标"，同时在头部去填充。这在**填充位恰好落在字段边界**时
必然错位：`0x000`（全零 ID）这种帧会在前 5 个零之后插填充位，于是后面所有字段的位置
都偏了一位。

修法：改成两步——先按帧结构算出"SOF..CRC 序列"应有的**原始位长**，再枚举窗口长度，
用"去填充后恰好得到这么多原始位、且填充位数与原始位序列自洽"这个**不变量**定位边界。
这个不变量对合法帧是唯一解。

### 3. 偏移一位：`IDE` 位到底算仲裁场还是控制场

标准帧的 IDE 位是**仲裁场的一部分**（它在 RTR 之前、决定后续仲裁场长度），我却把它放进了
控制场，导致解码器在标准帧上多算了 2 位、在扩展帧上少算了 2 位——现象就是"解码出来的
DLC 是 6，但我发的是 4"。

教训：位级协议里"字段从哪里开始"必须对着图逐位画一遍，不能凭记忆。

### 4. 解释器生成器漏水：`yield` 还是 `return`

脚本解释器用生成器实现，我一开始的写法是：

```python
def _eval(self, node):
    def gen():
        ...
        return value      # 想让 yield from 拿到返回值
    return gen()
```

问题在于：`yield from` 会**同时**传播"生成器 yield 出去的值"和"生成器 return 的返回值"，
我把两者混用了，于是赋值语句拿到的常常是**生成器对象本身**，而不是值——表现为
`int()` 报 "argument must be a string, a bytes-like object or a number, not 'generator'"。

修法：统一约定——**只有等待请求才 yield，值一律用 return**；同步实现的节点包一层
`_value_generator()` 适配；再加一道 `_unwrap_generator()` 兜底。

### 5. 挂起的处理器丢掉了作用域和剩余语句

CAPL 测试脚本最自然的写法是"发请求 → `testWaitForMessage` → 断言响应内容"。
我第一版实现里，处理器遇到等待请求就挂起，但**没有保存调用帧和后续语句**，
于是：

- `int r; r = testWaitForMessage(...)` 里恢复后 `r` 变成未定义变量；
- 挂起点之后的所有语句（包括断言）直接不执行了——报告里断言数从 29 掉到 3。

修法：引入 `SuspendedHandler`，把**调用帧、`this` 绑定、生成器、剩余语句列表**一起存下来，
恢复时还原栈帧再继续。这个 bug 花的时间最多，因为"断言变少"看起来像测试写错了。

### 6. 同步仿真下的"自己应答自己"竞态

本仿真器里 `output()` 是同步的：请求帧刚上线，ECU 的 `on message` 处理器就执行并
把应答也发了出来。于是测试脚本里

```text
sendRequest(...);                       /* 内部调用 output() */
testAssertEquals(testWaitForMessage(0x7E8, 100), 0, "...");
```

在 `output()` 返回的**同时**应答就已经到达了，而 `testWaitForMessage` 还没来得及注册等待者，
于是它永远等不到、每次都要等满 100 ms 超时。

修法：`testWaitForMessage` 在注册等待之前先扫一遍"本次已到达的报文"，命中就直接返回；
超时那一刻再做最后一次检查。同时 `_on_bus_frame` 里先派发 `on message` 处理器、再唤醒等待者，
保证"等到响应后立刻断言响应内容"读到的是**这一帧**的观测值而不是上一帧的。

### 7. ISO-TP 的 12 位与 32 位首帧长度**同形**

`0x10 0x00 <4 字节长度>`（32 位扩展形式）与"总长度恰是 4096 的整数倍"的
12 位形式 `<0x1H> <0xLL>` 的 PCI 字节**完全一样**。我一开始想靠"哪个解释更自洽"来猜，
结果 60 字节的帧被解成了 66051 字节。

修法：编码端保证不产生歧义帧（12 位形式下，若首帧数据的前 4 字节按大端解释大于声明长度，
就直接报错而不是发出一个无法唯一解码的帧）；CAN FD 默认走 32 位扩展形式彻底回避。

### 8. 块大小（BS）与同步应答的交错死锁

demo 06 里发送方发满一个块（BS=4）后进入"等下一个流控帧"，但接收方的 FC 在**第 4 帧传输
过程中**就已经回过来了，于是发送方把刚到手的 FC 当成"还没收到"而重新进入等待，双方互等。
现象是：前 4 帧发出去了，然后虚拟时间一路跑满 200 ms 却一帧没动。

修法：不要用单个布尔标志表示"是否在等 FC"，而是记录**已被授权的块号**，用块号比较判断
是否还需要等。这是"同步仿真"和"真实异步总线"最不一样的地方，非常容易写错。

### 9. 计数器的更新时机：`output()` 之前还是之后

同一个 demo 里，`sendConsecutive()` 里我写的是"先 `output(txMsg)`、再把 `cfSent` 自增"。
由于 `output()` 是同步的，接收方在自增之前就看到的是**过期的计数**，于是"块发完了没有"
判断错误，块边界永远不会触发。

修法：**先更新状态再 output**。这类"副作用发生在通信之前还是之后"的坑，在同步模型里到处都是。

### 10. 整数有符号性：`int` 是 16 位补码

脚本里写 `setTimer(t, 10 + jitter)`，当 `jitter = -0.5 ms` 时，我的实现把负数按无符号截断成
`65531`，于是定时器被设成 65541 ms——脚本表面正常，实际什么都不发生。

根因：我最初把所有整型都按无符号处理。CAPL 的 `int` 与 C 一样是**有符号**的。

修法：按声明类型分别处理有/无符号，有符号类型在截断后按补码解释回负数。
顺带发现 `busBitrate()` 返回 500000 会被截断成负数，于是改成返回 kbit/s。

### 11. 测试写错了，不是实现对错了

调试过程中有好几处"失败"最后查明是**我的测试期望错了**：

- 断言"基本 ID 相同时标准帧胜出"——实际上扩展帧在 SRR/IDE 位取胜，是我记反了；
- 断言"同 ID 时不会产生仲裁记录"——仲裁场完全一致时两个节点都能发出去，实现是对的；
- 断言"10 个零插 3 个填充位"——实际是 4 个（第 20 位之后还会再插一个），我算错了；
- 断言"清 DTC 按 DTC 号过滤"——实现是按**故障组（最高字节）**过滤的，测试没读文档。

教训：测试失败时先确认"标准要求的是什么"，再决定改代码还是改测试。
**不允许为了让测试通过而放宽断言**，但"把错误的期望改成正确行为"是必须做的。

### 12. Windows 下的字节级确定性陷阱

跨进程比较 CLI 输出时一直失败，diff 出来发现差异全在**临时目录路径**上
（`...\tmpXXXX\r.xml` vs `...\tmpYYYY\r.xml`）。报告本身（XML 与文本报告文件）
是逐字节一致的。顺手修掉了另一个更隐蔽的问题：CLI 原来把**完整脚本路径**写进
报告，于是报告内容依赖运行位置；改成只写文件名之后，换台机器跑同样的脚本也能
得到逐字节一致的产物。

另外 PowerShell 里用 here-string 传中文给 `python -c` 会被按 GBK 弄乱，
所以仓库里所有含中文的脚本与文档都用文件写入工具落盘，
不用 `python -c "…中文…"` 这种形式。

### 13. CI 假红：Windows runner 上 stdout 是 cp1252

第一次跑 CI 时，Ubuntu 与 Windows 的**测试全部通过**，但 `canlab selftest` 这一步
红了：

```
UnicodeEncodeError: 'charmap' codec can't encode characters in position 12-13
```

根因：本地我一直带着 `$env:PYTHONIOENCODING='utf-8'`，而 CI 里没有。Windows runner 的
标准输出编码是 cp1252（英文系统的代码页），一打印中文就炸。这不是"CI 环境有问题"，
而是**工具本身的可移植性缺陷**——一个命令行工具不该要求调用方先设置环境变量。

修法：CLI 启动时主动把 `sys.stdout` / `sys.stderr` 重配置成 UTF-8
（`stream.reconfigure(encoding="utf-8")`，并用 `getattr` 兜底不支持重配置的流对象），
同时补了 `tests/test_cli_encoding.py` 把"cp1252 stdout 下也能打印中文"锁进回归测试。
另外 CI 里"XML 报告用例数 >= 50"这个断言也是我自己写错的：JUnit 报告里的 `testcase`
数是**脚本声明的用例数**，断言数记在 properties 里，两者不该混为一谈。

### 14. cp1252 的连带失败：确定性校验

同一个编码问题还让 CI 里"同 seed 两次运行报告逐字节一致"这一步红了——因为那一步通过
`subprocess.run([...])` 调用 CLI，子进程同样没有 `PYTHONIOENCODING`，于是子进程直接
崩在 `UnicodeEncodeError` 上。一个编码问题同时打红两个看起来无关的步骤，
这也是"CI 红要多看几层日志"的原因。

---

## 许可证

[MIT](LICENSE)
