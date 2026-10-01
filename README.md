# WHU Electricity Meter（武大水电表）Home Assistant 集成

对接武汉大学水电服务平台（`http://zwhqbsd.whu.edu.cn/MobilePayWeb` 背后的 `ICBS_V2_Server` 接口），
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

设备会以楼栋名（如“信息学部9舍”）作为建议区域，方便挂到 HA 的区域/地板下。

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

## 迁移到正式系统

集成无外部依赖（只用 HA 内置 httpx），迁移只需三步：

1. 新 HA 实例装入 `custom_components/whu_meter/` 并重启；
2. 重新走一遍添加集成向导（或先备份/恢复 `/config` 下的
   `.storage/core.config_entries`，配置条目会原样迁移，无需重新配置）；
3. 在能源面板里重新勾选「累计电量」实体（实体 ID 按新实例重新生成）。

## 已知边界

- 登录用的是水电平台前端内置的服务账号（与官方页面行为一致），只做只读查询；
  请勿用本集成对平台做高频轮询（建议 ≥5 分钟）或遍历他人房间。
- `GetMeterMonthValue`（月用量）接口在服务端返回空，月度统计请用能源面板或
  基于日值传感器自行聚合。
- 若平台修改了前端内置账号或签名算法，需要同步更新 `const.py` / `api.py`。

---

## v1.1.0 新增：余额过低邮件警报自动化（集成自动管理）

集成现在会**自动创建并维护**一条「电费余额过低邮件警报」自动化（id: `whu_meter_balance_low_email`）：

- **安装/重载集成时**：自动在 `automations.yaml` 中写入（或原位更新）这条自动化——余额过低 binary_sensor 变为 `on` 时，通过 SMTP 通知实体发送 HTML 警告邮件（渐变卡片 + 余额大数字 + 进度条 + 用量表 + 充值按钮）。
- **删除集成时**：自动化一并移除，不留垃圾。
- **选项流新增两项**（设置 → 设备与服务 → WHU Electricity Meter → 配置）：
  - `余额过低邮件警报`：开关（默认开）
  - `SMTP 通知实体`：选择已配置的邮件通知实体（默认 `notify.home_assistant_manager`，即 UI 方式添加的 SMTP 通知）
- 自动化在 HA UI「自动化与场景」中可见、可手动编辑；但集成每次重载会按选项重新生成（手动改动会被覆盖，如需自定义请复制一条新自动化）。
- 前置条件：先在 HA 中配置好邮件通知（如 UI 添加 SMTP 集成生成 notify 实体），否则启动日志会有警告且不会创建自动化。

## v1.1.1 修复：弃用的 notify.<name> 服务调用 → smtp.send_message

HA 2027.3 起移除 legacy `notify.<实体名>` 服务。警报自动化已迁移为官方推荐的实体式调用：

```yaml
action:
  - action: smtp.send_message
    target:
      entity_id: notify.home_assistant_manager   # 选项中选择的 SMTP 通知实体
    data:
      title: ...
      message: ...
      html: ...   # html 现为顶层字段（不再是 data.data.html）
```

注意：`email_notify_entity` 选项现在必须选择 **SMTP 集成生成的 notify 实体**（如 `notify.home_assistant_manager`）；若选择其他平台（如手机通知）的 notify 实体，`smtp.send_message` 无法发送。

## v1.1.2 变更

- 邮件模板微调：预计可用天数由「1~2 天」改为「4~5 天」；余额比例条下方说明文字由「余额 / 告警阈值 比例条」精简为「余额 / 告警阈值」。


## v1.1.3 变更

- 新增集成自带图标：按 HA 2026.3+ 规范在集成目录下添加 `brand/` 文件夹（icon/logo 各 256px 与 512px 两档），安装后在集成列表与配置页显示绿色电费图标，无需再使用默认拼图占位图。


## 版本发布流程

1. 提交代码后打 tag：`git tag v1.1.4`
2. 手动运行发布脚本：`python scripts/release.py` —— 不带参数运行会进入中文交互向导（数字选择 tag / notes 方式 / 发布或预览），也可直接用命令行参数：`python scripts/release.py v1.1.4 --notes "手写说明" --dry-run`
3. 脚本自动完成：manifest 版本号与 tag 对齐 -> 打包 `whu_meter_vX.X.X.zip` -> 推送分支与 tag -> 在 GitHub 创建/更新 Release 并上传 zip -> 综合上一个 tag 以来的 commit 生成 Release notes

首次使用前，将 GitHub PAT（repo 权限）写入仓库根目录 `.github_token` 文件，或设置环境变量 `GITHUB_TOKEN`。

