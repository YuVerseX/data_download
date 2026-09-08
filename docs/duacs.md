# DUACS 南海逐日数据

脚本：`download_duacs.py`。使用 Copernicus Marine 官方工具箱，账号与 GLORYS 共用；必要时运行 `copernicusmarine login`，不在命令行传密码。

## 默认配置

- 数据集：`cmems_obs-sl_glo_phy-ssh_my_allsat-l4-duacs-0.125deg_P1D`，正式 MY/DT All-satellite L4 日产品。
- 区域：105–125°E、0–25°N，160×200 个网格点，间距 0.125°。
- 时间：2001 年起，到服务端最新完整年。2026-09-08 实测时间坐标截至 2026-01-16，默认处理 2001–2025，不混入 NRT。
- 变量：`sla adt ugos vgos ugosa vgosa err_sla`。高度为 m，速度为 m/s。
- 输出：每年一个 `DUACS_SCS_daily_2001.nc`，NetCDF 压缩级别 1。

## 用法

```powershell
# 先预估全部默认年份，不下载数据、不创建输出目录
python download_duacs.py -n -o "D:\DUACS"

# 先下载一年
python download_duacs.py 2001 -o "D:\DUACS"

# 下载 2001 年起全部完整年份
python download_duacs.py -o "D:\DUACS"

# 指定年份和变量
python download_duacs.py 2001,2003-2005 -v sla adt -o "D:\DUACS"
```

`--bbox W E S N` 与 GLORYS 顺序一致，注意不同于 MUR 的 W S E N。
`--dataset-version 202411` 可固定目录版本；`--compression 0` 关闭压缩。
`--retries 3` 表示每年最多尝试三次，包含第一次；失败后退避等待，默认继续下一年，`--stop-on-error` 则停止。

## 进度与恢复

交互终端显示年度总进度，以及工具箱内部的处理任务进度；后者不等同于字节传输百分比。重定向日志或使用 `--no-progress` 时改为普通阶段日志。

临时文件在同一输出目录，完整校验后才替换正式文件。校验包括完整逐日时间轴（包含闰年、重复和缺日检测）、变量维度、经纬度网格，并逐块读取全部变量检查可读性。陆地缺测值是合法数据，不要求全网格非空。

重复运行会完整校验已有文件，并检查文件内保存的请求和源版本；通过后跳过。不匹配或损坏的正式文件默认保留，报告失败，只有显式 `--overwrite` 才替换。未完成年份重新下载，不提供字节级断点续传。不要让两个进程向同一目录下载同一年。

默认仅收完整年：本次查询中 2026 年尚未完整，显式请求会报错，不悄悄裁短；后续以服务端实际时间坐标为准，不使用可能过时的全局 `time_coverage_end` 属性。

## 流量

2026-09-08 使用工具箱 2.4.1、版本 202411、默认区域及七变量的 dry-run：

| 请求 | 工具箱文件大小估计 | 网络流量估计 |
| --- | --- | --- |
| 2001 年 1 月 | 26.52 MiB | 868.48 MiB |
| 2001 全年 | 312.07 MiB | 4.45 GiB |
| 2001–2005 合并 | 1.52 GiB | 8.89 GiB |

估计不是实际压缩后大小，也不是已测网络用量。用户运行截图中，默认南海七变量的 2001、2002 年文件分别为 92.76 MiB、93.42 MiB，均已通过脚本校验；这只是两个年份的样本，不保证其他年份大小一致。服务端分块导致流量放大；逐年请求可能重复读取同一块。首版选择逐年直下以限制失败重下范围，尚未实现多年合并，不能照搬 GLORYS 的分块边界。正式下载前对所需年份运行 `-n`。

## 科研注意事项

网格间距不是有效空间分辨率，逐日输出也不是每日独立观测。`sla` 为相对参考平均期的海平面异常，`adt` 为绝对动力地形，`ugos/vgos` 是地转流而非完整表层流。数据产品版本写入下载文件的请求溯源属性，原始科学属性保留。

## 官方来源与验证

- 产品目录：https://data.marine.copernicus.eu/product/SEALEVEL_GLO_PHY_L4_MY_008_047/description
- 用户手册：https://documentation.marine.copernicus.eu/PUM/CMEMS-SL-PUM-008-046-047-060-068.pdf
- 工具箱：https://toolbox-docs.marine.copernicus.eu/en/stable/usage/subset-usage.html

2026-09-08 开发验证：8 项离线测试通过（临时测试脚本已移除）；2001 年默认南海七变量 dry-run 通过；115–115.25°E、15–15.25°N 的 2×2 格点、2001 全年 `sla` 实际下载和完整读取通过，文件 29.71 KiB，重跑校验后跳过。随后用户运行截图确认默认南海 2001、2002 年已下载并通过脚本校验；2001–2025 全部年份是否完成尚未确认。

本机离线测试出现 `numpy.ndarray size changed` 二进制兼容性警告，本次 NetCDF 读写仍通过；未修改现有 Python 环境。
