# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。

## 争议冻结与记录保留

档案管理员（负责人岗位）在收到患者争议通知后先冻结相关记录，再通过"先预览、后执行"的流程清理到期资料。相关操作需要 `records:manage`（写）或 `records:read`（读，内审岗）权限，全部进入审计哈希链。

### 争议冻结

- `POST /records/freezes` 建立冻结。范围可组合指定：`patient_id`（患者，含其后被合并进来的来源档案）、`scope_start`/`scope_end`（记录锚点时间所在的闭区间）以及 `associated_records`（显式关联档案，元素含 `category` 与 `id`，类别取八类受管记录或 `encounter`）。三者至少提供一项；必须填写 `reason` 与外部 `notice_ref`（争议通知编号）。仅含显式关联档案的冻结只保护所列记录，不扩大到其他资料。
- `GET /records/freezes?state=active|released`、`GET /records/freezes/{freeze_id}` 查询冻结。
- `POST /records/freezes/{freeze_id}/release` 解除冻结，必须提供外部通知依据 `external_ref`，可附 `doc_digest`（SHA-256）与 `note`。**解除人不得是设置人**；已解除的冻结不能重复解除。

冻结期间：

- 例行保留清理在预览中将命中记录计入 `frozen_skipped`，执行时不得删除；
- 患者导出若未包含冻结覆盖的章节，返回 `409 conflict`（`missing_sections` 指明遗漏章节）；包含时导出体附带 `frozen_records` 清单并写入审计。

### 保留策略与清理预览

受管记录类别固定为 `consent`、`assessment`、`plan`、`appointment`、`followup`、`observation`、`clinical_flag`、`incident`，各类别按记录自身锚点时间与终态判断（如 `appointment` 须为 `cancelled`/`no_show`）。患者档案、已签署病历（`encounters`/`encounter_notes`）、审计事件在任何策略下都不删除；仍被就诊病历、库存不可变流水或其他在线记录引用的父记录计入 `referenced_skipped`，计划修订/节点与事件处置流水等聚合私有子记录随父记录一并清理。

- `POST /retention/policy` 提交规则列表（每项 `category` 与 `retention_days`，1–36500 天，同类唯一）。每次提交生成递增的新版本并作废旧版本；`GET /retention/policy` 读取当前生效版本。
- `POST /retention/previews` 基于当前生效策略生成可复核预览：返回各类别 `candidates`、`frozen_skipped`、`referenced_skipped`、`removable` 统计、逐条候选（含命中的冻结编号）以及范围 `fingerprint`。
- `POST /retention/previews/{id}/refresh` 以当前数据重新计算范围。**策略版本变更后，旧预览既不能刷新也不能执行**，只能基于新版本重新生成。
- `POST /retention/previews/{id}/execute` 执行清理。执行前固定校验：策略版本一致且范围指纹未变化；预览与执行之间有新产生、被合并或冻结状态变化的记录时返回 `409 conflict`，须刷新复核。执行结果按预览编号唯一落库，重复调用安全重放（返回首次结果，`replayed: true`，不重复删除、不追加审计）。

