# 灵感来源：[kucherenko/jscpd](https://github.com/kucherenko/jscpd)

- 星标：6,459
- 许可证：MIT
- 语言：Rust（v5 重写；v4 是 TypeScript，仍在 `master-v4` 分支维护）
- 它承诺解决：找出项目里被复制粘贴的代码。

## 它做对了什么

**1. 按语言切 token，不按空白切。** 224 种格式各有一套注释和字符串规则，所以字符串里的 `//` 不会被当成注释，Markdown 散文里的 `/*` 不会吞掉半个文件。这一条决定了工具有没有用——按行切文本的检测器，第一周就会被误报淹没。

**2. 用滚动哈希而不是两两比较。** 对每个 token 窗口算 Rabin-Karp 哈希，只有哈希相同的窗口才做逐 token 比对。这让 O(n²) 降到接近 O(n)，是它能处理几万文件的前提。

**3. 归一化分层做，默认关闭。** 精确克隆、忽略名字（Type-2）、忽略字面量、允许行间空隙（Type-3）分别是独立的开关，默认只报精确克隆。默认行为保守，是对的。

**4. 边界情况处理得很细。** `--ignore-identifiers` 不是简单地把所有标识符映射成同一个占位符（那样 `if` 和 `while` 会相等），而是记录「这个名字距离上一次出现有多远」——也就是 Baker 的参数化匹配。滑窗时这个距离只变一个 token，滚动哈希仍然一步一格。

**5. 工程上肯下笨功夫。** 8 个 crate 拆分（tokenizer / core / finder / reporter / similarity / semantic / basta / cli），8 个平台预编译二进制，15 种 reporter，SARIF、Code Climate、OpenMetrics 都有。作为一个每月被下载一千万次、被 GitHub Super Linter 和 MegaLinter 默认启用的工具，这些不是装饰。

## 它在哪里让人痛苦

**痛点一：它报「对」，不报「组」。** 同一个函数被复制到 12 个文件，你拿到的是 11 条记录，每条两个文件。要看出这是同一段代码被抄了 11 次，得自己在脑子里做并查集。

> It looks like the duplicate code report is shown always for code on two files. If we have, for example, the same method on 4 different files, we won't get a report saying that the block of code is found on all those files. Instead, we will see that the code is duplicated on file 1 and 2, and 3 and 4.
>
> It would be really helpful to group the duplicate code on all the files where it is found, so if we have the same block of code in 10 files we can fix it on all of them at once
>
> —— [#221](https://github.com/kucherenko/jscpd/issues/221)，2019-04-05 提出，至今 open

这条 issue 开了七年半。它不是性能问题，不是 bug，是输出模型的选择：内部数据结构是 `Clone { firstFile, secondFile }`，一个克隆天然只有两端。改成「一组位置」要动数据模型、动 15 个 reporter、动 SARIF 映射——所以一直没动。

七年半里，这个工具涨到六千四百星、每月一千万次下载，issue 里的诉求一次都没被满足。

**痛点二：许可证头被算成重复——但我没能复现。**

> All my source files starts with a comment that, besides author, date and similar information, has the license and copyright information. Twenty lines equal for all source files. These lines are flagged as duplicated obscuring the list of legitimate duplicates.
>
> —— [#788](https://github.com/kucherenko/jscpd/issues/788)，2026-05-11

我按这条 issue 造了 4 个文件：每个开头 10 行完全相同的版权注释，正文互不相同。jscpd 5.4.2 报 0 条克隆，加 `--mode strict` 也是 0 条。

原因是注释在默认的 mild 模式下就不进 token 流，二十行版权声明贡献 0 个 token，自然没法被匹配。

所以这一条在 v5 上我复现不了。它可能是 v4 的行为，也可能要更复杂的前置条件（Vue 的 `<script>` 块、模板字符串里的头）才触发。**我没有为它实现任何东西**——第一版写了一个"识别样板注释头并排除"的功能，后来发现它是个空操作（注释本来就不产生 token），删掉了。

这件事本身有结论：**一个切 token 的检测器，天然不会把纯注释的头当成克隆。** 这不需要功能，需要的是别在切 token 的时候保留注释。

**痛点三：`.gitignore` 不是 git 的 `.gitignore`。**

> `.gitignore` files are parsed in a manner inconsistent with their specification. ... the `.yarn/sdks/` and `.yarn/unplugged/` directories are searched for duplicates despite being gitignored, resulting in a "fatal: no such path" crash from `git blame` since these directories are not tracked.
>
> —— [#466](https://github.com/kucherenko/jscpd/issues/466)，2021-06-07

被忽略的目录里通常装着整个 `node_modules`。扫进去不只是慢，是结果直接被第三方代码淹没。

**痛点四：一张表就是一片克隆。** 字典字面量 `"key": "value",` 重复四十次，token 序列是 `S : S , S : S , …`。任何两张表都能匹配上，检测器分不清「表」和「抄」。

**痛点五：v4 时代的内存和卡死。** 37,213 个文件直接把 Node 堆打爆（[#66](https://github.com/kucherenko/jscpd/issues/66)，1,378 MB 后 GC 放弃），elasticsearch 仓库跑到不返回（[#184](https://github.com/kucherenko/jscpd/issues/184)），55,000 行的单文件要十几分钟（[#591](https://github.com/kucherenko/jscpd/issues/591)）。这些是 v5 换 Rust 的主要动因，现在基本解决了——但代价是 8 个 crate 和 8 个平台的构建矩阵。

## 我们的取舍

### 砍掉了什么

| 砍掉 | 为什么 |
|---|---|
| 224 种格式 → 11 套注释/字符串语法 | 覆盖 25 个扩展名。剩下 199 种格式的克隆不会让人加班 |
| 15 种 reporter → 控制台 / JSON / 退出码 | 报告是给人看的，或者给 CI 判断的，没有第三种用途 |
| Type-3（行间空隙）、Type-4（语义嵌入） | Type-3 报的是「相似」，不是「复制」，混在一起就没人看了。Type-4 要跑模型 |
| `--baseline`、`--changed`、`--blame`、`--history`、`--health`、`--dashboard` | 都是「在重复率之上再叠一层指标」。核心指标还没做准之前，叠上去只是让数字更可信地错 |
| MCP server、LSP、GitHub Action、Docker 镜像 | 一个 Python 文件不需要分发渠道 |
| 增量缓存、并行 worker | 纯 Python 单线程 3.1 秒扫完 27 万行，还没到需要并行的规模 |

### 保留了什么

- 按语言切 token（这是正确性的底线）
- 滚动哈希（这是能跑完的前提）
- `--names` 做 Type-2，但用最简单的实现，并在 README 里写清它会过度报告
- 阈值退出码（CI 唯一真正需要的功能）

### 加了什么

**1. 克隆类（clone class）作为报告的原子单位。** 一个类 = 一段代码 + 它出现过的所有位置。12 处重复是 1 条记录，不是 11 条。

**2. 真正按 git 规则解析 `.gitignore`。** 锚定、`!` 取反、目录限定、`**`、最后一条生效，都按 `gitignore(5)` 实现，并有一条测试专门验证嵌套 `.gitignore` 里的取反。

**3. 低信息量窗口过滤。** 40 个 token 里不同取值少于 20% 的窗口直接丢弃。字典表、枚举表、长参数列表都属于这一类。

**4. 按内容分组，而不是按「连过」。** 两个位置被一次匹配连起来，不代表它们装着同一段代码——A 和 B 有 50 token 公共段，B 和 C 有 44 token 公共段，A 和 C 可能毫无关系。所以并查集之后还要按区间内容的摘要再分一次组。没有这一步，一个类里会混进长度不同的东西，报告就在撒谎。

## 与原版的差异

| 维度 | jscpd 5.4.2 | copyfind 1.0.0 |
|---|---|---|
| 报告的原子单位 | 一对文件（`firstFile` / `secondFile`） | 一个克隆类（N 个位置） |
| 12 处重复 → 几行 | 11 行 | 1 行 |
| 许可证头 | 取决于 mode，可能计入 | 注释不进 token 流，不可能计入 |
| `.gitignore` | 自己实现，与 git 有出入（#466） | 按 `gitignore(5)` 实现 |
| 语言 | 224 种格式，8 个 crate | 11 套语法，1 个文件 |
| 输出 | 15 种 reporter | 控制台 / JSON / 退出码 |
| 安装 | npm / cargo / brew / docker / 8 个平台二进制 | `curl` 一个文件 |
| 速度（27 万行） | 1.9 s | 3.1 s |
| 峰值内存（27 万行） | 12 MB | 156 MB |
| Type-3 / Type-4 | 有 | 无 |
| 增量、基线、LSP、MCP | 有 | 无 |

速度和内存这两行是劣势，写在这里而不是藏起来：Rust 二进制就是快，纯 Python 换来的是「目标机器上只要有 python 就能跑」。

## 许可证说明

jscpd 采用 MIT 许可证，允许在保留版权声明的前提下复用代码片段。

本项目**没有复用 jscpd 的任何代码**，也没有阅读其 Rust 实现的具体写法——参考的是 README、`docs/rust.md` 里公开描述的算法流程（Rabin-Karp 滚动哈希 + token 归一化），以及 issue 里暴露出来的行为问题。Rabin-Karp 本身是 1987 年发表的公开算法。

`copyfind.py` 的每一行都是独立实现的，与 jscpd 的模块划分、数据结构、命名没有对应关系。
