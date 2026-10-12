# 本地数据工具

脚本位于本 skill 的 `scripts/memory_store.py`，仅依赖 Python 3 标准库。默认数据库位置：

- macOS：`~/Library/Application Support/idea-to-insight/memory.sqlite3`
- Linux：`$XDG_DATA_HOME/idea-to-insight/memory.sqlite3`，未设置时 `~/.local/share/idea-to-insight/memory.sqlite3`
- Windows：`%LOCALAPPDATA%/idea-to-insight/memory.sqlite3`

设置 `WRITING_SKILL_DATA_DIR` 可让同设备的多个 Agent 指向同一数据目录。只支持同一设备顺序读写；不要在多设备同步盘上并发修改。

通过 Python 运行，避免依赖执行权限：

```bash
python3 scripts/memory_store.py list-suspended
python3 scripts/memory_store.py list-pending-candidates
python3 scripts/memory_store.py create-topic --title "主题" --direction "写作方向"
python3 scripts/memory_store.py checkpoint TOPIC_ID --stage dialogue --json '{"open_question":"..."}'
python3 scripts/memory_store.py suspend TOPIC_ID --reason "等待补充" --stage dialogue --json '{"open_question":"..."}'
python3 scripts/memory_store.py resume TOPIC_ID
python3 scripts/memory_store.py abandon TOPIC_ID --reason "当前不再继续"
python3 scripts/memory_store.py finalize TOPIC_ID --article-file /path/to/user-confirmed-final.md
python3 scripts/memory_store.py profile
python3 scripts/memory_store.py propose-candidate TOPIC_ID --article-sha256 ARTICLE_HASH --rule "..." --scope global --trigger "..." --evidence-json '["必要的反馈摘录"]'
python3 scripts/memory_store.py review-candidate CANDIDATE_ID --decision approve
python3 scripts/memory_store.py no-new-rule TOPIC_ID --article-sha256 ARTICLE_HASH --reason "没有足够证据归纳长期偏好"
python3 scripts/memory_store.py export /path/to/private-export.json
python3 scripts/memory_store.py import /path/to/private-export.json  # 默认仅预检
python3 scripts/memory_store.py import /path/to/private-export.json --apply
python3 scripts/memory_store.py integrity-check
```

`finalize` 读取用户确认的文章文件，将全文和 hash 保存在数据库中。调用前必须确认定稿，并以 UTF-8 写出准确版本；不得将草稿路径误当定稿。候选经验必须在话题已有定稿事件后创建。

每个候选必须用 `finalize` 返回的同一 `article_hash` 关联到具体定稿。`list-pending-candidates` 用于跨会话取回待审候选及其证据。若没有可靠候选，且没有待审/已激活规则，用 `no-new-rule` 为该定稿记一条幂等的“无新规则”学习结果。这样每篇定稿都会留下可审计事件，但不会强迫档案规则每篇变化。

## 错误处理

- 首次初始化失败：不声称已创建话题或保存；检查本地目录权限。
- `integrity-check` 失败：暂停写入；先备份并调查数据库，不直接删除或重建。
- 导入先运行默认 dry-run；若有冲突，不加 `--apply`。导入操作是事务性的。
- 用户迁移到新 Agent 时，复制 skill 目录并显式配置相同数据库路径，或通过加密/私密渠道迁移导出包；不要把 SQLite 与 JSON 导出包提交到代码仓库。
- Agent 没有 shell/python 或文件写权限时，说明不能执行持久化；临时对话内容不等于已保存状态。
