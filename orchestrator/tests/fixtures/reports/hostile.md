---
task_id: hostile-report
phase: reviewer
step: 4
date: 2026-10-06
verdict: approved_with_warnings
---

# Review: <script>alert(1)</script>

Summary for the owner: <img src=x onerror=alert(1)> and [click](javascript:alert(1))

## Summary
A report that tries to be markup: <script>alert(1)</script>, <img src=x onerror=alert(1)>, and a [click](javascript:alert(1)) link.

<div onclick="alert(1)">a raw block</div>

![picture](javascript:alert(1))

## Rated findings

| # | Severity | Finding | Place |
| --- | --- | --- | --- |
| 1 | high | <script>alert(1)</script> | <img src=x onerror=alert(1)> |
| 2 | low | [click](javascript:alert(1)) | "><svg onload=alert(1)> |

## Findings
1. **<b>bold</b>**: `<script>alert(1)</script>` in code, and in a list.

```html
<script>alert(1)</script>
```

> <iframe src="javascript:alert(1)"></iframe>
