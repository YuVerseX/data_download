# Argo（南海）

脚本：[download_argo.py](../download_argo.py)

| 项目 | 值 |
| --- | --- |
| 数据集覆盖 | 全球 1997 至今 |
| 区域覆盖 | 由运行时索引筛选结果确定，使用 `--refresh-index -n` 重新统计 |
| 时间分辨率 | 离散剖面，无固定间隔 |
| 脚本可用范围 | 1997 – 当前年（`--years`，上限随系统日期走） |
| 空间分辨率 | 离散剖面点，非网格 |
| 默认区域 | 南海 105–121°E, 2–25°N |
| 体积 | 单剖面约 18–22 KiB，总量取决于索引筛选结果 |

```powershell
python download_argo.py -o D:\Argo -n          # 只筛选统计，不下载
python download_argo.py -o D:\Argo             # 下载默认南海全部剖面
python download_argo.py -o D:\Argo --years 2010-2020 --mode D
python download_argo.py -o D:\Argo --bbox 99,122,0,25
```

默认范围是南海 `105,121,2,25`（`lon0,lon1,lat0,lat1`）。`--mode D` 只要延时模式，
`--years` 支持 `2010` / `2010-2020` / `2010,2013-2015`。中断后重跑同样的命令即可，
已下好的文件按存在性跳过，不重新校验大小；要强制重下加 `--overwrite`。

退出码：`0` 全部成功，`1` 有剖面失败（清单写在 `failed.txt`，重跑只补这些），
`2` 参数错误，`130` Ctrl-C 中断。

输出目录结构：

```
D:\Argo\
  ar_index_global_prof.txt.gz   全局索引缓存，7 天内复用
  index_selected.csv            本次筛出的剖面清单
  failed.txt                    有失败时才写
  dac\<dac>\<浮标号>\profiles\*.nc
```

脚本只用标准库加 tqdm。

## 数据源

<https://data-argo.ifremer.fr/>，GDAC 的 Coriolis 节点。

流程是先取全局剖面索引 `ar_index_global_prof.txt.gz`（约 56 MiB，每天更新，解压后 1 GiB 级，
脚本流式读取索引），按经纬度和年份筛出目标剖面，再逐个下 `dac/<相对路径>` 的 NetCDF。
单个剖面文件 18–22 KiB，纯 HTTP、无需注册。

## 覆盖与质量

```powershell
# 更新索引，只检查所选区域覆盖和数量
python download_argo.py --refresh-index -n --bbox 105,121,2,25
```

区域时间覆盖随索引、边界和质控模式变化，不应将一次筛选结果当作南海永久的观测截止日期。脚本下载核心 Argo 剖面；生化要素需另查对应数据产品。

`--mode D` 选择延时模式，`--mode R` 选择实时模式，默认两者都保留。使用前仍需检查变量质量标识。同一浮标重复剖面不能视为独立的空间采样；默认矩形也可能包含研究海域以外的点，可调整 bbox 或后续按海域掩膜筛选。

## 引用与许可

Argo 数据免费开放，无使用限制，但要求致谢。数据集 DOI 为
[10.17882/42182](https://doi.org/10.17882/42182)，标准致谢语：

> These data were collected and made freely available by the International Argo Program
> and the national programs that contribute to it (https://argo.ucsd.edu,
> https://www.ocean-ops.org). The Argo Program is part of the Global Ocean Observing System.
