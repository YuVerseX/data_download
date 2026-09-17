# data_download

科研数据下载脚本，一个数据源一个脚本。所有命令在仓库根目录运行，完整参数见 `python <脚本名> --help`。

## 脚本清单

| 脚本与文档 | 时间选择 | 默认区域与输出 |
| --- | --- | --- |
| [SCSORA](docs/scsora.md) · `download_scsora.py` | 必填年份，2001–2024 | 上游整文件，按年保存 |
| [MUR SST](docs/mur_sst.md) · `download_mur_sst.py` | 年、月、日或区间；默认当年 | 南海 105–125°E、0–25°N，逐日保存 |
| [Argo](docs/argo.md) · `download_argo.py` | 可选年份；默认索引中全部匹配剖面 | 南海 105–121°E、2–25°N，按剖面保存 |
| [GLORYS](docs/glorys.md) · `download_glorys.py` | 必填年份，1993–2025 | 南海 105–125°E、0–25°N，全深度，按年保存 |
| [DUACS](docs/duacs.md) · `download_duacs.py` | 可选年份；默认 2001 起至目录最新完整年 | 南海 105–125°E、0–25°N，按年保存 |
| [CMEMS SSS/SSD](docs/cmems_sss.md) · `download_cmems_sss.py` | 必填起止日期；日或月产品 | 西北太平洋 100–180°E、0–60°N，按源和年份保存 |
| [OSTIA REP](docs/ostia_rep.md) · `download_ostia_rep.py` | 必填起止日期；逐日数据 | 西北太平洋 100–180°E、0–60°N，默认按月保存 |
| [CCI SSS](docs/cci_sss.md) · `download_cci_sss.py` | 年份或起止日期；默认选至最新完整年 | 南海 105–125°E、0–25°N，七天滑动平均、逐日采样 |
| [CCMP](docs/ccmp.md) · `download_ccmp.py` | 年份或起止日期；默认 `2001-latest` | 南海 105–125°E、0–25°N，每天四时次，可另存日均 |
| [ERA5](docs/era5_flux.md) · `download_era5_flux.py` | 年份或起止日期；默认 2001 起完整最终 ERA5 年 | 南海 105–125°E、0–25°N，小时累计能量，可另存日均通量 |

时间参数能被接受，不代表源站已发布全部请求日期。各产品的时间定义、网格和质量标识不同，联合使用前需对齐。

## 安装与运行

```powershell
conda create -n data_download python=3.12
conda activate data_download
python -m pip install -r requirements.txt

# 查看单日计划
python download_cci_sss.py --start-date 2011-01-01 --end-date 2011-01-01 -n

# 下载单日，确认结果后再扩大日期范围
python download_cci_sss.py --start-date 2011-01-01 --end-date 2011-01-01 -o "F:\cci_sss"
```

`-o` 指定输出目录；更换区域、变量、深度或处理方式时使用不同目录，避免混用已有文件。相同命令重跑时的检查与跳过规则见产品文档。

| 数据源 | 认证准备 |
| --- | --- |
| MUR SST | Earthdata Login token，默认读取 `%USERPROFILE%\.edl_token` |
| GLORYS、DUACS、CMEMS SSS/SSD、OSTIA REP | Copernicus Marine 账号，运行 `copernicusmarine login` |
| ERA5 | CDS 账号、数据集条款和 `%USERPROFILE%\.cdsapirc` |
| SCSORA、Argo、CCI、CCMP | 脚本使用公开入口，不配置账号 |

## 预检查与下载量

`-n/--dry-run` 的行为因脚本而异，不能统一理解为离线操作：

| 脚本 | `-n` 的操作 |
| --- | --- |
| MUR SST | 只查看本地计划和已有文件，不联网、不读取 token |
| SCSORA | 向源站发送 HEAD 查询文件大小，不读取数据正文 |
| Argo | 获取或复用全局索引，筛选并写入 CSV；不下载剖面，索引本身可能有数十 MiB |
| GLORYS | 联网调用 Toolbox 估算，并创建输出目录 |
| DUACS、CMEMS SSS/SSD、OSTIA REP | 联网读取远端坐标、核对覆盖并估算待下载文件；不创建输出目录，但会检查已有文件 |
| CCI SSS、CCMP | 查询目录；CCMP 另查询一个文件的 HEAD，不下载 NetCDF |
| ERA5 | 查询公开目录并显示计划，不提交 CDS 任务 |

SCSORA 下载整年原件；CCMP 先下载全球日文件再裁剪。其他区域请求也可能读取边界以外的服务端存储块。输出大小不等于网络流量，需同时预留已完成文件和正在处理的临时文件空间。

## 网络与代理

脚本默认使用运行环境的代理设置。根据实际连接情况选择代理或直连，不固定要求某个产品必须开启或关闭代理。先用同一小请求比较线路；目录或认证成功不代表完整下载成功。

以下为 PowerShell 设置，只影响当前终端及其启动的进程。代理端口按本机配置修改。

```powershell
# 使用代理
$env:HTTP_PROXY = 'http://127.0.0.1:10808'
$env:HTTPS_PROXY = $env:HTTP_PROXY
$env:ALL_PROXY = $env:HTTP_PROXY
$env:NO_PROXY = 'localhost,127.0.0.1,::1'

# 使用直连（需要切换时执行）
Remove-Item Env:HTTP_PROXY,Env:HTTPS_PROXY,Env:ALL_PROXY -ErrorAction SilentlyContinue
$env:NO_PROXY = '*'
```

设置直连时保留 `NO_PROXY='*'`，避免 Python 使用 Windows 系统代理。VPN/TUN 仍可能转发流量。其他终端可设置等价环境变量。

CCMP 可直接使用 `--proxy direct` 或 `--proxy http://127.0.0.1:10808`。CCI 也有 `--proxy` 参数，但现有实现可能被环境代理覆盖；固定线路时使用上面的终端设置。其他脚本没有独立的代理参数。

## 并发与恢复

| 脚本 | 脚本层任务并发 | 中断后的处理 |
| --- | --- | --- |
| MUR SST | `-j/--jobs`，默认 4，范围 1–8，按日文件 | 检查已有文件，未完成日重新下载 |
| Argo | `-j/--workers`，默认 4，范围 1–16，按剖面 | 跳过已有非空文件，未完成文件重新下载 |
| SCSORA | 按年串行，无 `-j` | 保留 `.part`，支持字节续传 |
| GLORYS、DUACS、CMEMS SSS/SSD、OSTIA REP | 按输出文件串行，无 `-j`；Toolbox 负责内部任务调度 | 检查已有文件，未完成文件重新请求 |
| CCI SSS、CCMP | 按日串行，无 `-j` | 检查已有文件，未完成日重新下载 |
| ERA5 | 按月内日期块串行，无 `-j` | 检查已有文件，未完成块可能重新提交 CDS 任务 |

同一输出目录不要同时运行多个实例。各脚本的校验强度不同，尤其 MUR 不核对已有文件的请求区域，GLORYS 不核对已有文件的区域和深度；Argo 仅按文件非空判断是否跳过。

## 区域参数

- MUR：逗号分隔，`--bbox 西,南,东,北`。
- Argo：逗号分隔，`--bbox 西,东,南,北`。
- 其余支持区域裁剪的脚本：空格分隔，`--bbox 西 东 南 北`。
- CCMP 经度使用 0–360；其他脚本的允许范围见各自文档。SCSORA 不提供区域裁剪。

依赖见 [requirements.txt](requirements.txt)，数据和凭据的忽略规则见 [.gitignore](.gitignore)。
