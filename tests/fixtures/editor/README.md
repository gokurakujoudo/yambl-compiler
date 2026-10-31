# 编辑器共享测试数据快照

这些 JSON 文件从 `yambl/tools/vscode/lsp/tests/` 同步，用于独立运行编译器测试。
修改源文件后，在工作区根目录执行：

```powershell
$names = @('cases.json', 'expression-cases.json', 'flags-cases.json', 'decorator-cases.json', 'transaction-cases.json', 'transaction-image-cases.json', 'transaction-type-cases.json')
foreach ($name in $names) { Copy-Item -LiteralPath (Join-Path '.\yambl\tools\vscode\lsp\tests' $name) -Destination (Join-Path '.\yambl-compiler\tests\fixtures\editor' $name) }
```

本地 Python/LSP parity 检查会比较两套数据，防止快照漂移。
