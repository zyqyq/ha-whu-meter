# WHU Electricity Meter Home Assistant

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

1. 把 `custom_components/whu_meter/` 整个目录拷到 HA 配置目录：

   - 标准安装：`<config>/custom_components/whu_meter/`
   - Docker（容器名 `homeassistant`，在宿主机执行）：
     ```bash
     docker cp custom_components/whu_meter homeassistant:/config/custom_components/
     docker restart homeassistant
     ```

2. 重启 Home Assistant。
3. 设置 → 设备与服务 → 添加集成 → 搜索 **“WHU Electricity Meter / 武大水电表”**。
4. 按向导填写：
   - **网页根目录地址**：手机端打开的 URL 中 `#` 之前的部分，默认
     `http://zwhqbsd.whu.edu.cn`（如果部署在内网别的地址，照实填）。
   - **区域 → 楼栋 → 楼层 → 房间**：逐级下拉选择（房间列表实时从服务器拉取），
     多表房间会再让你选一次表具。

## 选项（抓取配置）

集成条目 → “配置” 里可随时改：

- **抓取频率**：每 5~1440 分钟一次（默认 15 分钟；登录+查询约 2 个请求/次）。
- **余额告警阈值**：默认 20 元。

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

---

## 版本发布流程

推荐使用交互向导：`python scripts/release.py`（不带参数运行）。

1. **选择新版本号**：基于 git 上最新 tag 给出主版本 / 次版本 / 修订号三种递增方案，也可自定义输入（如 v1.1.5 -> v2.0.0 / v1.2.0 / v1.1.6）
2. **工作区检查**：列出未提交文件（已暂存/未暂存/未跟踪），可选 合并提交（并入发版 commit，未跟踪文件需自行处理后继续）/ 撤回（丢弃，需二次确认）/ stash 保留（发版结束后自动复原）
3. **发版 commit**：选择是否同步「用户使用说明.md」（页首适用版本 + 页尾版本字样），与 manifest.json 一并提交为 `chore(release): bump version to vX.Y.Z`
4. **打 tag**：自动在发版 commit 上打 tag
5. **发布**：编写 Release notes（自动生成 / 手动 / 混合），支持先 dry-run 预览再按预览执行；发布同时推送发版 commit 与 tag，创建/更新 Release 并上传 `whu_meter_vX.Y.Z.zip`

熟练后可用命令行模式（要求工作区干净）：`python scripts/release.py v1.1.5 --notes "说明" [--no-doc] [--dry-run]`。

首次使用前，将 GitHub PAT（repo 权限）写入仓库根目录 `.github_token` 文件，或设置环境变量 `GITHUB_TOKEN`。
