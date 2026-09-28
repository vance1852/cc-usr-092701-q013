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

## 保留期限与争议冻结

例行清理只作用于三类从未进入临床或库存流水的运营记录，按记录类别分别配置保留天数：`appointment_cancelled`（已取消或未到诊、且无就诊记录与库存预留的预约）、`followup_closed`（已完成或已取消的随访）、`assessment_draft`（未签署、且未被计划引用的评估草稿）。已签署病历、授权、计划、观察值、不良事件、库存流水与审计哈希链不属于任何可清理类别，清理永远不会触及它们。

- `POST /retention/policies` 由诊所负责人提交 `{record_category: 保留天数}` 建立新版本策略；每次提交版本号递增，`GET /retention/policy` 返回当前版本。
- `POST /patients/{patient_id}/retention-freezes` 由档案/合规岗设置争议冻结，范围包含患者、诊所时区日期区间（`scope_start`、`scope_end`）和 `related_records`（可清理类别档案的显式编号），并登记 `external_notice_ref` 外部争议通知编号。
- `GET /retention/freezes`（可按 `patient_id`、`state` 过滤）和 `GET /retention/freezes/{freeze_id}` 查询冻结；`POST /retention/freezes/{freeze_id}/release` 解除冻结。
- 冻结设置人与解除人必须是不同人员（服务端强制执行）；解除必须提供 `release_notice_ref`（外部通知依据）和 `release_reason`，否则拒绝。
- 活动冻结期间，落在患者（含其合并来源）日期区间内或被显式关联的候选记录不会被清理；这些档案也不会从患者导出中遗漏（合并来源档案一并并入导出）。
- `POST /retention/previews` 生成可复核预览，统计候选总数、计划清理数和因冻结跳过数，并列出每条记录及其冻结依据；`GET /retention/previews/{run_id}` 取回预览。
- `POST /retention/previews/{run_id}/execute` 执行清理，必须提供 `Idempotency-Key`。执行时以预览时点实时重算范围：策略版本变更、预览后有新产生/被签署/被合并的记录或冻结状态变化，旧预览一律拒绝执行，需重新生成；仅时间流逝而数据不变时预览仍有效。
- 执行按幂等编号安全重放，重复请求返回同一结果且不重复删除；每次删除在 `retention_cleaned` 留存处置记录，`GET /retention/runs` 可查历次预览与执行，全程写入审计哈希链。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
- 争议冻结：生效 → 已解除（解除人不得为设置人，且必须记录外部通知依据）。
- 保留策略：每次提交生成递增的新版本；清理批次为已预览 → 已执行，预览绑定生成时的策略版本与范围指纹。
