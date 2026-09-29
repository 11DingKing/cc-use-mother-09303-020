# 合作设备共享预约

本项目维护合作设备共享预约的领域约定、角色边界与样例数据，并提供资源中心完整服务端：把设备能力、工位容量、维护窗口、课程需求、跨时区时段和指导教师日程汇成整体占用计划。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/resource_center/`：资源中心服务端（模型、存储、业务服务、HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动资源中心服务。
- `tests/`：契约与服务端回归测试。

## 服务端能力

- **组合暂锁再确认**：申请在同一事务内暂锁设备、工位、指导教师全部资源，任一冲突则整体失败，不留部分占用；确认后暂占转正。
- **原子更新关联占用**：设备停用、维护窗口延长、改期、取消都在单事务内整体更新关联占用——受冲击预约自动整体改期，找不到可行时段则组合级释放并转入受扰待重排，杜绝"只释放机器、工位和教师仍被占用"。
- **可解释替代方案**：冲突响应逐条说明原因（维护窗口、占用重叠、容量不足、教师日程不覆盖等），并给出整体平移时段与同能力设备替换建议。
- **过期恢复**：暂占带过期时间，运行期由后台线程清理，服务重启后 `recover` 继续释放过期暂占。
- **机构隔离**：请求头 `X-Institution-Id` 标识身份，各机构只能查看与自己预约相关的细节，占用计划中他机构预约脱敏为忙碌块；资源中心身份 `resource-center` 可管理全部。

## 接口概览

- `POST /api/bookings` 申请（暂锁全部资源）；`POST /api/bookings/{id}/confirm|cancel|reschedule|in-use|complete` 生命周期操作。
- `GET /api/bookings`、`GET /api/bookings/{id}`、`GET /api/plan?start=&end=` 机构隔离查询。
- `POST /api/institutions|equipment|workstations|instructors` 资源登记；`POST /api/instructors/{id}/availability` 教师日程。
- `POST /api/maintenance-windows`、`POST /api/maintenance-windows/{id}/extend` 维护窗口与延长；`POST /api/equipment/{id}/disable|enable` 设备停用与恢复。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

启动服务：`python3 tools/run_server.py [数据库路径] [端口]`
