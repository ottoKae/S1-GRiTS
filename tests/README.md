# Tests and local artifacts / 测试与本地产物

Keep reusable regression tests in this directory and tracked by Git. CI executes
these tests against production code; ignoring or deleting `tests/` would remove
the evidence that fixes and optional tools still work.

| Content / 内容 | Placement / 位置 | Delivery / 交付 |
| --- | --- | --- |
| Regression tests and fixtures | `tests/` | Tracked; not installed in the runtime wheel |
| Reusable demo, diagnosis and benchmark tools | `tools/`, `scripts/`, `benchmarks/` | Track when documented and reusable |
| One-off local diagnosis | `tooling/diagnostics/<run-id>/` | Ignored; not a public tool directory |
| Disposable pytest output | `.test-tmp/<unique-run-id>/` | Ignored; pytest may clear this exact run directory |
| Acceptance reports / real-data evidence | A separate retained project evidence location | Preserve; never classify as disposable merely by filename |
| Manuscripts and paper analysis | Separate research checkout or local ignored directories | Not in software distributions |

```sh
python -m pytest tests/test_onboarding.py -q --basetemp .test-tmp/onboarding-review -p no:cacheprovider
```

Choose a fresh run ID; do not point basetemp at a Cube or a shared parent.
Install the documented dev/Web dependencies before Web checks so missing
TestClient dependencies do not silently skip them. Optional-backend and online
tests retain their explicit conditions; record skips separately from passes.

不要按 `test`、`tmp` 等文件名关键词批量删除文件。先确认是否仍被代码／CI／文档调用，
再决定保留、归档或忽略。Git 已跟踪文件不会因新增 ignore 规则而自动停止跟踪；
取消跟踪前应确认独立归档和原文件仍在。
