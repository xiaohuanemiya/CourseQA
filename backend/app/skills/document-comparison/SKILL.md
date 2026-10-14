---
name: document-comparison
description: 对已定位的课程讲义或试卷进行概览、章节比较和考点分析，保留文件、版本与页码归属。
---

先读取 retrieval 的文档定位与 coverage。定位有歧义时说明需要补充的文件名、年份或课程，不以其他文档替代。概览按照题型或章节归纳；原文没有分值或题量时不猜测。

较长资料可使用 spawn_agent 分别核查独立章节，再由主 agent 合并重复项、保留差异与引用。子任务沿用当前文档范围；search_knowledge 不用于绕开原问题的范围约束。

coverage.complete 为 false 或 excerpt_truncated 为 true 时说明仅覆盖已展示内容。报告资料间矛盾时列出两边的条件、文档和页码，不把不同版本、试题与参考答案混成一条来源。
