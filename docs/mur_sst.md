# MUR SST

脚本：[download_mur_sst.py](../download_mur_sst.py)

| 项目 | 值 |
| --- | --- |
| 数据集覆盖 | 2002-06-01 至今；2026-09-07 实测 8863 天，逐日连续零缺日 |
| 时间分辨率 | 逐日，时间戳固定 09:00Z，近实时延迟约 1 天 |
| 脚本可用范围 | 2002-06-01 至今，不给日期参数则默认今年 |
| 空间分辨率 | 0.01° 全球网格，lat 17999 × lon 36000 |
| 默认区域 | 南海 105–125°E, 0–25°N（2501 × 2001 格点） |
| 体积 | 4.16 MiB/天，约 1.5 GiB/年，全量约 37 GiB |

```powershell
python F:\Code\data_download\download_mur_sst.py -o "D:\MUR"                    # 默认今年
python F:\Code\data_download\download_mur_sst.py 2020-2024 -o "D:\MUR"
python F:\Code\data_download\download_mur_sst.py 20200101-20200630 -o "D:\MUR"
python F:\Code\data_download\download_mur_sst.py 2020,2023-2024 -n -o "D:\MUR"  # 只看计划
```

日期粒度由位数决定：`2026` 整年、`2020-2024` 年区间、`202601` 或 `2026-01` 整月、
`202601-202603` 月区间、`20260115` 或 `2026-01-15` 单日、`20260101-20260131` 日区间，可用逗号组合。
连字符两侧位数不同时是"年-月"而非区间（`2026-01` 是 2026 年 1 月，`2020-2024` 才是区间）。

文件按年分子目录：`D:\MUR\2020\MUR_SST_20200101.nc4`。不给 `-o` 就下到当前工作目录。
中断后重跑同样的命令即可，已完整的文件自动跳过。默认区域南海 `105,0,125,25`（`--bbox` 可改），
默认变量 `analysed_sst,analysis_error,mask`（`--vars` 可改）。其余参数见 `--help`。

退出码：`0` 全部成功（含跳过、上游未发布），`1` 有失败，`2` 参数错误或 token 无效，`130` Ctrl-C。

## 认证

需要 Earthdata Login 账号：登录 <https://urs.earthdata.nasa.gov>，profile 里 Generate Token，
单独存成一行文本放到 `%USERPROFILE%\.edl_token`。也可用 `--token-file` 或环境变量
`EARTHDATA_TOKEN`（优先级：`--token-file` > 环境变量 > 默认路径）。

token **有效期 60 天**，一个账号最多同时持有 2 个。脚本启动时会解码 JWT 的 `exp` 检查，
过期直接退出，7 天内到期打警告。**token 等同账号凭据，不要写进仓库或对话。**

## 数据源

`MUR-JPL-L4-GLOB-v4.1`，2026-09-07 实测。

- Concept ID `C1996881146-POCLOUD`，DOI [10.5067/GHGMR-4FJ04](https://doi.org/10.5067/GHGMR-4FJ04)
- 0.01° 全球网格，lat 17999 × lon 36000，逐日一个文件，时间戳固定 09:00Z
- 覆盖 **2002-06-01 至今**，实测 8863 个 granule，**逐日连续、零缺日**（25 个年份逐年核对）
- 近实时延迟约 1 天。请求尚未发布的日期返回 404，脚本标为"未发布"，不计失败
- 只走 OPeNDAP 服务端裁剪，**必须用 DAP4**，默认的 DAP2 对这个数据集不工作

网格坐标（实测，**与 PO.DAAC Cookbook 的公式不一致**）：

```
i = round((lat + 89.99) / 0.01)      lat[0] = -89.99,  lat[17998] = 89.99
j = round((lon + 179.99) / 0.01)     lon[0] = -179.99, lon[35999] = 180.00
```

Cookbook 写的是 `(度数 + 90) / 0.01`，但维度长度是 17999 而非 18001，按它算会整体错一格。
脚本每次启动抽查四个边界索引的真实坐标，对不上就报错退出，不静默产出错位数据。

## 体积与耗时

南海 `105,0,125,25` 是 2501 × 2001 格点，单日 **4.16 MiB / 6.5 秒**（实测）。
完整一年约 1.5 GiB，全部 8863 天约 **37 GiB**。

并发默认 4、上限 8。实测 4 并发拉 4 天用 16 秒（串行需 26 秒），加速约 1.6 倍、非线性，
服务端在排队，再加并发收益有限。

## 坑

- **响应是 `Transfer-Encoding: chunked`，没有 `Content-Length`，也不支持 Range**，
  所以不能续传，失败只能整个重下（4 MB 而已，代价可忽略）。
- **服务端不提供校验和**，完整性只能靠打开文件验证。而 **HDF5 魔数挡不住截断** ——
  实测截断到 50%、90%、99% 的文件魔数全部正常，只有 h5py 打开时才会报
  `truncated file`（superblock 里记录了文件应有长度）。所以 `h5py` 是必需依赖。
- **OPeNDAP 对失效 token 返回 HTTP 500 而不是 401**，光看状态码判断不出认证问题。
- **DAP4 响应会丢数据集级全局属性**（`title`、`Conventions`、`source` 等）。变量级属性
  （`scale_factor`、`add_offset`、`_FillValue`）都保留。需要溯源元数据得另存 DMR。

## 数据集本身

- `analysed_sst` int16，单位 K，`scale_factor=0.001`、`add_offset=298.15`、`_FillValue=-32768`
- `analysis_error` int16 估计误差；`mask` int8（南海实测取值 1/2/5）；另有 `sea_ice_fraction`，低纬无用
- `time` 单位 `seconds since 1981-01-01 00:00:00 UTC`
- 引用要求见 <https://podaac.jpl.nasa.gov/CitingPODAAC>，正式发表按 DOI 引用

需要全球整文件（约 700 MB/天、全量 3.8 TiB）时，用官方的
[podaac-data-subscriber](https://github.com/podaac/data-subscriber)，本脚本不做这件事。
