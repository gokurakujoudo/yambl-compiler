# 从零开始

本教程在 Windows PowerShell 7 或更新版本中从一个独立模型生成数据接口，分别使用 SQLite 和文件后端。命令从编译器仓库根目录执行，要求 Python 3.14+ 和已安装依赖的 `venv`。Linux 或 macOS 可将 `venv\Scripts\python.exe` 换成 `venv/bin/python`，并按本机 shell 设置环境变量。

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -e '.[dev]'
$env:PYTHONUTF8 = '1'
```

## 1. 模型

示例文件在 [example](example/) 目录。模型根配置 [yambl-root.lclcfg](example/yambl-root.lclcfg) 包含：

```text
prefix: 'demo'
```

[model.yambl](example/model.yambl) 定义一个字典和实体：

```text
namespace catalog

decodes Categories {
    - 100 Book: 'Book'
    - 200 Tool: 'Tool'
}

entity Item {
    id item_id: string
    static attr name: string
    static attr category: code<Categories>
    static attr quantity: integer
    config attr multiplier: integer = 2
    computed attr doubled_quantity: integer {
        return self.quantity::value() * self.multiplier::value()
    }
}
```

完整名称由根前缀和文件 namespace 组成，因此实体是 `demo.catalog.Item`。`multiplier` 没有匹配的配置组且实例值为 `NO_ENTRY` 时使用默认值；`doubled_quantity` 根据读取结果计算，不占物理列。

## 2. 检查并生成

以下命令在 `build/tutorial-demo` 创建新的结果目录。请先确认该目录不存在；`generate` 要求目标目录不存在或为空。

```powershell
.\venv\Scripts\python.exe -m yambl_compiler check -o path docs\tutorial\example
.\venv\Scripts\python.exe -m yambl_compiler format -o path docs\tutorial\example\model.yambl -o check
.\venv\Scripts\python.exe -m yambl_compiler compile -o path docs\tutorial\example -o output build\tutorial-demo\model.json
.\venv\Scripts\python.exe -m yambl_compiler check-ir -o path build\tutorial-demo\model.json
.\venv\Scripts\python.exe -m yambl_compiler schema -o output build\tutorial-demo\ir-schema.json
.\venv\Scripts\python.exe -m yambl_compiler generate -o path build\tutorial-demo\model.json -o output build\tutorial-demo\generated -o package sample_model
```

生成目录中的 `python-schema.md` 是这个模型的 Python API；`schema.md` 和 `schema.sql` 描述存储结构。教程临时把生成目录加到 Python 模块搜索路径，正式使用也可安装生成的项目：

```powershell
$env:PYTHONPATH = (Resolve-Path build\tutorial-demo\generated).Path
# 正式安装的替代方式：.\venv\Scripts\python.exe -m pip install .\build\tutorial-demo\generated
```

## 3. 创建数据仓库并应用 Plan

创建两个配置文件。`path` 相对于配置文件所在目录解析，每个配置独立指向一个新仓库：

```powershell
"backend: 'sqlite'`npath: './data.sqlite'`n" | Set-Content -Encoding utf8NoBOM build\tutorial-demo\sqlite.lclcfg
"backend: 'files'`npath: './data-files'`n" | Set-Content -Encoding utf8NoBOM build\tutorial-demo\files.lclcfg
```

[initial.yaplan](example/initial.yaplan) 用完整实体名插入两个实例：

```text
plan initial {
    version 1
    group items {
        insert demo.catalog.Item (item_id, name, category, quantity) {
            'item-1', 'Notebook', demo.catalog.Categories.Book, 3
            'item-2', 'Ruler', demo.catalog.Categories.Tool, 5
        }
    }
}
```

分别创建并写入两个仓库；这两次提交相互独立。应用输出包含事务 `id`、导出文件 `path`、`status` 和 `changes`，请保留文件路径以便回滚。

```powershell
foreach ($backend in @('sqlite', 'files')) {
    $config = "build\tutorial-demo\$backend.lclcfg"
    .\venv\Scripts\python.exe -m sample_model create -o config $config
    .\venv\Scripts\python.exe -m sample_model apply -o config $config -o source docs\tutorial\example\initial.yaplan
    .\venv\Scripts\python.exe -m sample_model check-data -o config $config
}
```

`create` 不覆盖已有目标；同一 Plan 再次插入相同 ID 会失败。数据检查成功时输出的 JSON 包含 `"valid": true`。

## 4. 读取、计算、修改和回滚

[read_and_change.py](example/read_and_change.py) 对每个后端执行相同流程。它检查 Plan，打开仓库，读取原始值及两条批量计算结果，应用 [change.yaplan](example/change.yaplan)，比较旧读取器和新读取器，随后回滚并回滚该回滚：

```powershell
.\venv\Scripts\python.exe docs\tutorial\example\read_and_change.py build\tutorial-demo\sqlite.lclcfg docs\tutorial\example\change.yaplan
.\venv\Scripts\python.exe docs\tutorial\example\read_and_change.py build\tutorial-demo\files.lclcfg docs\tutorial\example\change.yaplan
```

每次输出 `apply=committed, rollback=committed, redo=committed`。`item-1` 初始数量为 3，计算结果为 6；应用修改后旧读取器仍返回 6，新读取器返回 8。回滚后新读取器返回 6，再次回滚后返回 8。脚本执行后 `item-1` 的 quantity 是 4；再次运行前需要先将该值恢复为 3，或使用新的数据仓库。读取器固定在创建时的快照，使用完应关闭；示例通过 `closing` 关闭各读取器。真实程序应先判断读取结果类型或状态，再访问成功结果的 `value`；示例用断言固定了已知数据。更多行为和错误处理见[参考手册](../reference/README.md)。

## 5. 扩充字典和配置组

在步骤 4 的结果上应用 [configure.yaplan](example/configure.yaplan)：它先完整替换字典 entries，保留原始 `Book` 和 `Tool` 三元组并加入 `Other`，再插入使用 `Other` 的实体；配置组对 `Book` 类实例将 multiplier 设为 3。每个后端分别执行：

```powershell
foreach ($backend in @('sqlite', 'files')) {
    $config = "build\tutorial-demo\$backend.lclcfg"
    .\venv\Scripts\python.exe -m sample_model apply -o config $config -o source docs\tutorial\example\configure.yaplan
    .\venv\Scripts\python.exe -m sample_model check-data -o config $config
    .\venv\Scripts\python.exe docs\tutorial\example\inspect_config.py $config
}
```

两个检查脚本均输出 `book=12, other=4`。`item-1` 的 quantity 为 4，匹配配置组后乘以 3；`item-3` 没匹配配置组，使用模型默认值 2。脚本还检查新增 code 和配置来源。

每次 apply 输出的 `.yatran` 路径应保留，以便回滚。也可用 CLI 回滚已有事务，把 `$transaction` 设为该次输出的路径，并按需要再次检查数据：

```powershell
.\venv\Scripts\python.exe -m sample_model rollback -o config build\tutorial-demo\sqlite.lclcfg -o source $transaction
.\venv\Scripts\python.exe -m sample_model check-data -o config build\tutorial-demo\sqlite.lclcfg
```

## 6. 时态、维度与分组回滚

这个独立例子在 [temporal](temporal/)：模型定义带 `venue` 维度的 snapshot `price` 和 SCD `rating`。[initial.yaplan](temporal/initial.yaplan) 先插入实例，再写入三个价格坐标和一个评级区间。[revise.yaplan](temporal/revise.yaplan) 按范围替换价格并更新评级，分为 `prices`、`ratings` 两组。

在仓库根目录执行；`build/temporal-demo` 必须是新目录，脚本会创建新仓库，不能对已有仓库重复运行：

```powershell
.\venv\Scripts\python.exe -m yambl_compiler check -o path docs\tutorial\temporal
.\venv\Scripts\python.exe -m yambl_compiler compile -o path docs\tutorial\temporal -o output build\temporal-demo\model.json
.\venv\Scripts\python.exe -m yambl_compiler generate -o path build\temporal-demo\model.json -o output build\temporal-demo\generated -o package temporal_model
$env:PYTHONPATH = (Resolve-Path build\temporal-demo\generated).Path
"backend: 'sqlite'`npath: './sqlite.db'`n" | Set-Content -Encoding utf8NoBOM build\temporal-demo\sqlite.lclcfg
"backend: 'files'`npath: './files'`n" | Set-Content -Encoding utf8NoBOM build\temporal-demo\files.lclcfg
foreach ($backend in @('sqlite', 'files')) {
    .\venv\Scripts\python.exe docs\tutorial\temporal\run.py "build\temporal-demo\$backend.lclcfg" docs\tutorial\temporal\initial.yaplan docs\tutorial\temporal\revise.yaplan
}
```

每个后端依次输出 `value=10.5 status=NO_ENTRY, message=`、`status=NO_ENTRY, message= partial=10, issues=1` 和 `partial rollback=committed, valid=True`。这里的 partial rollback 是按组回滚。第二行展示真实的 `Partial`：`Basket` 的两条 `Entry` 中一条 amount 为 10，另一条没有值。默认严格计算返回 `NO_ENTRY`；使用 `best_effort=True` 后得到 10 和一条 `NO_ENTRY` 问题。模型、数据和计算代码都在示例目录中。脚本查询 `2026-01-05` 的 primary 和 secondary 价格、primary 的 `[2026-01-01, 2026-02-01)` 价格列表，并验证 SCD 结束日期不含在区间中。

`revise.yaplan` 的价格替换选择 primary 和一月份，`expect any` 要求被选范围至少已有一条记录；评级替换使用 `expect one`，要求恰好一条。脚本先用 [expect_fail.yaplan](temporal/expect_fail.yaplan) 对不存在的维度运行预检，确认 `expect any matched 0` 且原数据未变；然后对 revise 做语法和期待值预检，再提交 Plan。`data.rollback(changed.path, groups=('prices',))` 只恢复价格组；评级仍是 B。最后对新读取器运行 `validate()` 并检查报告。全部 Python 代码见 [run.py](temporal/run.py)，读取结果分类及更多使用方式见[参考手册](../reference/README.md#时态和维度读取示例)。

## 7. 理解数据校验结果

模型可以用 `@unique_by` 声明实体字段的唯一组合，并在 entity 或 extend 中用
`constraint` 声明有序的 `let` 和 `assert`。生成包在 `apply` 和 `rollback` 提交前检查
最终候选；约束失败时事务不提交，CLI 输出包含实例、断言位置和失败状态的诊断。
`check-data` 和 Python 的 `reader.validate()` 可用于显式检查整个读取快照；取得成功结果后
仍须查看报告的 `valid` 和 `issues`。这两种检查覆盖模型声明的规则，不代替未声明的业务校验。

计算属性可以用 `field::records()` 读取物理记录，再用 `all` 检查每条记录。
`field::best_available()` 返回包含 `.value` 和 `.dimensions` 的选择结果；它与精确坐标
读取使用不同的选择规则。语言声明、日期回退及反向关系的完整示例见
[YAMBL 语言参考](https://github.com/gokurakujoudo/yambl)。
