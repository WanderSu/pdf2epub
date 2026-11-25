## 改动类型

<!-- 只影响 CI 跑哪几层检查，不代替 review；与实际改动不符会被打回。 -->

- [ ] 纯文档 / 截图 / LICENSE / 模板
- [ ] Python 引擎 / 测试 / config / scripts
- [ ] 桌面前端（`desktop/src/**`）
- [ ] Rust / Tauri 壳（`desktop/src-tauri/**`）
- [ ] 发布形态 / 版本号（`cli.spec`、`tauri.conf.json`、版本号文件）
- [ ] 依赖升级（锁文件）
- [ ] CI 配置自身（`.github/workflows/**`、`scripts/ci_scope.py`）
- [ ] 安全敏感路径（`net.py`、`safe_zip.py`、`mineru_http.py`、`epub/verify.py`）

> CI 会按改动路径自动判定范围（`scripts/ci_scope.py`）：锁文件、安全路径、CI 自身、
> 外部 fork PR 与 push 到 `main` 一律强制全量；出现不认识的路径也按全量处理。
> 想主动跑全量请打 `ci:full` 标签，或在 Actions → CI → workflow_dispatch 里选
> `scope=full`。纯文档 PR 仍会跑版本号一致性、凭证/产物门禁与本地链接检查。

## 这个改动做了什么

<!-- 一两句话说明用户能感知到的行为变化;纯重构请写明"无行为变化" -->

## 风险

<!-- 影响哪些路径/后端/平台;最坏情况是什么;有没有兼容性问题 -->

## 测试

<!-- 贴真实命令与结果,不要只写"已测试" -->

```
uv run pytest -q
uv run ruff check
```

## 回滚方式

<!-- 例:revert 本提交即可;或需要同时清理某个缓存目录/恢复某个配置 -->

## 自查

- [ ] 一个提交只做一件事,提交说明写清风险/测试/回滚
- [ ] 没有提交真实 Token、PDF/EPUB 样本、`work/`、`output/`、`logs/`
- [ ] 没有删除已有的缓存兼容逻辑
- [ ] 没有降低 EPUB 校验严格度
- [ ] 没有改变文件名、工作目录与产物恢复语义
- [ ] 若动了 `net.py` / `safe_zip.py` / `epub/verify.py` / 凭证逻辑，已人工复核异常、超时、重试与体积上限（SECURITY.md 范围内）
