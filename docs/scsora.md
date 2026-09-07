# SCSORA

脚本：[download_scsora.py](../download_scsora.py)

| 项目 | 值 |
| --- | --- |
| 数据集覆盖 | 2001–2024，逐年文件共 24 个，无缺年 |
| 时间分辨率 | 逐日 |
| 脚本可用范围 | 2001–2024 |
| 空间范围 / 分辨率 | 南海；上游无说明文档，分辨率未证实 |
| 体积 | 171.8 GiB/年，全量约 4.0 TiB |

```powershell
python F:\Code\data_download\download_scsora.py 2001 -o D:\SCSORA
python F:\Code\data_download\download_scsora.py 2001-2005 -o D:\SCSORA
python F:\Code\data_download\download_scsora.py 2001,2003-2005 -o D:\SCSORA
```

年份可以是单个 `2001`、区间 `2001-2005`，或两者组合 `2001,2003-2005`，可用范围 2001–2024。
不给 `-o` 就下到当前工作目录。中断后重跑同样的命令续传，别删 `.part`。其余参数见 `--help`。

退出码：`0` 全部成功（含已完整跳过），`1` 有年份失败，`2` 参数错误，`130` Ctrl-C 中断。

## 数据源

<https://www.hellosea.org.cn/SCSORA/>（2026-09-07 实测）

- `SCSORA_daily_{YYYY}.nc`，2001–2024 共 24 个，无缺年
- 单文件 `184476652676` 字节 ≈ **171.8 GiB**，24 个全量约 4.0 TiB
- 支持 Range 续传
- **上游没有校验和文件**，完整性只能靠字节数比对
- 只需 ~172 GiB 空闲空间，不用双份 —— 最后一步是同目录 rename，不发生复制

## 数据集本身

上游只有一个目录索引，没有说明文档、变量列表、许可条款。

- `SCSORA` 的官方定义没检索到，推测是 South China Sea Ocean ReAnalysis，**未证实**
- **引用与使用许可未知，正式使用前问数据提供方**
