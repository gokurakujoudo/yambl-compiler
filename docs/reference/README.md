# 编译器与数据接口参考

本文描述当前编译器和生成包的接口。完整可执行的通用模型与数据见[入门教程](../tutorial/README.md)。模型语法的完整定义见 [YAMBL 语言仓库](https://github.com/gokurakujoudo/yambl)。

## 命令行

安装后 `yambl` 和 `python -m yambl_compiler` 是等价入口。下表的 `path`、`output` 和 `package` 通过 lclang 的 `-o 名称 值` 传入；`path` 默认为当前目录。使用 `--help` 查看命令选项。

| 命令 | 输入和输出 | 作用 |
| --- | --- | --- |
| `check -o path PATH` | `.yambl` 文件或目录 | 检查语法、名称、类型及声明约束 |
| `format -o path PATH` | 文件或目录 | 格式化所选文件；`-o check` 或 `--dryrun` 只检查差异 |
| `compile -o path ROOT -o output MODEL.json` | 有根配置的模型仓库 | 编译完整仓库并写入 IR；`--dryrun` 不写入 |
| `check-ir -o path MODEL.json` | IR 文件 | 独立读取并检查 IR；`check_ir` 也可用 |
| `schema -o output SCHEMA.json` | 输出文件 | 导出 IR JSON Schema；`--dryrun` 不写入 |
| `generate -o path MODEL.json -o output DIR -o package NAME` | IR、新目录或空目录、snake_case 包名 | 生成 Python 包及双后端产物；`--dryrun` 不发布 |

状态码 0 表示成功，1 表示模型、文件或格式检查失败，2 表示命令用法或未预期的运行错误。warning 本身不使检查失败。诊断和结果在标准输出，工作流日志在标准错误。`-o check` 等无值选项放在末尾或下一个选项之前。只加载可信的 lclang 配置。

`check` 从文件或子目录向上寻找最近的 `yambl-root.lclcfg`，找到后检查整个仓库；嵌套根会导致父仓库检查失败。没有根配置时按所选文件或目录检查；目录含多个子仓库时应分别指定。隐藏目录、虚拟环境、构建目录和符号链接不参加发现。`format` 始终只处理所选范围，不因根配置扩大范围，也不要求引用已解析。

格式化输出 UTF-8 无 BOM、LF、四空格缩进，文件以一个 LF 结束；保留声明顺序、字符串拼写和注释内容。格式化先解析所有文件，语法或解码错误阻止任何写入；每个文件单独原子替换，发布时发生 I/O 错误则停止，已替换的文件保留。`---` 包围的 Markdown 块不参与语义。

## 模型配置与检查

模型仓库根目录的 `yambl-root.lclcfg` 只接受一个字面量 `prefix`，可为空：

```text
prefix: 'demo'
```

完整 namespace 是前缀与文件的 `namespace` 组合。同 namespace 的声明可跨文件使用；跨 namespace 在使用文件中显式 `from ... import ...`，导入相对当前仓库，不能跨仓库。文件最多一个 namespace，导入不支持别名和通配符。模型与字典 code 使用 ASCII 标识符，字符串使用单引号。

检查器涵盖实体、值类型、单继承、`extend`、`@override`/`@overload`、static/config/snapshot/SCD/计算属性、维度、别名、decorator、字典和集合等声明。静态检查用于模型约束，不承担业务数据完整性或实际实例存在性检查。`attr` 与 `static attr` 语义一致。实体引用保存目标 ID；`values` 保存值类型，`collection` 保存实体引用，`mapping` 的值不能是集合。普通 config 默认值限于已实现的标量字面量。

本地 `decodes` 的 code 按声明检查；`code<名称>` 必须解析到可见字典。未解析的 `code<'名称'>` 表示外部动态字典并逐处产生 warning，不会联网获取内容。flags 字典提供 `None` 和 `All`，只允许声明正单比特；普通字典不能使用 flags 的加减运算。自动子类型字典以 `实体完整名::subtypes` 为身份，成员来自继承关系与 `@decodes_rank`，这些派生字典只读。每个实体还提供由身份推导的 `entity_type` 和 `root_entity_type`，它们不是可写存储列。

## JSON IR 与生成

IR 包含内置定义、已解析声明、字段身份及实现、类型、计算和源位置。当前 `format_revision` 为 11；格式尚未固化，旧版本需从模型重新编译，不提供迁移。语义 SHA-256 摘要排除源位置和编译器版本。整数和 decimal 的载荷以字符串保精度；输出是 UTF-8 JSON。IR 校验不读取源模型，会检查内嵌内容、引用及派生视图的一致性。源注释和 Markdown 块不作为 IR 内容保存。

IR 保存 lookup 的键、解析后的字段路径、参数绑定及继承视图，并在独立校验时重建规则。`check` 检查 lookup 的字段路径和参数，`compile` 将规则写入 IR，`generate` 为支持的规则生成具名 Key、Matcher 和准备方法。当前支持 ID、static 和 SCD 字段及单值引用路径；计算、config、snapshot、集合展开和 `best_available` 不用于 lookup。

Python API 使用 `yambl_compiler.ir.compile_repository(path)`、`validate_ir(model)`、`write_ir(model, path)` 和 `load_ir(path)`；`validate_ir` 返回诊断字符串元组，空元组表示成功，其他操作的无效输入引发 `ValueError`，文件错误引发 `OSError`。使用教程模型时可执行：

```python
from pathlib import Path
from yambl_compiler.ir import compile_repository, load_ir, validate_ir, write_ir

model = compile_repository(Path('docs/tutorial/example'))
assert validate_ir(model) == ()
write_ir(model, Path('build/tutorial-demo/api-model.json'))
assert load_ir(Path('build/tutorial-demo/api-model.json')) == model
```

生成目录包含 Python 包、`pyproject.toml`、`schema.sql`、`schema.md`、`storage-manifest.json`、`schema-relations.puml`、`python-schema.md` 和 JSONL 模板。生成包携带 IR 和受摘要保护的运行时契约，当前 `runtime_revision` 为 12，物理存储 `layout_revision` 为 9。状态列以整数保存，单一实际类型的身份表省略类型判别列。依赖及 Python 版本写入生成项目配置；运行时不依赖编译器或模型源文件。生成前会拒绝后端不支持的字段类型，例如当前生成后端不支持 float 存储。生成包随模型重新生成，不提供数据库迁移。

### 存储生成支持范围

业务实体必须各有且仅有一个有效 ID，类型为 `string`、`integer` 或 `snowflake_id`；继承树中的实例共享根实体的 ID。字段支持 `string`、`bool`、`integer`、`snowflake_id`、`decimal`、`date`、普通字典 `code`、指向业务实体的引用及 `collection<业务实体>`。维度还须满足语言对维度类型的约束。内置实体的专用存储由生成器处理。

业务存储支持带时区的 `datetime`。Plan 使用 ISO 文本，Python 接口使用带时区的 `datetime.datetime`；输入统一转换为 UTC，存储为以 `Z` 结尾的文本。整秒不带小数，其他时间保留六位小数。业务存储目前不支持 `float`、flags code、业务结构化 value、`values<T>`、`mapping<K, V>` 或依赖类型；能通过静态检查不代表可以生成存储。生成器还检查 Python 实体名、方法和关键字冲突，以及 SQL 表名、列名和存储分组冲突；业务字段 `id`、`entity_type` 等保留名称也不能生成。出错时调整模型或等待后端支持。

例如，模型中的 `static attr instant: datetime` 可在 Plan 中写入
`'2026-01-01T08:00:00+08:00'`，读取结果为 UTC 的
`datetime(2026, 1, 1, tzinfo=timezone.utc)`。无时区时间、无效日期和超过六位的
小数秒会被拒绝。

## 仓库和读取

生成包的模型专属 `Data` 类通过 `await ...create(config_path)` 创建、`await ...open(config_path)` 打开；配置例子和完整代码见[教程](../tutorial/README.md)。`backend` 为 `sqlite` 或 `files`，`path` 相对于入口配置文件；创建不覆盖已有目标。数据接口可用 `with` 管理，`data.read()` 返回需关闭的独立读取器及固定快照。后续提交对新读取器可见，已打开的读取器仍观察旧快照。

生成的实体 handle 是冻结 dataclass，保留身份判等。`reader.get_item(id)` 返回结果，`get_item_many(ids)` 保持顺序及重复项。`read_字段()` 按参数读取；字段 property 首次访问加载完整字段。无时间、无维度的属性直接返回单值，有坐标的 property 返回包含字段专属记录的不可变元组；空坐标集是成功的空元组。时态点查询和范围查询接受对应日期，snapshot 范围采用 `[start, end)`，SCD 按有效区间查值，缺口为 `NO_ENTRY`。实体引用为 `EntityRef`，通过 `reader.resolve(ref)` 显式解析，悬空引用可返回 `ENTITY_NOT_FOUND`。handle 的 `entity_type`、`root_entity_type` 提供实际类型和继承根，也有相应 `read_` 方法。

`ReadResult[T]` 是 `Success[T]`、`NonNormalResult` 或 `Partial[T]`。`Success.value` 是值，`origin` 可标识存储、模型默认值、配置组或计算，`groups` 记录适用配置组；`NonNormalResult.status` 区分 `NOT_DEFINED`、`EMPTY`、`NO_ENTRY`、`NO_DATA`、`ENTITY_NOT_FOUND`、`ERROR` 和 `NOT_APPLICABLE`。`Partial` 仅用于带可用值及非错误遗漏明细的计算结果。先检查结果再取值；不要把异常状态当字符串或普通存储值。

`@relationship` 声明正向引用的基数，`@reverse_relationship` 声明不占物理存储的只读反向属性。显式数据校验和事务最终候选校验检查必填及同一坐标上的唯一性；snapshot 按日期和维度检查，SCD 按重叠区间检查。反向属性由正向数据读取，不能在 Plan 中写入；详细规则见语言参考。

读取器缓存规范化查询和已加载字段；`ERROR` 不缓存。关闭后已缓存数据仍可用，但需要新后端读取时返回 `ERROR`。范围列表每次返回独立副本。字典成员按读取器快照校验；扩充后新读取器可读取新 code，旧读取器保留原视图。运行时新增 code 使用字典专属类型，但不会为其生成新的具名枚举成员。

### 时态和维度读取示例

[可运行示例](../tutorial/temporal/run.py) 使用 `Instrument.price` 的 `day` 和 `venue`，以及 SCD 字段 `rating`。以下片段位于打开的数据接口及读取器上下文中：

```python
from datetime import date
from decimal import Decimal
from temporal_model import NonNormalResult, Status, Success

item = reader.get_instrument('i1').value
price = item.read_price(date(2026, 1, 5), venue='primary')
assert isinstance(price, Success) and price.value == Decimal('10.5')
missing = item.read_price(date(2026, 1, 6), venue='primary')
assert isinstance(missing, NonNormalResult) and missing.status == Status.NO_ENTRY
window = item.read_price_range(date(2026, 1, 1), date(2026, 2, 1), venue='primary')
assert isinstance(window, Success)
for observation_date, result in window.value:
    print(observation_date, result)
assert item.read_rating(date(2026, 1, 15)).value == 'A'
assert item.read_rating(date(2026, 2, 1)).status == Status.NO_ENTRY
```

`window` 的结束日期排除在外；不同 `venue` 是不同坐标。SCD 的结束日期同样排除在有效区间外。上面示例已知实体存在，实际应用也应检查 `get_instrument` 的结果。读取全部坐标可访问 `item.price`，其外层成功值是记录元组，每条记录内的 `value` 本身也是结果。

对单值或计算结果，可按类型区分正常、部分可用及非正常状态，下面为处理片段，`result` 来自生成包读取方法：

```python
from temporal_model import NonNormalResult, Partial, Success

if isinstance(result, Success):
    print(result.value, result.origin)
elif isinstance(result, Partial):
    print(result.value)
    for issue in result.issues:
        print(issue.status, issue.field, issue.message)
elif isinstance(result, NonNormalResult):
    print(result.status, result.message)
```

`Partial` 来自允许尽力计算的可用结果；普通静态读取不保证会产生它。不要仅以 truthiness 判断成功。时态教程中 `Basket` 聚合一个有值和一个缺失值的条目，下面片段取自完整脚本并可通过教程步骤运行：

```python
from decimal import Decimal
from temporal_model import NonNormalResult, Partial, Status

basket = reader.get_basket('b1').value
strict = basket.read_total()
assert isinstance(strict, NonNormalResult) and strict.status == Status.NO_ENTRY
partial = basket.read_total(best_effort=True)
assert isinstance(partial, Partial) and partial.value == Decimal('10')
assert partial.issues[0].status == Status.NO_ENTRY
```


## 计算和配置

计算字段不写物理列。表达式使用 `self.字段::value()` 读取依赖，时态和维度输入由依赖推导；额外可省略输入可通过 `@extra_input_param` 声明。点读取按精确坐标选择，`::best_available(...)` 使用 Hierarchy 排序和适用的回退规则。普通读取未要求时不会自动合并多个 Hierarchy。每个生成计算字段提供 `read_字段(...)` 和对应的批量 request 类与 `reader.read_实体_字段_many(...)`；批量结果保持请求顺序及重复项，整体和逐项结果均需检查。教程同时验证单次与批量计算。

计算默认严格传播非正常结果，`recover` 显式处理指定状态；`best_effort=True` 可保留部分成功贡献及 `Partial.issues`，但不把 `ERROR` 当作可忽略贡献。整数和 decimal 计算保持精度，`/` 返回近似 float，`//` 向下取整。并非所有静态可检查的模型类型都有存储实现。

ConfigGroup 的 `target_entity_type` 指定目标类型，`when` 中 static 字段按等值 AND 匹配，`then_set` 给 config 字段赋值。每个字段选最高 priority；同优先级同值可合并，冲突值产生 `ERROR`。配置组优先于实例值，实例为 `NO_ENTRY` 时才使用实际子类型默认值。条件中不适用于实际类型的字段不匹配。完整数据校验也会检查配置组及其映射。

## Plan、事务与回滚

`.yaplan` 用 `plan`、`version 1` 和有序 `group` 声明操作；每条数据记录单独占一行。[教程 Plan](../tutorial/example/initial.yaplan) 插入实体，[修改 Plan](../tutorial/example/change.yaplan) 更新字段。Plan 可省略模型摘要；提供时必须匹配生成包的 `MODEL_DIGEST`。操作包括实体 `insert`、`update`、`delete`，以及字段范围的 `replace`、`delete`；删除拥有数据的实体须明确处理其所属数据。字段范围、时间边界及字面量的完整语法见[语言仓库的事务参考](https://github.com/gokurakujoudo/yambl/blob/feat/language-foundations/docs/reference/transactions.md)。

`Data.validate_plan_syntax(path)` 不打开仓库，检查语法、模型摘要、目标、坐标和字面量类型，返回可复用的已解析 Plan。`data.validate_plan_expectations(plan)` 使用当前快照检查 `expect`，并按顺序追踪先前基于 ID 建立的身份和行坐标，不构造完整候选。可将返回的 Plan 传给 `data.apply(plan)`；应用会重新检查并校验最终候选。一个 Plan 可先插入 ID 再写其时态字段，也可对新 ID 使用 expect；不可依赖前序写入后通过非 ID 条件查找并修改，同一实例同一逻辑字段不可在一个 Plan 中多次修改。

### 范围、预期与分组回滚实例

[revise.yaplan](../tutorial/temporal/revise.yaplan) 包含以下操作：

```text
replace demo.records.Instrument.price expect any scope (key='i1', venue='primary', day in ['2026-01-01', '2026-02-01')) columns (venue, day, value) {
    'primary', '2026-01-10', 12.5
}
```

`scope` 选择实体 `i1`、指定维度和半开日期范围，`replace` 用给定记录替换该范围内全部记录；未选中的 `secondary` 坐标不受影响。`expect any` 要求操作前范围内至少有一条记录；`expect one` 要求恰有一条，省略时不设置该数量要求。时态字段的范围必须能确定所属实体，范围外的输入记录会被拒绝。示例的 `ratings` 组使用 `expect one`。

执行片段（`data` 为已打开的生成接口，Plan 和仓库为教程中的时态示例）：

```python
from pathlib import Path
from temporal_model import TemporalData

plan = TemporalData.validate_plan_syntax(Path('docs/tutorial/temporal/revise.yaplan'))
data.validate_plan_expectations(plan)
transaction = data.apply(plan)
rolled_back = data.rollback(transaction.path, groups=('prices',))
print(rolled_back.status, rolled_back.warnings)
```

这只回滚 `prices` 组，`ratings` 组继续生效。`expect` 的预检不发布数据，实际应用仍重新检查；冲突或字典引用问题可阻止最终提交。完整可运行脚本和准备步骤见[教程](../tutorial/README.md#6-时态维度与分组回滚)。

普通显式字典可以扩充；模型原始 code、decode、ordering 三元组必须保留，运行时新增条目可修改或移除。flags 和派生字典完全只读。引用新增 code 的实体字段、维度、时态值或配置映射仍存在时，删除该 code 会被最终候选校验拒绝；同一事务也可以移除引用和 code。回滚遵循相同约束。

`apply` 和 `rollback` 返回 `TransactionReport`，包括 `id`、`path`、`status`、`changes`、`warnings`、`publication_error`。`.yatran` 保存实际前后镜像、分组、执行时间、来源关系及完整性摘要；`data.export(id)` 可导出已提交事务。`data.rollback(path, groups=None)` 可回滚全部或选定组，回滚本身也生成事务。`conflicts` 表示合格的逆向变更已提交、后续冲突已跳过；`export_pending` 表示数据已提交但导出待恢复，重新打开仓库可恢复导出。遇这些状态应查看报告，不要盲目重放 Plan。

两个后端分别提交，不提供跨后端原子事务。调用方须保证同一仓库同时只有一个写入或恢复进程。事务和代际 ID 使用 worker ID 0 的 Snowflake 生成器，进程重启后复用前须确保墙钟已超过上次生成 ID 的时间。SQLite 使用原生事务；文件后端生成不可变代际并通过清单切换，发布需要文件系统支持硬链接。外部直接改写硬链接文件可能影响多个代际。

生成包 CLI 提供 `create`、`apply`、`rollback` 和 `check-data`，通过 `-o config 配置` 和写操作的 `-o source 文件` 传参。回滚可用 `-o groups` 指定组，参见命令帮助。写操作的 `--dryrun` 只检查配置和输入文档语法，不构造候选、不提交；`check-data --dryrun` 仍执行只读数据检查。

## 数据校验

应用与回滚校验变动记录及受影响关系，假定未变数据原本有效。`reader.validate()` 全量扫描仓库，返回包含 `ValidationReport` 的 `ReadResult`；检查成功后再检查 `report.valid` 和 `report.issues`。CLI `check-data` 输出 JSON，数据无效或读取失败时返回非零。检查包括物理记录、集合归属、SCD 重叠、配置映射及同字段引用环；不检查业务完整性，也不把悬空引用一律视作错误。绕过生成接口直接写入的数据应先做全量校验。

全量校验结果处理片段（`reader` 为已打开的读取器）：

```python
from temporal_model import Success

result = reader.validate()
if not isinstance(result, Success):
    raise RuntimeError(f'{result.status}: {result.message}')
if not result.value.valid:
    for issue in result.value.issues:
        print(issue.field, issue.message, issue.ids, issue.dimensions, issue.start, issue.end)
```

`issues` 提供字段或物理表、诊断、相关 ID、维度和时间边界；它为空时 `valid` 为真。完整运行示例包含通过后的断言和输出。
