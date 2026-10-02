# WHU Electricity Meter Home Assistant

[![Release](https://img.shields.io/github/v/release/zyqyq/ha-whu-meter?label=release)](https://github.com/zyqyq/ha-whu-meter/releases)
[![Validate](https://github.com/zyqyq/ha-whu-meter/actions/workflows/validate.yml/badge.svg)](https://github.com/zyqyq/ha-whu-meter/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/zyqyq/ha-whu-meter)](LICENSE)

对接水电服务平台（`http://zwhqbsd.whu.edu.cn/MobilePayWeb`），
自动抓取宿舍电表的余额、累计电量、昨日/今日用量、上次抄表时间，并提供余额过低告警。
「累计电量」传感器可直接接入 HA **能源（Energy）面板**。

## 功能

| 实体 | 类型 | 说明 |
|---|---|---|
| 账户余额 | sensor (CNY) | 预付费账户余额，含电表状态/回路/电价方案/上次抄表等属性 |
| 累计电量 | sensor (kWh, total_increasing) | **接入能源模块用这个**（电表总读数，单调递增） |
| 昨日用量 | sensor (kWh) | 系统每日结算后的前一日用量与电费 |
| 今日用量 | sensor (kWh) | 当日已结算用量（未到结算点为部分值） |
| 上次抄表时间 | sensor (timestamp) | readTime |
| 余额过低 | binary_sensor (problem) | 余额 < 阈值（默认 20 元，可在选项里改）时置 on |

设备会以楼栋名（如“信息学部11舍”）作为建议区域，方便挂到 HA 的区域/地板下。

## 安装

本仓库为标准的 HACS 集成仓库结构（仓库根目录下 `custom_components/whu_meter/`），
HACS 与 GPM 均可直接识别，安装后可自动检查更新。

### 方式一：HACS（推荐）

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zyqyq&repository=ha-whu-meter&category=integration)

点击上方按钮，或手动操作：

1. HACS → 右上角 ⋮ → **自定义存储库**
2. 填入 `https://github.com/zyqyq/ha-whu-meter`，类别选 **集成**，添加
3. 在 HACS 中搜索 **WHU 宿舍电费**，下载
4. 重启 Home Assistant

之后 HACS 会依据本仓库的 Release / tag 提示新版本，可在 HACS 内一键升级。

### 方式二：GPM（GIT Package Manager）

已安装 [GPM](https://github.com/tomasbedrich/gpm) 时：

1. 设置 → 设备与服务 → 添加集成 → **GPM**
2. 仓库地址填 `https://github.com/zyqyq/ha-whu-meter`
3. GPM 会自动创建更新实体，新 tag / commit 发布后可一键更新

### 方式三：手动安装

1. 把 `custom_components/whu_meter/` 整个目录拷到 HA 配置目录：

   - 标准安装：`<config>/custom_components/whu_meter/`
   - Docker（容器名 `homeassistant`，在宿主机执行）：
     ```bash
     docker cp custom_components/whu_meter homeassistant:/config/custom_components/
     docker restart homeassistant
     ```

2. 重启 Home Assistant。

## 配置

设置 → 设备与服务 → 添加集成 → 搜索 **“WHU Electricity Meter / 武大水电表”**，
按向导填写：

- **网页根目录地址**：手机端打开的 URL 中 `#` 之前的部分，默认
  `http://zwhqbsd.whu.edu.cn`（如果部署在内网别的地址，照实填）。
- **区域 → 楼栋 → 楼层 → 房间**：逐级下拉选择（房间列表实时从服务器拉取），
  多表房间会再让你选一次表具。

## 选项（抓取配置）

集成条目 → “配置” 里可随时改：

- **抓取频率**：每 5~1440 分钟一次（默认 15 分钟；登录+查询约 2 个请求/次）。
- **余额告警阈值**：默认 20 元。
- **余额过低邮件警报**：开启后自动创建 / 维护告警自动化，可指定 SMTP 通知实体。

## 接入能源模块

设置 → 仪表盘 → 能源 → 电网消耗 → 添加传感器：
选择 **“累计电量”**（device_class=energy，state_class=total_increasing，kWh）即可。
历史统计从实体创建时刻开始累积；电表总读数本身单调递增，适合直接作为电网购电量。

## 已知边界

- 登录用的是水电平台前端内置的服务账号（与官方页面行为一致），只做只读查询；
  请勿用本集成对平台做高频轮询（建议 ≥5 分钟）或遍历他人房间。
- `GetMeterMonthValue`（月用量）接口在服务端返回空，月度统计请用能源面板或
  基于日值传感器自行聚合。
- 若平台修改了前端内置账号或签名算法，需要同步更新 `const.py` / `api.py`。

## 文档

- [用户使用说明](docs/用户使用说明.md) —— 面向使用者：安装、配置、实体、告警邮件、常见问题
- [开发者说明](docs/开发者说明.md) —— 面向维护者：仓库结构、HACS/GPM 合规要点、发布流程、网络排查

## 许可

[MIT](LICENSE)
