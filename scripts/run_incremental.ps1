# A股每日增量采集脚本，由 Windows 任务计划程序调用。
$ErrorActionPreference = "Stop"

# 切换到项目目录，确保 .env 和 editable 包路径都按本项目解析。
Set-Location -LiteralPath "F:\astocks-collector"

# 日志级别使用 INFO，便于排查每日任务结果。
$env:LOG_LEVEL = "INFO"

# 采集最近 10 天窗口，重复运行会通过唯一键幂等更新。
astocks-collector incremental --days 10 *>> "F:\astocks-collector\logs\scheduled-incremental.log"
