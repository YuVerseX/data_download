# Argo 剖面

脚本：[download_argo.py](../download_argo.py)。从 GDAC Coriolis 节点获取全局核心 Argo 剖面索引，按位置、年份和文件模式筛选，再下载对应 NetCDF 剖面。

## 默认配置

- 区域：南海 `105,121,2,25`，`--bbox` 顺序为 **西、东、南、北**，用逗号分隔。
- 年份：默认不限制；`--years` 接受 1997 至当前年的年份或区间，实际覆盖取决于索引。
- 模式：`--mode all` 不按模式过滤；`D` 选择延时模式文件，`R` 选择实时模式文件。
- 输出：默认当前工作目录，保留上游 `dac/<数据中心>/<浮标号>/profiles/` 路径。
- 并发：`-j/--workers` 默认 4，允许 1–16，按剖面文件并发。

无需账号，安装与代理设置见 [README](../README.md)。

## 用法

```powershell
# 筛选统计，不下载剖面；可能下载全局索引
python download_argo.py --years 2020 -n -o "D:\Argo"

# 下载一个年份，保留当前索引中两种模式的匹配条目
python download_argo.py --years 2020 -j 4 -o "D:\Argo"

# 指定年份和延时模式
python download_argo.py --years 2010,2013-2015 --mode D -o "D:\Argo"

# 西北太平洋：先筛选统计，再移除 -n 下载剖面
python download_argo.py --bbox 100,180,0,60 --years 2010-2023 -n -o "F:\Argo_NorthwestPacific"
```

`--years` 不支持开放区间 `2010-`，需显式填写结束年。西北太平洋示例与 CMEMS SSS/SSD、OSTIA REP 的默认矩形一致；GLORYS 若需相同范围，要显式指定 `--bbox 100 180 0 60`。

## 索引与输出

```text
输出目录/
  ar_index_global_prof.txt.gz     全局索引缓存
  index_selected.csv             本次筛选清单
  failed.txt                     下载失败时写入的清单
  dac/<数据中心>/<浮标号>/profiles/*.nc
```

索引缓存 7 天内复用；`--refresh-index` 强制更新。**`-n` 也会创建输出目录、获取或复用索引、写入筛选 CSV**，只是不下载剖面。全局压缩索引可能有数十 MiB，脚本流式读取，不需先整体解压。

需要重新统计覆盖时运行：

```powershell
python download_argo.py --refresh-index --years 2020 -n -o "D:\Argo"
```

## 校验与恢复

下载先写 `.part`，成功后改名。未完成文件重跑从头下载，不支持字节续传。**已有非空文件直接跳过，不重新验证 NetCDF 内容或大小**；`--overwrite` 会重下所有选中文件。

`failed.txt` 用于查看失败项，不作为下一次运行的任务输入；成功重跑也不会自动删除旧清单。判断本次结果应看退出码和日志。退出码：`0` 成功或跳过，`1` 有剖面失败，`2` 参数错误，`130` 中断。

## 数据使用与来源

剖面是离散观测，不是逐日规则网格。模式筛选不代替变量质量控制；使用前应检查文件内的数据模式、质量标识和调整后变量。`--mode all` 下载当前索引中的匹配条目，不保证同一剖面同时保留 R、D 两个版本。矩形筛选也不等于海域掩膜。

- [GDAC 数据入口](https://data-argo.ifremer.fr/)。
- 数据集 DOI：[10.17882/42182](https://doi.org/10.17882/42182)。
- [Argo 项目](https://argo.ucsd.edu/)，正式使用按数据提供方要求致谢。
