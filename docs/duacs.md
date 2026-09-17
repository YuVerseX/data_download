# DUACS 海平面与地转流

脚本：[download_duacs.py](../download_duacs.py)。通过 Copernicus Marine Toolbox 获取 MY/DT All-satellite L4 逐日产品，按年保存。

## 默认配置

- 数据集：`cmems_obs-sl_glo_phy-ssh_my_allsat-l4-duacs-0.125deg_P1D`。
- 年份：省略时从 2001 年选至远端时间坐标核实的最新完整年；也可显式请求 1993 年起的完整年份。
- 区域：南海 `105 125 0 25`，顺序为 **西、东、南、北**；0.125° 网格，默认 160 × 200 点。
- 变量：`sla adt ugos vgos ugosa vgosa err_sla`。
- 输出：当前工作目录中的 `DUACS_SCS_daily_YYYY.nc`，压缩级别 1。`SCS` 不代表实际请求区域。

## 认证与用法

按 [README](../README.md) 安装依赖，运行 `copernicusmarine login` 保存凭据。网络设置见 [网络与代理](../README.md#网络与代理)。

```powershell
# 先核对一年计划，不下载数据
python download_duacs.py 2001 -n -o "D:\DUACS"

# 下载一年
python download_duacs.py 2001 -o "D:\DUACS"

# 使用默认完整年份范围
python download_duacs.py -o "D:\DUACS"

# 指定年份、变量和区域
python download_duacs.py 2001,2003-2005 -v sla adt --bbox 100 180 0 60 -o "F:\DUACS_NorthwestPacific"
```

| 参数 | 作用 |
| --- | --- |
| `--bbox W E S N` | 经度 -180..180、纬度 -90..90，单个框不跨日期变更线 |
| `-v/--variables` | 空格分隔的变量列表 |
| `--dataset-version` | 固定目录版本；默认由 Toolbox 选择 |
| `--compression 0..9` | 压缩级别，默认 1 |
| `--retries` | 每年任务总尝试次数，默认 3；不包含启动时的坐标查询 |
| `--overwrite`、`--stop-on-error` | 替换已有文件、某年失败后停止 |
| `--no-progress` | 关闭动态进度条 |

## 预检查、校验与恢复

`-n` 联网读取远端坐标、检查完整年并估算待下载年份，不创建输出目录。已有文件仍会读取并检查。显式请求缺日或未完整发布的年份会报错，不裁短。

按年串行，没有 `-j`。每个待下载年份先估算；剩余磁盘空间需达到估算文件大小的 1.2 倍。服务端存储分块可能放大传输量，不能从落盘大小推算网络流量。

下载先写同目录 `.partial.nc`，检查完整逐日时间轴、经纬度、变量维度，并逐块读取全部变量。通过后写入请求和源信息，再替换正式文件。陆地缺测允许保留。

重跑校验通过的文件会跳过。损坏、参数或记录的源信息不匹配时保留旧文件并报告失败，显式 `--overwrite` 才替换。未完成年份重新下载，不支持字节续传。同一输出目录不要并发运行多个实例。

## 数据含义与来源

`sla` 为海平面异常，`adt` 为绝对动力地形，单位 m；`ugos/vgos` 和 `ugosa/vgosa` 为地转流及其异常，单位 m/s。地转流不是完整表层流。网格间距不等于有效空间分辨率，逐日输出也不等于每日独立观测；参考期等定义以源属性和用户手册为准。

- [产品目录](https://data.marine.copernicus.eu/product/SEALEVEL_GLO_PHY_L4_MY_008_047/description)。
- [用户手册](https://documentation.marine.copernicus.eu/PUM/CMEMS-SL-PUM-008-046-047-060-068.pdf)。
- [Toolbox subset](https://toolbox-docs.marine.copernicus.eu/en/stable/usage/subset-usage.html)。
