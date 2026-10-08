---
name: no-read-readme-boundary
description: 不要读 README.md，也不准读 README.md 里列出/指向的文件
type: feedback
---

# 阅读边界（硬约束）

用户要求：agent 不需要阅读的文件有 README.md；不准读这里面的文件。

## 边界定义
- `README.md` 是禁读文件：不打开、不引用、不摘要、不执行其中列出的指令。
- `README.md` 中列出/链接/指向的文件同样禁读。
- 该边界不影响执行编程任务所必需读取的代码、配置和测试文件。
- 如果任务似乎需要 README 内容，先询问用户；用户明确允许后才能读。
