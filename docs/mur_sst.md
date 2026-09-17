# MUR SST

脚本：[download_mur_sst.py](../download_mur_sst.py)。通过 Earthdata OPeNDAP DAP4 获取 MUR v4.1 的区域海表温度，每天保存一个文件。

## 默认配置

- 日期：2002-06-01 起；省略日期参数时选择当年，超出可请求范围的日期会裁去。实际发布情况以源站为准。
- 区域：南海 `105,0,125,25`，`--bbox` 顺序为 **西、南、东、北**。
- 网格：0.01°，默认区域 2501 × 2001 个格点。
- 变量：`analysed_sst,analysis_error,mask`，可用逗号分隔的 `--vars` 修改。
- 输出：当前工作目录下 `YYYY/MUR_SST_YYYYMMDD.nc4`，用 `-o` 更改根目录。

## 认证与运行

按 [README](../README.md) 安装依赖，准备 Earthdata Login token，保存为 `%USERPROFILE%\.edl_token` 中的一行文本。读取优先级为 `--token-file`、`EARTHDATA_TOKEN` 环境变量、默认文件。脚本检查可解析 token 的有效期；不要把 token 写入仓库或命令示例。

```powershell
# 本地计划，不联网、不读取 token
python download_mur_sst.py 20200101 -n -o "D:\MUR"

# 下载一天
python download_mur_sst.py 20200101 -o "D:\MUR"

# 按日并发下载一个月
python download_mur_sst.py 202001 -j 4 -o "D:\MUR"

# 年份或日期区间
python download_mur_sst.py 2020,2023-2024 -o "D:\MUR"
python download_mur_sst.py 20200101-20200630 -o "D:\MUR"
```

支持 `2020`（年）、`202001` 或 `2020-01`（月）、`20200115` 或 `2020-01-15`（日），也支持 `2020-2024`、`202001-202003`、`20200101-20200131` 等区间及逗号组合。

| 参数 | 作用 |
| --- | --- |
| `-j/--jobs` | 按日并发，默认 4，允许 1–8 |
| `--bbox 西,南,东,北` | 区域边界；按脚本网格索引获取数据 |
| `--vars analysed_sst,analysis_error,mask` | 选择变量 |
| `--token-file 路径` | 指定 token 文件 |
| `--overwrite` | 重下已有文件；当前 `-n` 统计未计入强制重下的文件 |

## 校验与恢复

脚本在正式下载前抽查区域边界索引对应的坐标。文件检查使用 h5py 打开 HDF5 并检查请求变量是否存在，**不逐块读取全部数据，也不核对已有文件的日期和区域**。更换区域时必须使用不同输出目录，避免旧文件被跳过。

先写临时文件，检查通过后替换最终文件。不支持字节续传，未完成日从头下载；已有文件通过上述检查则跳过，检查失败则重新下载。HTTP 404 会标记为源站未发布并不计入失败，因此退出码 0 不保证请求的每一天都有文件。

退出码：`0` 没有计入失败的日期，`1` 下载或服务请求失败，`2` 参数或本地认证配置错误，`130` 中断。代理配置见 [README](../README.md#网络与代理)。

## 数据含义与来源

`analysed_sst` 为 SST，单位 K；读取整数存储值时需应用源变量的 `scale_factor`、`add_offset` 和缺测标识。`analysis_error` 为分析误差，`mask` 为类别掩膜，另可请求 `sea_ice_fraction`。文件名中的时间为 09:00 UTC，不应据此当作该时刻的独立瞬时观测。

- 产品：`MUR-JPL-L4-GLOB-v4.1`，Concept ID `C1996881146-POCLOUD`。
- DOI：[10.5067/GHGMR-4FJ04](https://doi.org/10.5067/GHGMR-4FJ04)。
- [Earthdata Login](https://urs.earthdata.nasa.gov)。
- [PO.DAAC 引用说明](https://podaac.jpl.nasa.gov/CitingPODAAC)。
