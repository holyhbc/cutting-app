# 当前状态快照

> 最后更新：2026-10-09
> 用途：新会话接手时先读这份文件，5秒内知道"现在能不能跑、下一步做什么"。

---

## 一、文件健康状态

| 文件 | 大小 | 行数 | 编译 | 说明 |
|---|---|---|---|---|
| `cutt/hbc-print-v7.7.4-多颜色完整优化版.py` | 229431 B | 5243 | ✅ 通过 | 条码打印主程序 |
| `cutt/zdno_edit.py` | 87204 B | — | ✅ 通过 | 裁剪工序数据维护 + 统计导出 |

验证命令：

```bash
cd /mnt/g/hbc/opencode/cutt
python3 -m py_compile "hbc-print-v7.7.4-多颜色完整优化版.py" && echo OK
```

---

## 二、重要澄清：文件从未丢失

之前一次 `ls -la` 显示主文件 0 字节，**这是误读**。

实际情况：

- 文件一直完好，5243 行 / 229431 字节
- 当时的"语法错误"来自一次**内存内字符串替换失败后的误判**，磁盘文件未被写坏
- **二维码阶段 0 的改动从未成功落盘**——文件里搜不到 `gen_short_id` / `build_qr_url` / `QR_BASE_URL`

结论：主程序处于**二维码开发之前的干净可运行基线**，可以放心继续开发。

---

## 三、二维码阶段 0 当前进度：未开始

目标文件里的关键锚点：

| 位置 | 内容 | 状态 |
|---|---|---|
| `hbc-print-v7.7.4-多颜色完整优化版.py:1805` | `make_full_barcode()` | 原始 `4+5+3+1 = 13位`，**未改动** |
| `hbc-print-v7.7.4-多颜色完整优化版.py:2334` | `QrCodeWidget` | 这是原有的模板打印 UI，**与标签二维码无关** |
| `create_barcode_pdf()` 内 `draw_label()` | 标签绘制 | 仍是纯条码，**未加二维码** |

---

## 四、数据库环境

| 项 | 生产库 | 测试库 |
|---|---|---|
| 服务器 | 192.168.0.73 | 192.168.0.73 |
| 库名 | `ShintHrmDb` | `ShintHrmDb-test` |
| 用户 / 密码 | sa / 1 | sa / 1 |
| 版本 | SQL Server 2000 | SQL Server 2000 |

**关键：主程序默认连生产库。** 任何测试必须显式指定：

```bash
export HBC_DB=ShintHrmDb-test
# 可选覆盖
export HBC_SERVER=192.168.0.73
export HBC_USER=sa
export HBC_PWD=1
```

未验证项：测试库 `ShintHrmDb-test` 是否已有 `jfzd2_detail` / `jfgz` / `ygzl` 表，**待只读核查**。

---

## 五、Git 状态

- 本次已创建**首个 commit**，作为可回滚基线
- 恢复某文件：`git checkout -- "cutt/hbc-print-v7.7.4-多颜色完整优化版.py"`
- 查看历史：`git log --oneline`
- **约定：每完成一个阶段（阶段 0/1/2…）就 commit 一次**

---

## 六、下一步（按顺序）

1. 核查 `ShintHrmDb-test` 表结构（只读）
2. 阶段 0：新增 `gen_short_id()` + 标签二维码绘制，**不覆盖现有条码**
3. 生成样张 PDF，人工验证微信扫码可用
4. 阶段 0 完成后再 commit 一次

详见 `TODO.md`。

---

## 七、硬性约束（每次改动都必须遵守）

1. **不碰生产库写操作**，测试一律 `HBC_DB=ShintHrmDb-test`
2. SQL Server 2000：禁止 CTE / MERGE / `sys.indexes` / 参数化 TOP
3. **改动条码前先 commit**，保证随时可回滚
4. 改 `make_full_barcode` 会把 Code128 从 13 位变 14 位，**必须先确认扫码枪兼容**
5. 扫码标签必须带 `CC` 字段（已发现 59 组 `(ZDNO,ZH)` 跨 CC 重复）