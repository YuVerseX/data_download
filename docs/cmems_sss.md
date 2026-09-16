# Copernicus Marine 海表盐度与密度

脚本：`download_cmems_sss.py`。产品为 `MULTIOBS_GLO_PHY_S_SURFACE_MYNRT_015_013`，使用官方 `copernicusmarine` Toolbox 的 `open_dataset` 和 `subset` 接口。

## 默认配置

- 区域：西北太平洋 `100 180 0 60`，顺序为 `W E S N`。
- 频率：逐日；`--frequency monthly` 可改为逐月。
- 变量：`sos sos_error dos dos_error sea_ice_fraction`。
- 数据：全球 L4、0.125° 网格、表层单层。
- 数据源：`--source auto` 在 2024 年以前使用 MY，2024-01-01 起使用 NRT；跨边界自动拆分。
- 输出：按数据源和年份保存 NetCDF，失败只需重下对应文件。

变量含义：`sos` 为海表盐度，`sos_error` 为盐度误差，`dos` 为海表密度（kg/m³），`dos_error` 为密度误差，`sea_ice_fraction` 为海冰面积百分比。

## 区域选择

官方 `coordinates_selection_method="inside"` 会选择请求区间内部的格点，并同时作用于经度、纬度、时间和深度。默认边界 `100 180 0 60` 对应的实际格点中心是：

- 经度 `100.0625` 至 `179.9375`，共 640 点；
- 纬度 `0.0625` 至 `59.9375`，共 480 点。

这是格点中心与请求边界的正常差异。没有使用 `strict-inside`，因为产品最东格点中心为 `179.9375`，请求上界 `180` 会被严格边界检查视为超出坐标范围。脚本下载前读取官方远端坐标，打印实际格点边界，并验证请求的每个时间戳都存在，防止 `inside` 静默裁短超出时间覆盖的请求。

脚本接受 `-180 <= W < E <= 180`，不支持一个 bbox 跨越日期变更线；这种区域应拆成两个命令下载。

## 安装与认证

```powershell
python -m pip install -r requirements.txt
copernicusmarine login
```

不要把账号密码写进脚本或命令行。Toolbox 会读取 `copernicusmarine login` 保存的凭据。

## 使用

```powershell
# 先检查计划、远端覆盖和流量估算，不下载
python download_cmems_sss.py --start-date 2024-01-01 --end-date 2024-12-31 -n

# 下载默认西北太平洋区域和全部变量
python download_cmems_sss.py --start-date 2024-01-01 --end-date 2024-12-31 -o "D:\CMEMS_SSS"

# 只下载盐度与盐度误差
python download_cmems_sss.py --start-date 2001-01-01 --end-date 2001-12-31 -v sos sos_error -o "D:\CMEMS_SSS"

# 下载月产品
python download_cmems_sss.py --start-date 2001-01-01 --end-date 2023-12-31 --frequency monthly -v sos sos_error -o "D:\CMEMS_SSS"

# 自定义区域
python download_cmems_sss.py --start-date 2026-08-01 --end-date 2026-08-31 --bbox 120 150 20 50 -v sos -o "D:\CMEMS_SSS"
```

日期必须写为 `YYYY-MM-DD`。月产品只选择请求区间内落在每月 1 日的时间戳；建议起止日期都使用月初。`--dataset-version` 可固定官方目录版本，`--source my|nrt` 可覆盖自动选择。MY 与 NRT 使用不同版本号，因此跨源请求不能同时指定 `--dataset-version`，需要拆成两个命令。

## 校验与恢复

下载前先执行官方 dry-run。下载写入同盘 `.partial.nc`，随后检查时间轴、经纬度网格、变量维度并分块读取全部数据，最后才原子改名。文件中会写入本次请求参数用于重复运行校验。

重复运行同一命令时，通过校验的文件会跳过。损坏或参数不同的正式文件默认保留，只有 `--overwrite` 才替换。每个文件默认尝试 3 次，可用 `--retries` 修改。Toolbox 的区域裁剪可能产生明显大于最终 NetCDF 的网络传输量，长时间段先用 `-n` 查看估算。

## 官方资料

- 产品下载页：https://data.marine.copernicus.eu/product/MULTIOBS_GLO_PHY_S_SURFACE_MYNRT_015_013/download
- Product User Manual：https://documentation.marine.copernicus.eu/PUM/CMEMS-MOB-PUM-015-013.pdf
- Toolbox subset：https://toolbox-docs.marine.copernicus.eu/en/stable/usage/subset-usage.html
