# 测试

```bash
python tests/run_all.py          # 跑全部
python tests/run_all.py -v       # 详细
python tests/run_all.py -k 权限   # 只跑名字含「权限」的
```

## 约定

- **不连数据库、不联网**：需要写 SQL Server 的逻辑用 `FakeCursor` 验证
  「发了什么语句、带什么参数」
- **不碰真实数据**：每个用例建独立临时库，跑完删掉
- **零第三方依赖**：只用标准库 `unittest`，任何机器直接能跑

## 四个文件

| 文件 | 守住什么 |
|---|---|
| `test_permissions.py` | 登录、**工资数据不外泄**、改量归属 |
| `test_business.py` | 报工/改量规则、进度算法、数据库迁移 |
| `test_http.py` | 路由鉴权、重定向、推送令牌校验 |
| `test_sync.py` | 改量回写的 SQL 正确性、幂等、金额重算 |
| `test_desktop.py` | 桌面版 SQL Server 2000 语法限制、列契约、导出兜底 |

## 每个用例都对应一个真实踩过的坑

写测试时逐条对照历史 bug 加了回归，例如：

- 今天报完工要立刻能看到（日期上界曾写成开区间）
- 没报的工序不能被漏判成完工（MIN 只在有报工的工序上取）
- 扎数不被「扎×工序」交叉放大
- 字符串 `report_id` 绑 INTEGER 列会炸
- `cur_change_id` 之前根本没定义（改量功能从上线起就没生效过）
- `<a><button>` 嵌套会让浏览器误提交报工表单
- 报工成功后自动跳 `GET /api/report` → 404
- `/admin` 曾经完全没有鉴权
- 未登录访问 `/report` 曾经显示「标签未同步」页

## CI

`.github/workflows/tests.yml` —— push 到 main 或提 PR 自动跑：

- **三档 Python**（3.8 / 3.11 / 3.13），防止用��语法写代码只在本地跑
- **compileall** 全量语法检查，防止漏掉的文件语法错被测试漏过
- **`.cmd` 纯 ASCII 检查** —— 专门防「中文注释在 cmd.exe 里变乱码命令」那个坑

CI 上**不装 pyodbc**（Linux 装它要编译，慢且容易因为系统包翻车），
用 `tests/_bootstrap.py` 打桩。反正测试从不真正连库。

Windows 和 Linux 都验证过能全绿：

```bash
python tests/run_all.py          # 94/94
```
