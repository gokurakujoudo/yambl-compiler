# YAMBL 编译器

YAMBL 编译器检查和格式化模型，将模型编译为 JSON IR，并生成 Python 数据接口、SQLite 和 JSONL 文件存储。生成的接口支持读取、计算、事务应用、回滚和数据校验。

- [从零开始：创建模型并操作两种后端](docs/tutorial/README.md)
- [参考手册：命令、生成物、读取和事务](docs/reference/README.md)
- [YAMBL 语言定义](https://github.com/gokurakujoudo/yambl)

编译器要求 Python 3.14 或更新版本。在本仓库创建环境并安装：

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -e '.[dev]'
```

## 最简上手

以下命令在本仓库根目录的 Windows PowerShell 7 中运行，创建独立模型并编译；`build/hello-yambl` 必须尚不存在。

```powershell
$env:PYTHONUTF8 = '1'
if (Test-Path build\hello-yambl) { throw 'build/hello-yambl already exists' }
New-Item -ItemType Directory -Path build\hello-yambl -Force | Out-Null
"prefix: 'hello'" | Set-Content -Encoding utf8NoBOM build\hello-yambl\yambl-root.lclcfg
"namespace catalog`n`nentity Item {`n    id item_id: string`n    static attr name: string`n}" | Set-Content -Encoding utf8NoBOM build\hello-yambl\model.yambl
.\venv\Scripts\python.exe -m yambl_compiler check -o path build\hello-yambl
.\venv\Scripts\python.exe -m yambl_compiler compile -o path build\hello-yambl -o output build\hello-yambl\model.json
.\venv\Scripts\python.exe -m yambl_compiler check-ir -o path build\hello-yambl\model.json
```

成功时依次显示 `checked 1 file(s)`、`compiled IR` 和 `checked IR`；生成的 JSON 在 `build/hello-yambl/model.json`。根前缀和 namespace 使实体完整名成为 `hello.catalog.Item`。可继续按[教程](docs/tutorial/README.md)生成 Python 包、创建数据并操作两种后端。

## 开发

已激活环境时，可用 `yambl` 或 `python -m yambl_compiler`。开发者可运行 `python -m black --check src tests`、`python -m ruff check src tests`、`python -m mypy`、`python -m pyright` 和 `python -m unittest discover -s tests -v`。源代码的语句和分支覆盖率门禁由 `python -m coverage run -m unittest discover -s tests -v` 与 `python -m coverage report` 执行。
