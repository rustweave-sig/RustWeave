# 原生构建与测试记录

## 构建

- 工作目录：`testing/projects/xxHash`
- 命令：
  - `make clean`
  - `make xxhsum`
- 结果：成功
- 耗时：
  - `clean`：约 `0.11s`
  - `build`：约 `2.45s`
- 说明：
  - 顶层 `make xxhsum` 即可完成 CLI 目标构建
  - 构建过程使用项目自身在 `build/make/multiconf.make` 中定义的 `cachedObjs/` 缓存布局

## 测试

- 工作目录：`testing/projects/xxHash`
- 命令：
  - `make test_sanity`
  - `make check`
- 结果：成功
- 耗时：
  - `test_sanity`：约 `1.46s`
  - `check`：约 `1.47s`
- 说明：
  - `make test_sanity` 共通过 `49948` 个测试
  - `make check` 还会额外覆盖已构建出的 `xxhsum` CLI 与 benchmark 子命令
  - 原生路径下无需修改项目源码

## 本地兼容性处理

- 无
