# 债务台账

状态机：`open`（未动）/ `doing`（在修）/ `closed`（已清，写清偿版本）。
纪律：每个 feature 版本至少清偿一项（"整理税"，2026-08-14 起）；新增债务必须入账，不许只活在某次对话里。

| # | 债务 | 状态 | 备注 |
|---|---|---|---|
| 1 | **错误码注册表**：码→语义→remediation（retry / retry_via_update_page / change_target / manual / investigating）进 manifest 供机读 | doing | 本周两次"错误码语义写错误导调用方"的根治；0.24.6 载体 |
| 2 | `quote_candidates_unavailable`（弹窗未开族）：等新取证（已带自证据字段） | open | 15 例中 14 例无"滚不进"告警，成因未明 |
| 3 | 深位笔记定位：候选窗口 ~50 篇上限，老笔记引用不可达 | open | 需滚动预算/直达方案设计 |
| 4 | 发布路径封面 `cover_exception`（更新页可用是绕行，发布页待根治） | open | 3/3 失败 vs 更新页 3/3 成功 |
| 5 | 手工删除不落 `deleted_at`（现靠人工补标） | open | 0.25.0 对账设计可能顺带解决 |
| 6 | 视频笔记 content/title 编辑开放（20 次读取实证，待写入验证） | open | P1；真号验证顺带测"发布后更新页可用性" |
| 7 | `_ROW_BAND` 比例判据 vs 固定小视口（号6/号7 ~850 高疑命中） | open | 等 quote_candidates_unavailable 新证据一并看 |
| 8 | note_ops 文档"标题兜底"过时描述 | open | 与 #1 同批清 |
| 9 | docs/design 24 份加状态头（implemented/superseded/pending）+ 索引 | open | 0.24.6 载体 |
| 10 | **同题新旧并存按标题取数必错**：一天咬两口（判"重发无效"/判"互刷缺席"各一次）。消费方必须按 note_id/publish_time 取数 | open | 0.25.0 spec §6 已有测试要求；考虑在 /notes 类端点对同题多行加 `same_title_siblings` 提示字段 |
| 11 | 几何统一层（A1）：8 处滚动/几何实现收编 | open | 长会话引擎地基，排 0.24.6 后 |
| 12 | note_components.py 5019 行拆分（A2）/ 调度器基类（A3） | open | 0.25.x 各版携带 |
| 13 | **自动补量路径 AUTO_WINDOW 散开失效**：2026-08-14 16:12:04 同秒登记 7 张补量单并行执行（done 16:21-16:52 交叠）——"一拥而上"正是调度器 docstring 警告的补量特征形态 | open | 长会话引擎重做编排层时可能直接吸收；先查触发源与 not_before 是否被 worker 尊重 |
| 14 | 熔断篇数一日内 6→8：新触发 2 篇待核（真不可达 or 假阳性二例） | open | 结合 #2/访客延迟假设一并看 |
