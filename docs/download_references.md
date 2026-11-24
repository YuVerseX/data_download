# 数据下载：开源代码对照

下面列出实际查看过的源码及其适用范围。开源示例中的接口、数据版本和时间范围也可能过时，应以提供方当前文档和一个小样本为准。

## GLORYS、DUACS、CMEMS 盐度和 OSTIA

- [Marine Toolbox 的 subset 源码](https://github.com/mercator-ocean/copernicus-marine-toolbox/blob/main/copernicusmarine/python_interface/subset.py)：现有官方接口，处理裁剪、认证、传输及 NetCDF 输出。
- [ROMS-Tools 的 GLORYS 下载 notebook](https://github.com/CWorthy-ocean/roms-tools/blob/main/docs/datasets.ipynb)：直接调用 `copernicusmarine.subset` 下载温盐、流速和海面高度，区域边界可由模型网格计算。
- [NOAA-GFDL 的 GLORYS 下载脚本](https://github.com/NOAA-GFDL/CEFI-regional-MOM6/blob/main/tools/initial/get_glorys_data.sh)：按日循环调用 Toolbox。可以参考参数组织，但其中登录、命令拼接和旧参数不适合照搬到 Windows。

我们的下载底层已与这些项目一致。比较值得简化的是外层流程：通常按月请求并保留文件即可；只有需要单个年文件时才执行年度合并。下载边界用于覆盖模型插值范围时，需要留出余量；`inside` 和 `outside` 应按用途选择，不能直接照抄其他模型的设置。

DUACS、CMEMS 盐度和 OSTIA 可以继续各留一个短脚本，保留产品的变量、日期和网格检查。重复出现几段月份循环或参数解析，并不值得单独建立下载框架。注意本机 Toolbox 2.4.1 没有新文档中提到的 `split-on` 功能，不能仅根据最新版示例删除现有分块逻辑。

## Argo

- [argopy 的 GDAC fetcher 源码](https://github.com/euroargodev/argopy/blob/master/argopy/data_fetchers/gdac_data.py)：从索引查询文件，并行读取后合并为 xarray 数据。区域请求较大时会转而读取整浮标多剖面文件，再筛选区域。
- [NOAA-PMEL OneArgoPy](https://github.com/NOAA-PMEL/OneArgoPy/blob/main/OneArgo.py)：下载索引和浮标文件，检查 NetCDF 能否打开，并用索引 `date_update` 与文件内 `DATE_UPDATE` 判断是否更新。
- [argoFloats 的 getProfiles](https://github.com/ArgoCanada/argoFloats/blob/main/R/get.R) 和 [下载函数](https://github.com/ArgoCanada/argoFloats/blob/main/R/download.R)：先筛选索引，再并行下载原始文件；支持根据索引更新时间刷新文件。

现有“下载压缩索引 → 筛选 → 保存原始剖面”的结构适合本项目，无需换成分析库。脚本已借鉴文件可读性和更新时间检查：重跑会按索引 `date_update` 判断同名文件是否应更新。若以后需要调整值选择、逐层 QC 或合并分析，再使用 argopy。

## MUR SST

- [PO.DAAC downloader 源码](https://github.com/podaac/data-subscriber/blob/main/subscriber/podaac_data_downloader.py)：用 CMR 发现源文件，对原始文件检查提供方校验和，支持 `--subset`。
- [Harmony 裁剪源码](https://github.com/podaac/data-subscriber/blob/main/subscriber/subsetting.py)：检查产品是否支持裁剪，记录并恢复已有 Harmony 任务。
- [MUR 产品入口](https://podaac.jpl.nasa.gov/dataset/MUR-JPL-L4-GLOB-v4.1) 与 [NASA Harmony 示例](https://nasa-openscapes.github.io/earthdata-cloud-cookbook/how-tos/subset.html)。

现有 OPeNDAP 区域裁剪可以继续使用。Harmony 更适合不想维护网格索引和服务端查询语法时采用，但多一套作业与认证处理，暂不强制迁移。PO.DAAC 工具的 `--bounds` 首先用于查找相交原件；需要裁剪文件内容应显式使用 `--subset`，并核对集合支持情况，避免下载全球原件。原始文件校验和也不能直接用于裁剪产物。

## ESA CCI SSS

- [ESA 盐度示例](https://esa-cci.github.io/cci-notebook-viewers/external-notebooks/esa-cci/cci-notebooks/notebooks/sss-cci/)：用 ESA Climate Toolbox 选择变量、时间和区域。示例使用较旧的 15 天盐度产品，并非本仓库 v5.5 七天产品。
- [ESA Climate Toolbox](https://github.com/esa-cci/esa-climate-toolbox)：完整的数据访问与分析工具，短示例依赖其数据存储实现。
- [简短的 OPeNDAP 提取代码](https://github.com/spiccolroaz/CCI_extractor/blob/main/Extract_CCI_data.py)：基于 xarray 的区域 hyperslab 写法。该脚本面向湖泊数据，可以借鉴请求方式，不能直接替代盐度脚本。

现有代码中最难维护的是手动解码 DAP2 响应。以后简化时，优先尝试 xarray 直接打开受约束的 OPeNDAP URL；确认 v5.5 的八个变量、整数缺测值和质量标识都能正确读取后再替换。当前保留可工作的专门处理，比引入整套 ESA 分析工具更合适。

## CCMP

- [RSS 产品说明与读取示例入口](https://remss.com/measurements/ccmp/)。
- [datamatch 的 CCMP 源码](https://github.com/chross22/datamatch/blob/master/R/ccmp.R)：构造每日 RSS URL，下载全球文件，裁剪后清理原件。

这与我们采用的传输方式相同，RSS HTTPS 文件入口不提供区域裁剪。可以参考按请求缓存区域结果和及时清理原件。该第三方代码及其注释也有与 RSS 当前目录不一致的产品描述，不能直接用作科学定义。原始四时次是 00/06/12/18 UTC 分析值；日均是另外的计算，分量均值的模也不是风速均值。

## ERA5 与 SCSORA

ERA5 参考 [CDS API](https://cds.climate.copernicus.eu/how-to-api) 和 [atlite 的 ERA5 源码](https://github.com/PyPSA/atlite/blob/master/atlite/datasets/era5.py)。本项目已经合并为一个入口，详见 [ERA5 用法](era5.md)。

SCSORA 的针对性公开下载代码未找到足够可靠的替代。按 [发布论文的数据入口](https://essd.copernicus.org/articles/18/8471/2026/essd-18-8471-2026-assets.html) 下载年度原件即可。保留字节续传，今后若需持续跟踪源站替换，再给 `.part` 保存对应 ETag。

网上常见的旧 `motuclient` GLORYS 脚本不是优先参考对象。本仓库已使用官方 Toolbox；精简应集中在实际使用的模式和重复流程，不必倒退到旧接口，也无需为了下载几个产品引入完整分析框架。
