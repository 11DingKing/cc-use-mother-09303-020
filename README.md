# 合作设备共享预约

本项目维护合作设备共享预约的领域约定、角色边界与样例数据，并提供资源中心完整服务端：把设备能力、工位容量、维护窗口、课程需求、跨时区时段和指导教师日程汇成整体占用计划，支撑两阶段预约、原子级联变更与机构隔离。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/resource_center/`：资源中心服务端（模型、持久化、规划、服务、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动资源中心服务端。
- `tools/demo_scenario.py`：端到端演示维护窗口延长后的原子释放与重新安排。
- `tests/`：契约与服务端回归测试。

## 资源中心服务端

### 状态机

```
held(暂占) → confirmed(确认) → in_use(使用) → released(释放)
   │             │
   ▼             ▼
expired(过期)   cancelled(取消) / released(级联释放)
```

- **两阶段预约**：申请时在同一事务内暂锁设备、工位与指导教师（`held`，含 `hold_expires_at`），确认后生效（`confirmed`）。
- **原子级联**：设备停用、维护窗口登记/延长、改期、取消都在单事务内更新全部关联占用——不会只释放机器而留下工位和指导教师。
- **可解释冲突**：冲突返回 409，附匿名化的冲突原因与替代方案（同时段替代资源 / 时间平移），不泄露其他机构预约细节。
- **过期恢复**：过期暂占由后台巡检释放；服务重启时 `recover()` 继续释放宕机期间的过期暂占。
- **机构隔离**：除 `/health` 与 `/admin/*` 外均需 `X-Institution-Id`；机构只能查看与自己预约相关的细节，其余占用仅以匿名忙块呈现。

### 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /applications` | 申请：暂锁全部资源，冲突返回 409 + 替代方案 |
| `POST /bookings/{id}/confirm` | 确认暂占（仅使用院校，需在有效期内） |
| `POST /bookings/{id}/cancel` | 取消并释放全部占用 |
| `POST /bookings/{id}/reschedule` | 改期：原子替换占用，冲突时原占用不变 |
| `POST /bookings/{id}/start` / `complete` | 开始使用 / 完成释放 |
| `GET /bookings` / `GET /bookings/{id}` | 仅返回与本机构相关的预约 |
| `GET /plan` | 整体占用计划（相关预约含细节，其余匿名忙块） |
| `GET /catalog` | 资源目录（静态信息，不含占用细节） |
| `POST /equipment/{id}/deactivate` / `activate` | 停用/启用设备（所属机构或管理员），停用级联释放 |
| `POST /equipment/{id}/maintenance-windows` | 登记维护窗口，重叠预约整体释放并附替代方案 |
| `POST /maintenance-windows/{id}/extend` | 延长维护窗口，新重叠预约整体释放 |
| `POST /admin/*` | 资源登记与手动巡检（需 `X-Admin-Token`） |

时间一律以 `start_local` + `timezone`（IANA 时区名）提交，服务端统一换算为 UTC 存储。

### 运行

```bash
python3 tools/run_server.py --db data/resource_center.db --port 8080 --seed
```

管理员令牌默认 `dev-admin-token`，可用环境变量 `RESOURCE_CENTER_ADMIN_TOKEN` 覆盖。

端到端演示（维护窗口延长 → 原子释放 → 替代方案 → 重新安排 → 过期恢复 → 机构隔离）：

```bash
python3 tools/demo_scenario.py
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
