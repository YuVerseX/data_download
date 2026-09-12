# DUACS 南海逐日数据

脚本：`download_duacs.py`。使用 Copernicus Marine 官方工具箱，账号与 GLORYS 共用；必要时运行 `copernicusmarine login`，不在命令行传密码。

## 默认配置

- 数据集：`cmems_obs-sl_glo_phy-ssh_my_allsat-l4-duacs-0.125deg_P1D`，正式 MY/DT All-satellite L4 日产品。
- 区域：105–125°E、0–25°N，160×200 个网格点，间距 0.125°。
- 时间：2001 年起，到服务端时间坐标核实的最新完整年，不混入 NRT。
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

只接受完整年；显式请求不完整年份会报错，不裁短。覆盖以服务端实际时间坐标为准。

## 流量

默认南海七变量的年文件样本约 93 MiB，实际大小随年份和压缩变化。服务端分块会放大网络读取量，不能用落盘大小估计流量。脚本逐年请求，以限制失败重下范围；对需要的年份运行 `-n` 查看工具箱估算。

## 科研注意事项

网格间距不是有效空间分辨率，逐日输出也不是每日独立观测。`sla` 为相对参考平均期的海平面异常，`adt` 为绝对动力地形，`ugos/vgos` 是地转流而非完整表层流。数据产品版本写入下载文件的请求溯源属性，原始科学属性保留。

## 官方来源

- 产品目录：https://data.marine.copernicus.eu/product/SEALEVEL_GLO_PHY_L4_MY_008_047/description
- 用户手册：https://documentation.marine.copernicus.eu/PUM/CMEMS-SL-PUM-008-046-047-060-068.pdf
- 工具箱：https://toolbox-docs.marine.copernicus.eu/en/stable/usage/subset-usage.html
